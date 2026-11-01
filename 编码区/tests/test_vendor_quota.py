#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_vendor_quota.py —— 第三方直采：凭据保险仓 / 额度三级降级 / 登录即同步。

覆盖：
  · 凭据加密落盘后**磁盘上无明文**，掩码只出 `xx●●●xx`，口令错则解不开；
  · 额度降级链 live → manual → doc → none，各层都带 source 印章；
  · `POST /api/official/vendor-login` 一步到位：存盘 + 立即抓额度 + 与本地账本对账；
  · 浏览器探针拿不到 profile 时如实回 `profile_unavailable`，不伪造会话；
  · 官方「点数」与本地「请求数」不同口径时不硬算差额。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core import quota  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.official_ledger import OfficialLedger, compare  # noqa: E402
from core.vendor_creds import VendorCreds, VendorCredsError, mask_creds  # noqa: E402
from core.vendor_session import ProfileUnavailable  # noqa: E402

SECRET = "sk-live-VENDOR-SECRET-42"


class TestVendorCreds(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault_"))
        self.vault = VendorCreds(self.tmp / "vendor_creds.json",
                                 passphrase="pw-under-test",
                                 key_path=self.tmp / "vendor_creds.key")

    def test_roundtrip_and_no_plaintext_on_disk(self):
        saved = self.vault.save("opencode", {"username": "neo", "token": SECRET})
        self.assertTrue(saved["stored"])
        self.assertEqual(self.vault.get("opencode")["token"], SECRET)
        raw = (self.tmp / "vendor_creds.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw, "磁盘上绝不能出现明文令牌")
        self.assertIn("blob", raw)

    def test_masked_view_hides_secret(self):
        self.vault.save("tokenharbor", {"username": "neo", "password": SECRET})
        masked = self.vault.masked("tokenharbor")["masked"]
        self.assertEqual(masked["username"], "neo")
        self.assertNotIn(SECRET, json.dumps(masked))
        self.assertIn("●●", masked["password"])

    def test_wrong_passphrase_fails_loud(self):
        self.vault.save("opencode", {"token": SECRET})
        intruder = VendorCreds(self.tmp / "vendor_creds.json",
                               passphrase="wrong-pw", key_path=self.tmp / "k2")
        with self.assertRaises(VendorCredsError):
            intruder.get("opencode")

    def test_drop_removes_channel(self):
        self.vault.save("trae", {"token": SECRET})
        self.assertTrue(self.vault.drop("trae"))
        self.assertEqual(self.vault.channels(), [])
        self.assertFalse(self.vault.drop("trae"))

    def test_mask_creds_keeps_non_secret_fields(self):
        out = mask_creds({"username": "neo", "realm": "intl", "cookie": SECRET})
        self.assertEqual(out["username"], "neo")
        self.assertEqual(out["realm"], "intl")
        self.assertNotIn(SECRET, json.dumps(out))


class TestQuotaFallback(unittest.TestCase):
    def test_live_wins(self):
        got = quota.fetch_channel_quota(
            "demo", base_url="https://x/v1",
            fetcher=lambda ch, base, tok, hdrs: {"used": 30, "limit": 100, "unit": "requests"})
        self.assertEqual(got["source"], "live")
        self.assertEqual(got["used_pct"], 30.0)
        self.assertEqual(got["remaining"], 70.0)

    def test_manual_when_live_unreachable(self):
        got = quota.fetch_channel_quota(
            "demo", base_url="https://x/v1", fetcher=lambda *a: None,
            manual={"used": 45, "limit": 100, "unit": "points", "note": "面板录入"})
        self.assertEqual(got["source"], "manual")
        self.assertEqual(got["used_pct"], 45.0)

    def test_doc_then_none(self):
        doc = quota.fetch_channel_quota("demo", doc={"used": 8, "limit": 10})
        self.assertEqual((doc["source"], doc["used_pct"]), ("doc", 80.0))
        empty = quota.fetch_channel_quota("demo")
        self.assertEqual(empty["source"], "none")
        self.assertIsNone(empty["used_pct"], "拿不到就必须是 None，不能编 0")

    def test_remaining_only_derives_pct(self):
        got = quota.fetch_channel_quota("demo", manual={"remaining": 25, "limit": 100})
        self.assertEqual(got["used"], 75.0)
        self.assertEqual(got["used_pct"], 75.0)

    def test_official_windows_are_split_not_merged(self):
        """官方按日/5 小时/周/月分窗报时，每个窗口各自归一，不拼成一个总数。"""
        body = {"used": 30, "limit": 100, "unit": "requests",
                "five_hour": {"used": 8, "limit": 20, "unit": "requests"},
                "weekly": {"used": 60, "limit": 200, "unit": "requests"},
                "month": {"used": 210, "limit": 900, "unit": "requests"}}
        row = quota._extract("demo", body)
        self.assertEqual(row["used_pct"], 30.0)
        wins = row["windows"]
        self.assertEqual(sorted(wins), ["h5", "month", "week"])
        self.assertEqual(wins["h5"]["used_pct"], 40.0)
        self.assertEqual(wins["week"]["used_pct"], 30.0)
        self.assertEqual(wins["month"]["remaining"], 690.0)

    def test_windows_absent_when_upstream_says_nothing(self):
        row = quota.fetch_channel_quota("demo", manual={"used": 1, "limit": 2})
        self.assertEqual(row["windows"], {}, "manual/doc 层没有分窗就保持空，不补 0")

    def test_snapshot_roundtrip(self):
        path = Path(tempfile.mkdtemp(prefix="qsnap_")) / "quota_snapshots.json"
        quota.save_snapshot("opendesign", {"used": 550, "limit": 1000, "unit": "pts"}, path=path)
        row = quota.load_snapshots(path)["opendesign"]
        self.assertEqual(row["used"], 550)
        self.assertEqual(quota.source_label("manual"), "MANUAL")


class TestOfficialReconcile(unittest.TestCase):
    def test_pending_windows_not_frozen_by_cache(self):
        """后台还在取分窗时不能把「取数中」缓存 60 秒：官方数字到了必须马上出。"""
        class _Ledger:
            def rows(self, since=None):
                return []

        led = OfficialLedger(_Ledger(), ttl=60)
        pending = {"source": "none", "windows": {}, "windows_pending": True}
        self.assertTrue(led.entry("ollama", pending)["official"]["windows_pending"])
        fresh = {"source": "live", "used": 1.0, "unit": "USD",
                 "windows": {"today": {"used": 0.0}}, "windows_pending": False}
        got = led.entry("ollama", fresh)
        self.assertFalse(got["official"]["windows_pending"])
        self.assertEqual(sorted(got["official"]["windows"]), ["today"])

    def test_same_unit_compares(self):
        got = compare("c", {"used": 10, "unit": "requests", "source": "live"},
                      {"requests": 7, "spent_cny": 0.5})
        self.assertTrue(got["comparable"])
        self.assertEqual(got["delta_requests"], 3)
        self.assertEqual(got["verdict"], "official_ahead")

    def test_points_never_mixed_with_cny(self):
        got = compare("c", {"used": 10, "unit": "points", "source": "live"},
                      {"requests": 7, "spent_cny": 0.5})
        self.assertFalse(got["comparable"])
        self.assertIsNone(got["delta_requests"])
        self.assertEqual(got["verdict"], "units_differ")

    def test_no_official_source_is_not_units_differ(self):
        """一层官方数都没取到：如实报 no_official，不能套「单位不同」暗示手里有点数。"""
        got = compare("c", {"used": None, "unit": "", "source": "none"},
                      {"requests": 7, "spent_cny": 0.5})
        self.assertFalse(got["comparable"])
        self.assertIsNone(got["delta_requests"])
        self.assertEqual(got["verdict"], "no_official")
        self.assertIn("未取到官方额度", got["note"])

    def test_cache_ttl(self):
        ledger = LocalLedger(Path(tempfile.mkdtemp()) / "l.db",
                             Path(tempfile.mkdtemp()) / "p.json")
        official = OfficialLedger(ledger, ttl=60)
        first = official.entry("c", {"used": 1, "unit": "requests", "source": "live"})
        second = official.entry("c", {"used": 99, "unit": "requests", "source": "live"})
        self.assertIs(first, second, "60s 内应命中缓存")
        official.invalidate("c")
        third = official.entry("c", {"used": 99, "unit": "requests", "source": "live"})
        self.assertIsNot(first, third)


class TestVendorLoginHTTP(unittest.TestCase):
    """登录即同步：一次请求完成存盘 + 抓额度 + 对账。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vlogin_"))
        self.old = {
            "LEDGER": server.LEDGER,
            "CONTROL": server.CONTROL,
            "MODEL_ROUTER": server.MODEL_ROUTER,
            "MODEL_UPSTREAM": server.MODEL_UPSTREAM,
            "MODEL_TOKEN": server.MODEL_TOKEN,
            "TOKEN": server.TOKEN,
            "LEDGER_MODE": server.LEDGER_MODE,
            "VENDOR_CREDS": server.VENDOR_CREDS,
            "_fetch_quota": server._fetch_quota,
        }
        self.addCleanup(self._restore)

        pricing = self.tmp / "pricing.json"
        pricing.write_text(json.dumps({"models": {
            "glm-5.3": {"in": 0.7, "cache": 0.07, "out": 2.8}}}), encoding="utf-8")
        self.ledger = LocalLedger(self.tmp / "ledger.db", pricing)
        self.ledger.record(model="glm-5.3", channel="opencode", prompt=10, completion=2,
                           status=200, ok=True)
        server.LEDGER = self.ledger
        self.control = LocalControl(self.tmp / "control.json", default_channel="opencode")
        self.control.set_budget("global", 10)          # 让“本地闸门”那一轴真有数
        server.CONTROL = self.control
        server.MODEL_ROUTER = _FakeRouter()
        server.MODEL_UPSTREAM = "http://127.0.0.1:9/v1"
        server.MODEL_TOKEN = ""
        server.TOKEN = ""
        server.LEDGER_MODE = "local"
        server.VENDOR_CREDS = VendorCreds(self.tmp / "vendor_creds.json",
                                          passphrase="pw-http", key_path=self.tmp / "k")
        server._fetch_quota = lambda channel: quota.normalize(
            channel, source="live", used=4, limit=10, unit="requests",
            note="登录当场抓取")
        server._QUOTA_CACHE.clear()
        server._OFFICIAL_LEDGER["ledger"] = None
        server.reset_budget_cache()

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _restore(self):
        for key, value in self.old.items():
            setattr(server, key, value)
        server._QUOTA_CACHE.clear()
        server._OFFICIAL_LEDGER["ledger"] = None
        server.reset_budget_cache()

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:      # noqa: F821
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_login_stores_and_syncs_immediately(self):
        status, doc = self._post("/api/official/vendor-login", {
            "channel": "opencode", "username": "neo", "token": SECRET})
        self.assertEqual(status, 200, doc)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["quota"]["source"], "live")
        self.assertEqual(doc["quota_label"], "LIVE")
        self.assertEqual(doc["quota"]["used_pct"], 40.0)
        # 本地账本 1 笔 vs 官方 4 次（同为 requests 口径）→ 必须出真差额
        reconcile = doc["reconcile"]
        self.assertEqual(reconcile["local_requests"], 1)
        self.assertEqual(reconcile["official_used"], 4)
        self.assertTrue(reconcile["comparable"])
        self.assertEqual(reconcile["delta_requests"], 3)
        self.assertEqual(reconcile["verdict"], "official_ahead")
        # 凭据落盘且无明文
        raw = (self.tmp / "vendor_creds.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(SECRET, json.dumps(doc), "响应体只能回掩码")
        # live 额度同时落成 manual 快照
        snap = quota.load_snapshots()
        self.assertIn("opencode", snap)

    def test_login_requires_some_credential(self):
        status, doc = self._post("/api/official/vendor-login", {"channel": "opencode"})
        self.assertEqual(status, 400, doc)
        self.assertIn("至少提供", json.dumps(doc, ensure_ascii=False))

    def test_browser_probe_unavailable_is_reported(self):
        original = server.read_cookies

        def boom(host):
            raise ProfileUnavailable("没找到 Firefox profile")

        server.read_cookies = boom
        try:
            status, doc = self._post("/api/official/vendor-login",
                                     {"channel": "workbuddy", "from_browser": True})
        finally:
            server.read_cookies = original
        self.assertEqual(status, 200, doc)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["code"], "profile_unavailable")

    def test_browser_probe_host_falls_back_to_channel_name(self):
        """映射表只收「渠道名 ≠ 域名」的；其余由 `or channel` 兜底。

        这条判据锁住的是**约定**而不是缺陷：`host_contains` 全程按**域名子串**匹配
        （`_read_firefox_cookies` 的 `LIKE '%<host>%'`），所以 `ollama`→ollama.com、
        `opencode`→opencode.ai 天然命中，**不必**逐个补进 `VENDOR_COOKIE_HOSTS`。
        将来若有人"顺手补全"这张表，本测试仍然绿，但约定不再成立 ⇒ 故断言的是
        **实际传给探针的 host**，不是表的长度。
        """
        seen = []
        original = server.read_cookies

        def spy(host):
            seen.append(host)
            raise ProfileUnavailable("停在这里：只验 host 取值")

        server.read_cookies = spy
        try:
            for channel in ("ollama", "opencode"):
                self._post("/api/official/vendor-login",
                           {"channel": channel, "from_browser": True})
        finally:
            server.read_cookies = original
        # 渠道名即域名子串：兜底后必须仍能命中真实域名
        self.assertEqual(seen, ["ollama", "opencode"])
        for host, real in (("ollama", "ollama.com"), ("opencode", "opencode.ai")):
            self.assertIn(host, real, "%s 必须能子串命中 %s" % (host, real))

    def test_channels_overview_has_both_quota_axes(self):
        self._post("/api/official/vendor-login",
                   {"channel": "opencode", "token": SECRET})
        req = urllib.request.Request(self.base + "/api/channels/overview")
        with self.opener.open(req, timeout=10) as resp:
            doc = json.loads(resp.read().decode())
        self.assertTrue(doc["ok"])
        row = next((c for c in doc["channels"] if c["channel"] == "opencode"), None)
        self.assertIsNotNone(row, doc)
        self.assertEqual(row["official"]["source"], "live")
        self.assertIn("gateway", row, "本地闸门条必须独立给出")
        self.assertEqual(row["official_source"], "live")
        # 两条轴各自有值，绝不能混成一个百分比
        self.assertIsNotNone(row["official"]["used_pct"])
        self.assertEqual(row["gateway"]["cap_cny"], 10.0)
        self.assertIsNotNone(row["gateway"]["used_pct"])
        self.assertEqual(doc["summary"]["official_sources"].get("live"), 1)


class _FakeRouter:
    """只有一张空路由表：让读模型聚合能跑，又不碰生产 model_upstreams.json。"""

    def summary(self):
        return {"default_channel": "opencode", "channels": [{
            "channel": "opencode", "base_url": "http://127.0.0.1:9/v1",
            "pricing_channel": "opencode", "endpoint": "/v1/chat/completions",
            "decision": False, "models": ["glm-5.3"], "token_present": True,
            "headers": {"x-opencode-token": "●●●●●●"},
            "header_names": ["x-opencode-token"],
        }]}

    def channel(self, name):
        return {"base_url": "http://127.0.0.1:9/v1", "token": "", "headers": {},
                "models": ["glm-5.3"]}


if __name__ == "__main__":
    unittest.main()
