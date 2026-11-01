#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_price_sync.py —— 官方价目同步：只认官方端点，不推算、不补 0。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core import price_sync  # noqa: E402

OFFICIAL = {"data": [
    {"model": "deepseek-v4-flash",
     "pricing": {"unit": "1m_tokens", "currency": "USD",
                 "input": 0.44, "output": 1.32, "cacheRead": 0.014}},
    {"model": "claude-haiku-5.5",
     "pricing": {"unit": "1m_tokens", "currency": "USD", "input": 0.1,
                 "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125,
                 "longContext": {"inputTokensAbove": 100000, "input": 0.5}}},
    {"model": "no-price-model", "pricing": {"unit": "1m_tokens"}},
]}


class _Headers:
    def __init__(self, set_cookies=()):
        self._set_cookies = list(set_cookies)

    def get_all(self, name):
        return list(self._set_cookies) if str(name).lower() == "set-cookie" else None


class _Resp:
    def __init__(self, doc, status=200, set_cookies=()):
        self._body = json.dumps(doc).encode("utf-8")
        self.status = status
        self.headers = _Headers(set_cookies)

    def read(self, _n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class TestParse(unittest.TestCase):
    def test_maps_official_fields(self):
        parsed = price_sync.parse_payload(OFFICIAL)
        self.assertEqual(len(parsed["rows"]), 2)      # 无价目行不进表
        self.assertEqual(parsed["skipped"], ["no-price-model"])
        row = {r["model"]: r for r in parsed["rows"]}["deepseek-v4-flash"]
        self.assertEqual((row["in"], row["cache"], row["out"]), (0.44, 0.014, 1.32))
        long_row = {r["model"]: r for r in parsed["rows"]}["claude-haiku-5.5"]
        self.assertTrue(long_row["long_context"])
        self.assertEqual(long_row["cache_write"], 0.125)

    def test_missing_cache_is_not_filled_with_zero(self):
        doc = {"data": [{"model": "m", "pricing": {"input": 1, "output": 2}}]}
        parsed = price_sync.parse_payload(doc)
        self.assertNotIn("cache", parsed["rows"][0])

    def test_origin_strips_path(self):
        self.assertEqual(price_sync.origin_of("https://host.example/v1"),
                         "https://host.example")
        self.assertEqual(price_sync.origin_of("nope"), "")


class TestFetch(unittest.TestCase):
    def test_tries_mapped_price_host(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(req.full_url)
            if "amr-api" in req.full_url:
                return _Resp(OFFICIAL)
            raise IOError("404")

        found = price_sync.fetch_official("https://models.example.invalid/v1",
                                          opener=opener)
        self.assertTrue(found["ok"])
        self.assertIn("amr-api.open-design.ai", found["source_url"])
        self.assertEqual(len(seen), 2)

    def test_all_endpoints_down_returns_no_rows(self):
        def opener(req, timeout=None):
            raise IOError("boom")

        found = price_sync.fetch_official("https://host.example/v1", opener=opener)
        self.assertFalse(found["ok"])
        self.assertEqual(found["rows"], [])
        self.assertTrue(found["error"])

    def test_sends_identifiable_user_agent(self):
        seen = {}

        def opener(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return _Resp(OFFICIAL)

        price_sync.fetch_official("https://host.example/v1", opener=opener)
        self.assertEqual(seen["ua"], price_sync.USER_AGENT)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pricing.json"
        self.path.write_text(json.dumps({
            "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {"open-design": {"models": {
                "legacy-model": {"in": 9.0, "cache": 0.9, "out": 9.0},
            }}},
        }, ensure_ascii=False), encoding="utf-8")

    def test_writes_provenance_and_keeps_unlisted(self):
        rows = price_sync.parse_payload(OFFICIAL)["rows"]
        res = price_sync.apply_prices(self.path, "open-design", rows,
                                      "https://api.example/model-prices",
                                      checked_at="2026-10-26")
        self.assertEqual(res["written"], 2)
        self.assertEqual(res["kept"], ["legacy-model"])
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        block = doc["upstreams"]["open-design"]["models"]
        self.assertIn("legacy-model", block)          # 未在官方表的旧行原样保留
        row = block["deepseek-v4-flash"]
        self.assertEqual(row["source_url"], "https://api.example/model-prices")
        self.assertEqual(row["checked_at"], "2026-10-26")
        self.assertTrue(row["source"].startswith("上游公开价目端点"))
        self.assertIn(row["in"], (0.44,))

    def test_other_channels_untouched(self):
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["upstreams"]["ollama"] = {"models": {"glm-5.3": {"in": 1.4}}}
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        price_sync.apply_prices(self.path, "open-design",
                                price_sync.parse_payload(OFFICIAL)["rows"],
                                "https://api.example/model-prices")
        out = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(out["upstreams"]["ollama"]["models"]["glm-5.3"]["in"], 1.4)
        self.assertEqual(out["usd_cny"], 6.72)

    def test_official_block_does_not_write_on_failure(self):
        def opener(req, timeout=None):
            raise IOError("down")

        before = self.path.read_text(encoding="utf-8")
        res = price_sync.official_block("https://host.example/v1", "open-design",
                                        self.path, opener=opener)
        self.assertFalse(res["ok"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_empty_channel_rejected(self):
        with self.assertRaises(ValueError):
            price_sync.apply_prices(self.path, " ", [], "https://x")


class TestApplySchemaSafety(unittest.TestCase):
    """落盘后必须仍能通过价目表校验：写坏一个字段会连带整表报废。"""

    def _tmp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pricing.json"
        path.write_text(json.dumps({"unit": "USD_per_mtok", "usd_cny": 6.72,
                                    "upstreams": {}}), encoding="utf-8")
        return path

    def test_billing_kind_is_valid_value(self):
        from core import pricing
        path = self._tmp()
        price_sync.apply_prices(path, "ollama",
                                [{"model": "m1", "in": 1, "out": 2, "cache": 0.1}],
                                "https://x/pricing")
        doc, err = pricing.read_doc(path)
        self.assertEqual(err, "")
        self.assertIn("ollama", doc["upstreams"])
        self.assertIn(doc["upstreams"]["ollama"]["billing_kind"], ("usage", "subscription"))

    def test_missing_cache_falls_back_to_input_price(self):
        from core import pricing
        path = self._tmp()
        price_sync.apply_prices(path, "ollama",
                                [{"model": "no-cache-model", "in": 2, "out": 4}],
                                "https://x/pricing")
        doc, err = pricing.read_doc(path)
        self.assertEqual(err, "")
        row = doc["upstreams"]["ollama"]["models"]["no-cache-model"]
        self.assertEqual(row["cache"], 2)          # 按输入价计，不编 0
        self.assertEqual(row["cache_source"], "in")

    def test_off_peak_fields_land(self):
        path = self._tmp()
        price_sync.apply_prices(path, "ollama",
                                [{"model": "m1", "in": 1, "out": 2, "cache": 0.1,
                                  "off_in": 0.5, "off_cache": 0.05, "off_out": 1}],
                                "https://x/pricing")
        row = json.loads(path.read_text(encoding="utf-8"))["upstreams"]["ollama"]["models"]["m1"]
        self.assertEqual((row["off_in"], row["off_cache"], row["off_out"]), (0.5, 0.05, 1.0))

    def test_one_bad_channel_does_not_blank_table(self):
        """坏渠道只剔自己，好渠道继续可用（回归：payg 一写，全渠道计费变 None）。"""
        from core import pricing
        path = self._tmp()
        doc = {"unit": "USD_per_mtok", "usd_cny": 6.72, "upstreams": {
            "good": {"billing_kind": "usage",
                     "models": {"m1": {"in": 1, "cache": 0.1, "out": 2}}},
            "bad": {"billing_kind": "payg",
                    "models": {"m2": {"in": 1, "cache": 0.1, "out": 2}}},
        }}
        path.write_text(json.dumps(doc), encoding="utf-8")
        loaded, err = pricing.read_doc(path)
        self.assertIn("good", loaded["upstreams"])
        self.assertNotIn("bad", loaded["upstreams"])
        self.assertIn("bad", err)
        self.assertIsNotNone(pricing.rate_of(loaded, "good", "m1"))


class TestAccountSession(unittest.TestCase):
    """登录态取价：只认官方账单 unitPrices，API Key 不能冒充会话。"""

    def test_session_from_creds(self):
        self.assertEqual(
            price_sync.session_from_creds(
                {"cookie": "a=1; __Secure-better-auth.session_token=sess-123; b=2"}),
            "sess-123")
        # API Key 对账单端点只会 401，不当会话用
        self.assertEqual(price_sync.session_from_creds({"token": "sk-amr-abc"}), "")
        self.assertEqual(price_sync.session_from_creds({"session": "raw-session-value"}),
                         "raw-session-value")
        # 面板「登录即同步」只有一个 token 框：整行粘贴 cookie 也要认
        self.assertEqual(
            price_sync.session_from_creds(
                {"token": "__Secure-better-auth.session_token=pasted-abc"}),
            "pasted-abc")
        # 不相干的 cookie 不猜
        self.assertEqual(price_sync.session_from_creds({"cookie": "theme=dark; lang=zh"}), "")
        self.assertEqual(price_sync.session_from_creds({}), "")

    def _opener(self, seen, usage_pages):
        def opener(req, timeout=None):
            seen.append((req.full_url, dict(req.headers)))
            if req.full_url.endswith("/api/v1/workspaces/current"):
                return _Resp({"workspaceId": "ws-42"})
            if "billing/usage" in req.full_url:
                return _Resp(usage_pages.pop(0) if usage_pages else {"entries": []})
            raise AssertionError("unexpected url %s" % req.full_url)
        return opener

    def test_reads_unit_prices_and_takes_latest(self):
        seen = []
        page = {"entries": [
            {"model": "deepseek-v4-flash", "createdAt": "2026-10-20T00:00:00Z",
             "unitPrices": {"input": 0.5, "output": 1.5, "cacheRead": 0.02}},
            {"model": "deepseek-v4-flash", "createdAt": "2026-10-25T00:00:00Z",
             "unitPrices": {"input": 0.44, "output": 1.32, "cacheRead": 0.014}},
            {"model": "no-prices-model", "createdAt": "2026-10-25T00:00:00Z"},
        ], "hasMore": False}
        out = price_sync.fetch_account_prices("sess-1", opener=self._opener(seen, [page]))
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(out["ws"], "ws-42")
        rows = {r["model"]: r for r in out["rows"]}
        self.assertEqual(rows["deepseek-v4-flash"]["in"], 0.44)   # 最新一条胜出
        self.assertNotIn("no-prices-model", rows)
        self.assertIn("no-prices-model", out["skipped"])
        # 登录态必须带会话 Cookie 与 workspace 头
        usage_url, headers = [s for s in seen if "billing/usage" in s[0]][0]
        self.assertIn("__Secure-better-auth.session_token=sess-1", headers.get("Cookie"))
        self.assertEqual(headers.get("X-vela-workspace-id"), "ws-42")

    def test_pages_with_cursor(self):
        seen = []
        first = {"entries": [{"model": "m1", "createdAt": "2026-10-25T00:00:00Z",
                              "unitPrices": {"input": 1, "output": 2}}],
                 "hasMore": True, "nextCursor": "c2"}
        second = {"entries": [{"model": "m2", "createdAt": "2026-10-25T00:00:00Z",
                               "unitPrices": {"input": 3, "output": 4}}],
                  "hasMore": False}
        out = price_sync.fetch_account_prices("sess-1", opener=self._opener(seen, [first, second]))
        self.assertTrue(out["ok"])
        self.assertEqual({r["model"] for r in out["rows"]}, {"m1", "m2"})
        self.assertTrue(any("cursor=c2" in url for url, _ in seen))

    def test_missing_session_is_reported_not_faked(self):
        out = price_sync.fetch_account_prices("")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "no_session")
        self.assertEqual(out["rows"], [])

    def test_expired_session_is_auth_error(self):
        def opener(req, timeout=None):
            return _Resp({"error": "unauthenticated"}, status=401)
        out = price_sync.fetch_account_prices("stale", opener=opener)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "auth_error")

class TestCredentialLogin(unittest.TestCase):
    """账号密码 → 登录态会话：登录即抓价的入口，绝不伪造会话。"""

    def _login_opener(self, seen, status=200, set_cookies=None):
        cookies = (["__Secure-better-auth.session_token=fresh-sess; Path=/; HttpOnly"]
                   if set_cookies is None else set_cookies)

        def opener(req, timeout=None):
            seen.append((req.full_url, dict(req.headers), req.data))
            return _Resp({"user": {"id": "u1"}}, status=status, set_cookies=cookies)
        return opener

    def test_signs_in_and_returns_session(self):
        seen = []
        out = price_sync.login_session(
            "https://models.example.invalid/v1", "me@example.com", "pw-123",
            opener=self._login_opener(seen))
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(out["session"], "fresh-sess")
        url, headers, body = seen[0]
        self.assertIn("/api/auth/sign-in/email", url)
        self.assertEqual(json.loads(body.decode("utf-8"))["email"], "me@example.com")
        self.assertEqual(headers.get("Content-type"), "application/json")

    def test_falls_through_to_mapped_api_host(self):
        """amr-link 只接入，登录端点在 amr-api：要跨过去试，不能停在 404。"""
        seen = []

        def opener(req, timeout=None):
            seen.append(req.full_url)
            if req.full_url.startswith("https://amr-api.open-design.ai/"):
                return _Resp({"user": {}}, status=200,
                             set_cookies=["better-auth.session_token=mapped-sess"])
            return _Resp({"error": "not found"}, status=404)

        out = price_sync.login_session("https://models.example.invalid/v1",
                                       "me@example.com", "pw", opener=opener)
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(out["session"], "mapped-sess")
        self.assertTrue(any("amr-api.open-design.ai" in u for u in seen))

    def test_turnstile_required_is_reported(self):
        def opener(req, timeout=None):
            return _Resp({"code": "TURNSTILE_REQUIRED", "message": "check"}, status=400)

        out = price_sync.login_session("https://models.example.invalid/v1",
                                       "me@example.com", "pw", opener=opener)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "turnstile_required")
        self.assertIn("人机校验", out["error"])

    def test_bad_password_is_auth_error_not_session(self):
        seen = []
        out = price_sync.login_session("https://models.example.invalid/v1",
                                       "me@example.com", "wrong",
                                       opener=self._login_opener(seen, status=401,
                                                                 set_cookies=[]))
        self.assertFalse(out["ok"])
        self.assertEqual(out["session"], "")
        self.assertEqual(out["code"], "auth_error")

    def test_200_without_cookie_is_not_success(self):
        seen = []
        out = price_sync.login_session("https://models.example.invalid/v1",
                                       "me@example.com", "pw",
                                       opener=self._login_opener(seen, set_cookies=[]))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "no_cookie")

    def test_missing_credentials_short_circuits(self):
        out = price_sync.login_session("https://models.example.invalid/v1", "", "")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "no_credentials")

    def test_logged_in_session_then_reads_unit_prices(self):
        """端到端：登录拿会话 → 同一会话直读账单 unitPrices。"""
        login_seen = []
        got = price_sync.login_session("https://models.example.invalid/v1",
                                       "me@example.com", "pw-123",
                                       opener=self._login_opener(login_seen))
        self.assertTrue(got["ok"], got["error"])
        seen = []

        def opener(req, timeout=None):
            seen.append((req.full_url, dict(req.headers)))
            if req.full_url.endswith("/api/v1/workspaces/current"):
                return _Resp({"workspaceId": "ws-9"})
            return _Resp({"entries": [
                {"model": "deepseek-v4-flash", "createdAt": "2026-10-26T00:00:00Z",
                 "unitPrices": {"input": 0.44, "output": 1.32, "cacheRead": 0.014}}],
                "hasMore": False})

        out = price_sync.fetch_account_prices(got["session"], opener=opener)
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual({r["model"] for r in out["rows"]}, {"deepseek-v4-flash"})
        _url, headers = [s for s in seen if "billing/usage" in s[0]][0]
        self.assertIn("__Secure-better-auth.session_token=fresh-sess", headers.get("Cookie"))

    def test_account_rows_carry_account_source_label(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pricing.json"
        path.write_text(json.dumps({"upstreams": {}}), encoding="utf-8")
        res = price_sync.apply_prices(
            path, "open-design", [{"model": "m1", "in": 1, "out": 2, "cache": 0.1}],
            "https://api/billing/usage", source_label="登录态官方账单 unitPrices")
        self.assertEqual(res["written"], 1)
        row = json.loads(path.read_text(encoding="utf-8"))["upstreams"]["open-design"]["models"]["m1"]
        self.assertEqual(row["source"], "登录态官方账单 unitPrices")


if __name__ == "__main__":
    unittest.main()
