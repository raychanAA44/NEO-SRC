# -*- coding: utf-8 -*-
"""KWSA lite 回归：配置、路由、峰谷账本、代理契约、SSE 与控制面。"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "编码区" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.server_runtime import SingleInstanceHTTPServer  # noqa: E402
from kwsa_lite.app import Gateway, Handler, classify_upstream_error  # noqa: E402
from kwsa_lite.audit import AuditLog  # noqa: E402
from kwsa_lite.auth import AuthError, PanelAuth  # noqa: E402
from kwsa_lite.clientkeys import ClientKeyError, ClientKeyStore  # noqa: E402
from kwsa_lite.config import Config, ConfigError  # noqa: E402
from kwsa_lite.ledger import Ledger, PriceBook  # noqa: E402
from kwsa_lite.router import NoActiveMembers, Router, UnknownGroup  # noqa: E402
from kwsa_lite.vault import SecretVault, VaultError, mask_token  # noqa: E402


# ── 数据面 Key / 审计 ───────────────────────────────────────
class TestClientKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keys.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_authenticate_persist_revoke(self):
        store = ClientKeyStore(self.path, default_rps=5, default_burst=10)
        secret, row = store.create("lan-console", 2, 4)
        self.assertTrue(secret.startswith("kwsl_"))
        self.assertEqual(row["name"], "lan-console")
        self.assertEqual(row["rps"], 2)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertIn("hash", raw)
        self.assertIn(row["id"], raw)

        fresh = ClientKeyStore(self.path)
        found = fresh.authenticate(secret)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], row["id"])
        self.assertIsNone(fresh.authenticate(secret + "x"))
        self.assertIsNone(fresh.authenticate("too-short"))
        snap = fresh.snapshot()
        self.assertNotIn("hash", snap[0])
        self.assertEqual(fresh.active_count(), 1)

        removed = fresh.revoke(row["id"])
        self.assertEqual(removed["id"], row["id"])
        self.assertIsNone(fresh.authenticate(secret))
        self.assertEqual(fresh.active_count(), 0)

    def test_duplicate_toggle_and_per_key_bucket(self):
        store = ClientKeyStore(self.path)
        secret, row = store.create("one", 1, 1)
        with self.assertRaises(ClientKeyError):
            store.create("ONE", 1, 1)
        with self.assertRaises(ClientKeyError):
            store.create("bad\x00name", 1, 1)
        key = store.authenticate(secret)
        self.assertTrue(store.allow(key)[0])
        allowed, retry = store.allow(key)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)

        off = store.set_enabled(row["id"], False)
        self.assertFalse(off["enabled"])
        self.assertIsNone(store.authenticate(secret))
        store.set_enabled(row["id"], True)
        self.assertIsNotNone(store.authenticate(secret))

    def test_external_file_change_is_reloaded(self):
        writer = ClientKeyStore(self.path)
        reader = ClientKeyStore(self.path)
        secret, _ = writer.create("later")
        self.assertIsNotNone(reader.authenticate(secret))
        second, row = writer.create("second")
        self.assertIsNotNone(reader.authenticate(second))
        self.assertEqual(len(reader.snapshot()), 2)
        writer.revoke(row["id"])
        self.assertIsNone(reader.authenticate(second))


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_tail_and_filter(self):
        log = AuditLog(self.path)
        self.assertTrue(log.record("login_failed", username="admin",
                                   ip="192.0.2.9", peer_ip="127.0.0.1",
                                   detail="bad_credentials"))
        self.assertTrue(log.record("password_changed", username="admin",
                                   ip="192.0.2.9", detail="sessions_revoked=1"))
        rows = log.tail(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "password_changed")
        self.assertEqual(rows[1]["event"], "login_failed")
        only = log.tail(limit=10, event="login_failed")
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["ip"], "192.0.2.9")
        self.assertEqual(log.tail(limit=10, event="not_an_event"), [])

    def test_missing_file_and_unknown_event(self):
        log = AuditLog(self.path)
        self.assertEqual(log.tail(), [])
        self.assertFalse(log.record("Bad Event"))
        self.assertTrue(log.record("password_changed"))
        self.assertEqual(len(log.tail()), 1)


# ── 假上游 ──────────────────────────────────────────────────
class UpstreamState:
    def __init__(self):
        self.requests = []
        self.mode = "json"
        self.status = 200
        self.body = {}
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            self.requests = []
            self.mode = "json"
            self.status = 200
            self.body = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "m1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: UpstreamState = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        payload = json.dumps({"ok": True, "data": [{"id": "probe-m1"},
                                                       {"id": "probe-m2"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        with self.state.lock:
            self.state.requests.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw,
            })
        if self.state.mode == "sse":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            frames = [
                b'data: {"id":"s1","model":"m1","choices":[{"delta":{"content":"hi"}}]}\n\n',
                b'data: {"id":"s1","model":"m1","usage":{"prompt_tokens":10,'
                b'"completion_tokens":5,"total_tokens":15}}\n\n',
                b"data: [DONE]\n\n",
            ]
            for frame in frames:
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        payload = json.dumps(self.state.body).encode("utf-8")
        self.send_response(self.state.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()


def _write_config(directory: Path, upstream: str, ledger: Path, **overrides) -> Path:
    doc = {
        "schema": 1,
        "server": {"host": "127.0.0.1", "port": 8798, "max_body_bytes": 1024 * 1024,
                   "timeout_s": 5},
        "auth": {"enabled": False, "username": "admin", "session_ttl_s": 3600,
                 "cookie_secure": False, "login_max_failures": 5,
                 "login_window_s": 300, "trust_proxy": False},
        "default_channel": "ch",
        "channels": {
            "ch": {
                "base_url": upstream + "/v1",
                "token_env": "KWSA_TEST_TOKEN",
                "models": ["m1", "m2"],
                "aliases": {"fast": "m1"},
                "probe_path": "/health",
            }
        },
        "groups": {
            "auto/g": {
                "name": "测试组",
                "policy": {"task_lock": True, "smart_tier": True},
                "members": [
                    {"channel": "ch", "id": "m1", "tier": "strong", "priority": 1},
                    {"channel": "ch", "id": "m2", "tier": "weak", "priority": 2},
                ],
            }
        },
        "control": {
            "channels": {"ch": {"enabled": True}},
            "models": {},
        },
        "pricing": {
            "unit": "CNY_per_mtok",
            "usd_cny": 1,
            "channels": {"ch": {"m1": {"in": 1.0, "out": 2.0}}},
        },
        "ledger": {"path": str(ledger), "keep_days": 7},
        "rate_limit": {"enabled": False, "rps": 1000, "burst": 1000},
        "data_plane": {"require_key": False,
                       "keys_path": str(directory / "client_keys.json"),
                       "default_rps": 1000, "default_burst": 1000},
        "audit": {"path": str(directory / "audit.jsonl"), "max_bytes": 65536},
        "routing": {"timeout_s": 5, "task_lock_ttl_s": 60},
        "probe": {"enabled": False, "interval_s": 60, "timeout_s": 2},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key].update(value)
        else:
            doc[key] = value
    path = directory / "kwsa_lite.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _lower(headers) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


# ── 密钥仓 / 渠道收录 ─────────────────────────────────────
class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_mask(self):
        vault = SecretVault(self.dir / "creds.json")
        vault.set("ch", "sk-secret-value-1234")
        self.assertEqual(vault.get("ch"), "sk-secret-value-1234")
        self.assertTrue(vault.has("ch"))
        self.assertNotIn("sk-secret-value-1234",
                         (self.dir / "creds.json").read_text(encoding="utf-8"))
        masked = mask_token(vault.get("ch"))
        self.assertTrue(masked.startswith("sk"))
        self.assertTrue(masked.endswith("34"))
        self.assertNotIn("secret-value", masked)
        snapshot = vault.snapshot()
        self.assertTrue(snapshot["ch"]["present"])
        self.assertNotIn("secret-value", snapshot["ch"]["masked"])

    def test_tamper_and_key_swap_raise(self):
        vault = SecretVault(self.dir / "creds.json")
        vault.set("ch", "secret")
        doc = json.loads((self.dir / "creds.json").read_text(encoding="utf-8"))
        doc["items"]["ch"]["ct"] = doc["items"]["ch"]["ct"][:-4] + "AAAA"
        (self.dir / "creds.json").write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(VaultError):
            SecretVault(self.dir / "creds.json").get("ch")

        (self.dir / "creds.key").write_bytes(b"x" * 32)
        with self.assertRaises(VaultError):
            SecretVault(self.dir / "creds.json")


class TestChannelIntake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = _write_config(self.dir, "http://127.0.0.1:9", self.dir / "ledger.db")
        self.env = {"KWSA_TEST_TOKEN": "secret"}
        self.vault = SecretVault(self.dir / "creds.json")
        self.cfg = Config.load(self.path, environ=self.env).attach_vault(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_persists_without_plaintext(self):
        row = self.cfg.upsert_channel({
            "name": "new",
            "base_url": "http://127.0.0.1:9/v1",
            "token": "sk-new-secret-9999",
            "models": ["n1", "n2"],
            "endpoint": "/v1/chat/completions",
            "enabled": False,
        })
        self.assertEqual(row["channel"], "new")
        self.assertEqual(row["token_source"], "vault")
        self.assertTrue(row["token_present"])
        self.assertFalse(self.cfg.channel_enabled("new"))
        self.assertNotIn("sk-new-secret-9999", self.path.read_text(encoding="utf-8"))
        self.assertTrue(self.cfg.delete_channel("new")["token_deleted"])
        self.assertNotIn("new", self.cfg.channels)
        self.assertNotIn("sk-new-secret-9999",
                         (self.dir / "creds.json").read_text(encoding="utf-8"))

    def test_upsert_preserves_unedited_aliases(self):
        self.cfg.upsert_channel({"name": "ch", "base_url": "http://127.0.0.1:9/v1",
                                 "token_env": "KWSA_TEST_TOKEN", "models": ["m1"]})
        self.assertEqual(self.cfg.channels["ch"]["aliases"], {"fast": "m1"})

    def test_upsert_rejects_plaintext_header_and_bad_name(self):
        with self.assertRaises(ConfigError):
            self.cfg.upsert_channel({"name": "bad name", "base_url": "http://x/v1"})
        with self.assertRaises(ConfigError):
            self.cfg.upsert_channel({
                "name": "h", "base_url": "http://x/v1",
                "headers": {"x-api-key": "plain-secret"},
            })

    def test_default_and_forget_token(self):
        self.cfg.upsert_channel({"name": "new", "base_url": "http://x/v1",
                                 "token": "secret-new", "models": ["n1"]})
        self.cfg.set_default_channel("new")
        self.assertEqual(self.cfg.default_channel, "new")
        self.assertTrue(self.cfg.forget_channel_token("new")["token_deleted"])
        self.assertEqual(self.cfg.channels["new"]["token_source"], "none")

    def test_official_login_is_encrypted_and_masked(self):
        row = self.cfg.upsert_channel({
            "name": "new", "base_url": "http://x/v1", "models": ["n1"],
            "login_url": "https://upstream.example/auth/login",
            "usage_path": "/api/usage",
            "login_user": "operator@example.com",
            "login_password": "plain-password-123",
        })
        self.assertTrue(row["login_present"])
        self.assertTrue(row["password_present"])
        self.assertTrue(row["login_user_masked"].startswith("o"))
        self.assertNotIn("plain-password-123", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("plain-password-123",
                         (self.dir / "creds.json").read_text(encoding="utf-8"))
        public = self.cfg.public()["channels"][0]
        self.assertNotIn("login_password", public)
        self.assertNotIn("plain-password-123", json.dumps(public, ensure_ascii=False))
        self.cfg.delete_channel("new")
        self.assertNotIn("login:new", self.vault.names())

    def test_official_login_can_be_cleared(self):
        self.cfg.upsert_channel({
            "name": "new", "base_url": "http://x/v1", "models": ["n1"],
            "login_user": "u", "login_password": "p",
        })
        self.assertTrue(self.cfg.channels["new"]["login_present"])
        self.cfg.upsert_channel({"name": "new", "base_url": "http://x/v1",
                                 "models": ["n1"], "clear_login": True})
        self.assertFalse(self.cfg.channels["new"]["login_present"])


# ── 配置 / 路由 / 账本 ─────────────────────────────────────
class TestConfigRouter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "ledger.db"
        self.env = {"KWSA_TEST_TOKEN": "secret"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_forbidden_port(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["server"]["port"] = 8792
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(path, environ=self.env)

    def test_trust_proxy_requires_loopback(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["auth"].update({"enabled": True, "trust_proxy": True})
        doc["server"]["host"] = "0.0.0.0"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(path, environ=self.env)
        doc["server"]["host"] = "127.0.0.1"
        path.write_text(json.dumps(doc), encoding="utf-8")
        cfg = Config.load(path, environ=self.env)
        self.assertTrue(cfg.auth["trust_proxy"])
        self.assertTrue(cfg.data_plane["require_key"] is False)

    def test_rejects_plaintext_token(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["channels"]["ch"]["token"] = "plain"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(path, environ=self.env)

    def test_public_masks_token(self):
        cfg = Config.load(_write_config(self.dir, "http://127.0.0.1:9", self.ledger),
                          environ=self.env)
        row = cfg.public()["channels"][0]
        self.assertTrue(row["token_present"])
        self.assertNotIn("token", row)
        self.assertNotEqual(row["token_env"], "")

    def test_control_roundtrip_persists(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        cfg = Config.load(path, environ=self.env)
        cfg.set_channel_enabled("ch", False)
        cfg.set_model_enabled("ch", "m1", False)
        cfg.save()
        fresh = Config.load(path, environ=self.env)
        self.assertFalse(fresh.channel_enabled("ch"))
        self.assertFalse(fresh.model_enabled("ch", "m1"))
        self.assertNotIn("budgets", fresh.control)

    def test_unavailable_annotation_roundtrip(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        cfg = Config.load(path, environ=self.env)
        cfg.set_model_enabled("ch", "m1", False, code="upstream_plan",
                              reason="模型不在套餐内",
                              evidence="HTTP 402 model_not_in_plan",
                              fix="升级套餐后复检")
        cfg.save()
        fresh = Config.load(path, environ=self.env)
        state = fresh.model_state("ch", "m1")
        self.assertFalse(state["enabled"])
        self.assertEqual(state["code"], "upstream_plan")
        self.assertEqual(state["label"], "上游计划不含此模型")
        self.assertEqual(state["fix"], "升级套餐后复检")
        self.assertGreater(state["tested_at"], 0)
        # 启用即清空解释，并且不留 {"enabled": true} 垃圾行
        cfg.set_model_enabled("ch", "m1", True)
        cfg.save()
        fresh = Config.load(path, environ=self.env)
        self.assertNotIn("m1", fresh.control["models"].get("ch") or {})
        self.assertTrue(fresh.model_state("ch", "m1")["enabled"])

    def test_channel_annotation_scopes_models(self):
        cfg = Config.load(_write_config(self.dir, "http://127.0.0.1:9", self.ledger),
                          environ=self.env)
        cfg.set_channel_enabled("ch", False, code="upstream_denied",
                                reason="Key 的 Team 策略拒绝",
                                evidence="HTTP 403 MODEL_ACCESS_DENIED")
        state = cfg.model_state("ch", "m2")
        self.assertEqual(state["scope"], "channel")
        self.assertEqual(state["label"], "上游拒绝此 Key 访问")
        # 渠道已停时 annotate_channel 能刷新证据而不改启停
        cfg.annotate_channel("ch", "upstream_denied", "策略仍拒绝")
        self.assertFalse(cfg.channel_enabled("ch"))

    def test_classify_upstream_errors(self):
        cases = [
            (402, '{"error":{"code":"model_not_in_plan","message":"not in plan"}}',
             "upstream_plan"),
            (403, '{"error":{"code":"MODEL_ACCESS_DENIED","message":"denied"}}',
             "upstream_denied"),
            (403, "<html>error code: 1010</html>", "upstream_blocked"),
            (400, '{"error":{"code":"MissingSessionID","message":"Request is missing '
                  'x-opencode-session and cache key"}}', "upstream_gate"),
            (403, '{"error":{"code":"api_error","message":"This model is currently '
                  'being rolled out"}}', "upstream_rollout"),
            (404, '{"error":{"code":"model_not_found"}}', "upstream_missing"),
            (401, "{}", "auth_error"),
            (429, "{}", "rate_limited"),
            (0, "", "network_error"),
        ]
        for status, blob, expected in cases:
            result = classify_upstream_error(status, blob)
            self.assertEqual(result["code"], expected, (status, blob))
            self.assertTrue(result["fix"], "恢复路径不得为空")
        self.assertEqual(classify_upstream_error(402, "")["label"], "上游计划不含此模型")

    def test_router_alias_and_default(self):
        cfg = Config.load(_write_config(self.dir, "http://127.0.0.1:9", self.ledger),
                          environ=self.env)
        router = Router(cfg)
        self.assertEqual(router.resolve("fast").request_model, "m1")
        self.assertEqual(router.resolve("ch/m2").channel, "ch")
        route = router.resolve("unknown")
        self.assertEqual(route.channel, "ch")
        self.assertEqual(route.request_model, "unknown")
        self.assertEqual(router.resolve("custom/ch/m1").request_model, "m1")

    def test_router_plain_model_skips_disabled_channel(self):
        path = _write_config(
            self.dir, "http://127.0.0.1:9", self.ledger,
            channels={"ch2": {"base_url": "http://127.0.0.1:9/v1",
                               "token_env": "KWSA_TEST_TOKEN",
                               "models": ["m1"]}},
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["control"]["models"]["ch2"] = {"m1": False}
        path.write_text(json.dumps(doc), encoding="utf-8")
        router = Router(Config.load(path, environ=self.env))
        self.assertEqual(router.resolve("m1").channel, "ch")
        self.assertEqual(router.resolve("ch2/m1").channel, "ch2")
        doc["control"]["models"]["ch"] = {"m1": False}
        path.write_text(json.dumps(doc), encoding="utf-8")
        router = Router(Config.load(path, environ=self.env))
        self.assertEqual(router.resolve("m1").channel, "ch2")

    def test_group_smart_tier_and_lock(self):
        cfg = Config.load(_write_config(self.dir, "http://127.0.0.1:9", self.ledger),
                          environ=self.env)
        router = Router(cfg)
        first = router.resolve_group("auto/g", task_id="task-1")
        self.assertEqual(first.request_model, "m1")
        self.assertEqual(first.tier, "strong")
        self.assertFalse(first.locked)
        second = router.resolve_group("auto/g", task_id="task-1")
        self.assertTrue(second.locked)
        self.assertEqual(second.request_model, "m1")
        self.assertEqual(len(router.locks.snapshot()), 1)
        self.assertTrue(router.release_lock("task-1", "auto/g"))
        self.assertEqual(router.locks.snapshot(), [])

    def test_group_unknown_and_empty(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        cfg = Config.load(path, environ=self.env)
        router = Router(cfg)
        with self.assertRaises(UnknownGroup):
            router.resolve_group("auto/nope")
        doc = json.loads(path.read_text(encoding="utf-8"))
        for member in doc["groups"]["auto/g"]["members"]:
            member["enabled"] = False
        path.write_text(json.dumps(doc), encoding="utf-8")
        cfg = Config.load(path, environ=self.env)
        with self.assertRaises(NoActiveMembers):
            Router(cfg).resolve_group("auto/g")

    def test_router_decision_alias(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["channels"]["dec"] = {
            "base_url": "http://127.0.0.1:9/v1",
            "models": ["d1"],
            "aliases": {"d": "d1"},
            "endpoint": "/v1/systemone",
            "decision": True,
        }
        path.write_text(json.dumps(doc), encoding="utf-8")
        cfg = Config.load(path, environ=self.env)
        route = Router(cfg).resolve("d")
        self.assertEqual(route.channel, "dec")
        self.assertEqual(route.request_model, "d1")
        self.assertTrue(route.decision)
        self.assertTrue(route.path.endswith("/v1/systemone"))

    def test_config_hot_reload_stamp(self):
        path = _write_config(self.dir, "http://127.0.0.1:9", self.ledger)
        cfg = Config.load(path, environ=self.env)
        self.assertFalse(cfg.changed_on_disk())
        time.sleep(0.01)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["control"]["channels"]["ch"]["enabled"] = False
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertTrue(cfg.changed_on_disk())
        cfg.reload()
        self.assertFalse(cfg.channel_enabled("ch"))


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db", {
            "unit": "CNY_per_mtok",
            "usd_cny": 1,
            "channels": {"ch": {"m1": {"in": 1.0, "cache": 0.5, "out": 2.0}}},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_and_unpriced(self):
        row = self.ledger.record(model="m1", channel="ch", prompt=100, completion=50,
                                 ok=True, status=200, rid="r1")
        self.assertAlmostEqual(row["cost_cny"], 0.0002, places=6)
        self.assertEqual(row["total_tokens"], 150)
        missing = self.ledger.record(model="m2", channel="ch", prompt=1, completion=1,
                                     ok=True, status=200)
        self.assertIsNone(missing["cost_cny"])
        self.assertEqual(missing["cost_reason"], "价目未配置")

    def test_failure_not_billed(self):
        row = self.ledger.record(model="m1", channel="ch", prompt=100, completion=50,
                                 ok=False, status=500)
        self.assertIsNone(row["cost_cny"])
        self.assertEqual(row["cost_reason"], "失败不计费")

    def test_cached_tokens_billed_once(self):
        row = self.ledger.record(model="m1", channel="ch", prompt=1_000_000,
                                 cached=400_000, completion=0, ok=True, status=200)
        # 600k 未命中按 in=1，400k 命中按 cache=0.5，不把命中量再按 in 计一次。
        self.assertAlmostEqual(row["cost_cny"], 0.8, places=6)

    def test_usd_conversion(self):
        ledger = Ledger(Path(self.tmp.name) / "usd.db", {
            "unit": "USD_per_mtok", "usd_cny": 7.0,
            "channels": {"ch": {"m1": {"in": 1.0, "out": 2.0}}},
        })
        row = ledger.record(model="m1", channel="ch", prompt=1_000_000,
                            completion=0, ok=True, status=200)
        self.assertAlmostEqual(row["cost_cny"], 7.0, places=6)

    def test_spend_and_summary(self):
        self.ledger.record(model="m1", channel="ch", prompt=1_000_000, completion=0,
                           ok=True, status=200)
        self.ledger.record(model="m2", channel="ch", prompt=10, completion=10,
                           ok=True, status=200)
        self.ledger.record(model="m1", channel="ch", prompt=1, completion=1,
                           ok=False, status=500)
        self.assertAlmostEqual(self.ledger.spend_today(), 1.0, places=6)
        self.assertAlmostEqual(self.ledger.spend_today("ch"), 1.0, places=6)
        summary = self.ledger.summary_today()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["unpriced"], 1)
        self.assertEqual(summary["by_channel"][0]["channel"], "ch")

    def test_recent_limit_and_stream_usage(self):
        self.ledger.record_from_payload(
            b'data: {"model":"m1","usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            b"data: [DONE]\n\n",
            request_model="m1", status=200, ok=True, rid="s1", channel="ch",
        )
        rows = self.ledger.recent(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prompt_tokens"], 7)
        self.assertEqual(rows[0]["completion_tokens"], 3)
        self.assertEqual(rows[0]["stream"], 1)
        self.assertEqual(rows[0]["rid"], "s1")

    def test_prune(self):
        old = time.time() - 9 * 86400
        self.ledger.record(model="m1", channel="ch", prompt=1, completion=0,
                           ok=True, status=200, at=old)
        self.assertGreaterEqual(self.ledger.prune(force=True), 1)
        self.assertEqual(self.ledger.health()["rows"], 0)

    def test_pricebook_cache_fallback(self):
        book = PriceBook({"unit": "CNY_per_mtok", "usd_cny": 1,
                          "channels": {"ch": {"m1": {"in": 2.0, "out": 4.0}}}})
        cost, reason = book.compute("m1", 1_000_000, 0, 0, channel="ch")
        self.assertAlmostEqual(cost, 2.0, places=6)
        self.assertEqual(reason, "")

    def test_peak_and_valley_are_priced_and_recorded(self):
        ledger = Ledger(Path(self.tmp.name) / "peak.db", {
            "unit": "CNY_per_mtok", "usd_cny": 1,
            "channels": {"ch": {
                "peak_rule": {"kind": "utc_weekday_window", "start_hour": 12, "end_hour": 18},
                "models": {"m1": {
                    "in": 1.0, "cache": 0.5, "out": 2.0,
                    "off_in": 0.5, "off_cache": 0.25, "off_out": 1.0,
                }},
            }},
        })
        peak_at = datetime.datetime(2026, 10, 26, 13, 0,
                                    tzinfo=datetime.timezone.utc).timestamp()
        off_at = datetime.datetime(2026, 10, 26, 20, 0,
                                   tzinfo=datetime.timezone.utc).timestamp()
        peak = ledger.record(model="m1", channel="ch", prompt=1_000_000,
                             completion=0, ok=True, status=200, at=peak_at)
        off = ledger.record(model="m1", channel="ch", prompt=1_000_000,
                            completion=0, ok=True, status=200, at=off_at)
        self.assertEqual(peak["price_period"], "peak")
        self.assertEqual(off["price_period"], "offpeak")
        self.assertAlmostEqual(peak["cost_cny"], 1.0, places=6)
        self.assertAlmostEqual(off["cost_cny"], 0.5, places=6)
        self.assertEqual(peak["price_in"], 1.0)
        self.assertEqual(off["price_in"], 0.5)
        periods = {row["price_period"] for row in ledger.recent(2)}
        self.assertEqual(periods, {"peak", "offpeak"})


# ── HTTP 端到端 ───────────────────────────────────────────
class TestGatewayHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = UpstreamState()
        FakeUpstream.state = cls.state
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.upstream_base = "http://127.0.0.1:%d" % cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        self.state.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "ledger.db"
        self.env = {"KWSA_TEST_TOKEN": "secret"}
        self.gateway = None
        self.httpd = None
        self.panel = None
        self.panel_password = "correct-horse-battery-2026"
        self.cookie = ""

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.gateway:
            self.gateway.stop()
        self.tmp.cleanup()

    def start(self, **overrides):
        path = _write_config(self.dir, self.upstream_base, self.ledger, **overrides)
        cfg = Config.load(path, environ=self.env)
        cfg.attach_vault(SecretVault(self.dir / "creds.json"))
        if cfg.auth["enabled"]:
            self.panel = PanelAuth(
                self.dir / "panel_auth.json",
                username=cfg.auth["username"],
                ttl=cfg.auth["session_ttl_s"],
                max_failures=cfg.auth["login_max_failures"],
                window_s=cfg.auth["login_window_s"],
            )
            self.panel.set_password(self.panel_password)
        self.gateway = Gateway(cfg, auth=self.panel)
        self.httpd = SingleInstanceHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.gateway = self.gateway
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        return cfg

    def request(self, method, path, data=None, headers=None):
        body = json.dumps(data).encode("utf-8") if isinstance(data, dict) else data
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, _lower(res.headers), res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, _lower(exc.headers), exc.read()

    def login(self, password=None, origin=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = self.base
        status, response_headers, body = self.request(
            "POST", "/api/login",
            {"username": "admin", "password": password or self.panel_password},
            headers,
        )
        raw = response_headers.get("set-cookie", "")
        if status == 200 and raw:
            self.cookie = raw.split(";", 1)[0]
        return status, response_headers, body

    def session_request(self, method, path, data=None, headers=None, origin=True):
        merged = dict(headers or {})
        if self.cookie:
            merged.setdefault("Cookie", self.cookie)
        if origin:
            merged.setdefault("Origin", self.base)
        return self.request(method, path, data, merged)

    def test_health_and_metrics(self):
        self.start()
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["service"], "kwsa-lite")
        self.assertEqual(data["default_channel"], "ch")
        self.assertTrue(headers.get("x-request-id"))
        self.assertEqual(len(data["contract"]), 8)
        self.assertFalse(data["probe"]["enabled"])
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        status, headers, body = self.request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn(b"kwsa_lite_up 1", body)
        self.assertIn("text/plain", headers["content-type"])

    def test_catalog_prices_and_route_check(self):
        self.start()
        status, _, body = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertGreaterEqual(len(data["models"]), 2)
        self.assertTrue(data["models"][0]["price"]["configured"])
        self.assertIn("routes", data)
        self.assertIn("error_count", data["routes"])
        status, _, body = self.request("GET", "/api/routes")
        self.assertEqual(status, 200)
        self.assertIn("check", json.loads(body))

    def test_model_probe_records_reason_then_clears_on_success(self):
        self.start()
        self.gateway.config.set_model_enabled("ch", "m1", False)
        self.state.status = 402
        self.state.body = {"error": {"code": "model_not_in_plan",
                                    "message": "model not in plan"}}
        status, _, body = self.session_request(
            "POST", "/api/control/model/probe", {"channel": "ch", "model": "m1"},
            {"X-KWSA-UI": "1"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "upstream_plan")
        self.assertEqual(result["status"], 402)
        status, _, body = self.request("GET", "/api/catalog")
        data = json.loads(body)
        row = next(m for m in data["models"] if m["id"] == "m1")
        self.assertEqual(row["unavailable"]["code"], "upstream_plan")
        self.assertIn("402", row["unavailable"]["evidence"])
        self.assertTrue(row["unavailable"]["fix"])
        unavailable = data["unavailable"]
        self.assertEqual(unavailable["total_off"], 1)
        self.assertEqual(unavailable["total_models"], 2)
        self.assertEqual(unavailable["groups"][0]["channel"], "ch")
        self.assertEqual(unavailable["groups"][0]["count"], 1)
        # 上游恢复后复检：证据改成「复检已通过」，但不自动启用
        self.state.status = 200
        self.state.body = {"id": "x", "object": "chat.completion",
                           "choices": [{"index": 0,
                                         "message": {"role": "assistant", "content": "ok"}}]}
        status, _, body = self.session_request(
            "POST", "/api/control/model/probe", {"channel": "ch", "model": "m1"},
            {"X-KWSA-UI": "1"})
        result = json.loads(body)
        self.assertTrue(result["ok"])
        status, _, body = self.request("GET", "/api/catalog")
        row = next(m for m in json.loads(body)["models"] if m["id"] == "m1")
        self.assertEqual(row["unavailable"]["code"], "probe_ok")
        self.assertFalse(row["enabled"])

    def test_model_probe_rejects_unlisted_model(self):
        self.start()
        status, _, body = self.session_request(
            "POST", "/api/control/model/probe", {"channel": "ch", "model": "nope"},
            {"X-KWSA-UI": "1"})
        self.assertEqual(status, 400)
        self.assertIn("未收录", json.loads(body)["error"]["message"])

    def test_official_sources_configured_without_outbound_call(self):
        self.start()
        status, _, body = self.request("GET", "/api/official/sources")
        self.assertEqual(status, 200)
        before = json.loads(body)
        self.assertFalse(before["connected"])
        self.assertEqual(before["configured"], 0)
        status, _, body = self.request("GET", "/api/official/usage")
        usage = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(usage["connected"])
        self.assertEqual(usage["source"], "none")
        self.assertTrue(all(r["source"] == "none" for r in usage["rows"]))
        sent = len(self.state.requests)
        status, _, body = self.request(
            "POST", "/api/control/channel",
            {"action": "save", "name": "official-smoke",
             "base_url": self.upstream_base + "/v1", "models": ["om"],
             "login_url": "https://upstream.example/auth/login",
             "usage_path": "/api/usage",
             "login_user": "operator@example.com",
             "login_password": "plain-password-123"},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", "/api/official/sources")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["configured"], 1)
        self.assertFalse(data["connected"])
        self.assertEqual(data["source"], "none")
        self.assertEqual(len(self.state.requests), sent, "官方接口预检不得出站")
        self.assertNotIn("plain-password-123",
                         self.gateway.config.path.read_text(encoding="utf-8"))
        self.request("POST", "/api/control/channel",
                     {"action": "delete", "name": "official-smoke"},
                     {"X-KWSA-UI": "1"})

    def test_forward_headers_and_request_id(self):
        self.start()
        status, headers, body = self.request("POST", "/v1/chat/completions", {
            "model": "m1", "messages": [{"role": "user", "content": "hi"}],
        }, {"traceparent": "00-abc-def-01", "x-request-id": "rid-fixed"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-request-id"), "rid-fixed")
        self.assertEqual(headers.get("x-kwsa-channel"), "ch")
        self.assertEqual(headers.get("x-kwsa-model"), "m1")
        sent = self.state.requests[-1]
        self.assertEqual(sent["headers"]["x-request-id"], "rid-fixed")
        self.assertEqual(sent["headers"]["traceparent"], "00-abc-def-01")
        self.assertEqual(sent["headers"]["x-real-ip"], "127.0.0.1")
        self.assertIn("127.0.0.1", sent["headers"]["x-forwarded-for"])
        self.assertEqual(sent["headers"]["x-forwarded-proto"], "http")
        self.assertEqual(sent["headers"]["authorization"], "Bearer secret")
        self.assertIn("127.0.0.1", sent["headers"]["host"])
        self.assertEqual(sent["headers"]["x-forwarded-host"],
                         self.base.replace("http://", ""))

    def test_access_log_has_seven_required_fields(self):
        import contextlib
        import io

        self.start()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            status, headers, _ = self.request("POST", "/v1/chat/completions", {"model": "m1"})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("x-request-id"))
        lines = [line for line in buf.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(lines)
        entry = json.loads(lines[-1])
        for key in ("timestamp", "req_id", "client_ip", "status", "upstream_status",
                    "latency_ms", "upstream_latency_ms"):
            self.assertIn(key, entry)
        self.assertEqual(entry["req_id"], headers.get("x-request-id"))
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["upstream_status"], 200)

    def test_ledger_records_usage_and_cost(self):
        self.start()
        self.request("POST", "/v1/chat/completions", {"model": "m1"})
        row = self.gateway.ledger.recent(1)[0]
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["completion_tokens"], 50)
        self.assertAlmostEqual(row["cost_cny"], 0.0002, places=6)
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["channel"], "ch")
        self.assertIsNone(row["ttft_ms"], "非流式没有首块时延，不能拿总耗时冒充 TTFT")

    def test_alias_rewrites_model(self):
        self.start()
        self.request("POST", "/v1/chat/completions", {"model": "fast"})
        sent = self.state.requests[-1]
        self.assertEqual(json.loads(sent["body"].decode())["model"], "m1")

    def test_group_rewrite_and_sticky_lock(self):
        self.start()
        status, headers, _ = self.request(
            "POST", "/v1/chat/completions", {"model": "auto/g"},
            {"x-hub-task-id": "t1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-kwsa-group"), "auto/g")
        self.assertEqual(headers.get("x-kwsa-locked"), "0")
        self.assertEqual(json.loads(self.state.requests[-1]["body"].decode())["model"], "m1")
        status, headers, _ = self.request(
            "POST", "/v1/chat/completions", {"model": "auto/g"},
            {"x-hub-task-id": "t1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-kwsa-locked"), "1")
        self.assertEqual(self.gateway.router.locks.snapshot()[0]["task_id"], "t1")

    def test_unknown_group_and_model_control(self):
        self.start()
        status, _, body = self.request("POST", "/v1/chat/completions", {"model": "auto/none"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "unknown_group")

        self.gateway.config.set_model_enabled("ch", "m1", False)
        status, _, body = self.request("POST", "/v1/chat/completions", {"model": "m1"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "model_disabled")

    def test_channel_disabled(self):
        self.start()
        self.gateway.config.set_channel_enabled("ch", False)
        status, _, body = self.request("POST", "/v1/chat/completions", {"model": "m1"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "channel_disabled")

    def test_budget_gate_is_removed(self):
        self.start()
        self.gateway.ledger.record(model="m1", channel="ch", prompt=1_000_000,
                                   completion=0, ok=True, status=200)
        before = len(self.state.requests)
        status, _, _ = self.request("POST", "/v1/chat/completions", {"model": "m1"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.state.requests), before + 1)
        self.assertEqual(self.request("GET", "/api/budget")[0], 404)
        status, _, body = self.request(
            "POST", "/api/control/budget", {"scope": "global", "amount": 0.01},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_sse_stream_and_usage(self):
        self.state.mode = "sse"
        self.start()
        status, headers, body = self.request("POST", "/v1/chat/completions", {
            "model": "m1", "stream": True,
        })
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertEqual(headers.get("transfer-encoding"), "chunked")
        self.assertIn(b"[DONE]", body)
        deadline = time.time() + 2
        row = None
        while time.time() < deadline and row is None:
            rows = self.gateway.ledger.recent(1)
            if rows:
                row = rows[0]
            else:
                time.sleep(0.02)
        self.assertIsNotNone(row, "SSE 账本行未落库")
        self.assertEqual(row["prompt_tokens"], 10)
        self.assertEqual(row["completion_tokens"], 5)
        self.assertEqual(row["stream"], 1)
        self.assertIsNotNone(row["ttft_ms"])

    def test_sensitive_paths_and_unknown_api(self):
        self.start()
        for path in ("/api/admin/x", "/api/actuator/x", "/api/vault",
                     "/admin/x", "/actuator/x", "/admin", "/api/admin"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 403, path)
        status, headers, _ = self.request("HEAD", "/api/admin")
        self.assertEqual(status, 403)
        self.assertTrue(headers.get("x-request-id"))
        status, _, body = self.request("POST", "/api/budgets", {})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "write_not_allowed")
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)

    def test_rate_limit_and_health_exemption(self):
        self.start(rate_limit={"enabled": True, "rps": 1, "burst": 1})
        first = self.request("GET", "/api/status")[0]
        second = self.request("GET", "/api/status")[0]
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        for _ in range(4):
            self.assertEqual(self.request("GET", "/health")[0], 200)

    def test_channel_intake_probe_save_route_delete(self):
        self.start()
        status, _, body = self.request(
            "POST", "/api/control/channel",
            {"action": "probe", "name": "new", "base_url": self.upstream_base + "/v1"},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        probe = json.loads(body)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["models"], ["probe-m1", "probe-m2"])

        status, _, body = self.request(
            "POST", "/api/control/channel",
            {"action": "save", "name": "new", "base_url": self.upstream_base + "/v1",
             "token": "sk-intake-123456", "models": ["new-m1"], "enabled": True},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["token_source"], "vault")
        self.assertNotIn("sk-intake-123456", self.gateway.config.path.read_text(encoding="utf-8"))

        status, headers, _ = self.request(
            "POST", "/v1/chat/completions", {"model": "new-m1"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-kwsa-channel"), "new")
        self.assertEqual(self.state.requests[-1]["headers"]["authorization"],
                         "Bearer sk-intake-123456")

        status, _, body = self.request(
            "POST", "/api/control/channel", {"action": "delete", "name": "new"},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["deleted"])
        self.assertNotIn("new", self.gateway.config.channels)

    def test_control_requires_ui_header_and_persists(self):
        cfg = self.start()
        status, _, body = self.request("POST", "/api/control/channel",
                                       {"name": "ch", "enabled": False})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "csrf_guard")
        status, _, _ = self.request(
            "POST", "/api/control/channel", {"name": "ch", "enabled": False},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        fresh = Config.load(cfg.path, environ=self.env)
        self.assertFalse(fresh.channel_enabled("ch"))
        status, _, _ = self.request("POST", "/v1/chat/completions", {"model": "m1"})
        self.assertEqual(status, 403)

    def test_ui_and_head(self):
        self.start()
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"KWSA LITE", body)
        self.assertIn(b"/api/status", body)
        self.assertTrue(headers.get("x-request-id"), "静态页也应回填 X-Request-Id")
        status, headers, body = self.request("HEAD", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_panel_auth_gate_and_login(self):
        self.start(auth={"enabled": True})
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "login_required")
        self.assertEqual(self.request("GET", "/v1/models")[0], 200,
                         "/v1/* 数据面不跟随面板会话")

        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="loginForm"', body)
        self.assertNotIn(b"/api/status", body)

        status, _, body = self.request("GET", "/api/session")
        session = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(session["authenticated"])
        self.assertTrue(session["auth_enabled"])

        status, headers, _ = self.login("wrong-password")
        self.assertEqual(status, 401)
        self.assertNotIn("set-cookie", headers)

        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        cookie = headers.get("set-cookie", "")
        self.assertIn("kwsa_lite_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        status, _, body = self.session_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["auth"]["enabled"])
        status, _, body = self.session_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/status", body)

    def test_panel_control_requires_session_origin_and_ui_header(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        status, _, body = self.session_request(
            "POST", "/api/control/channel", {"name": "ch", "enabled": False},
            {"X-KWSA-UI": "1"}, origin=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "csrf_origin")
        status, _, body = self.session_request(
            "POST", "/api/control/channel", {"name": "ch", "enabled": False},
            origin=True,
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "csrf_guard")
        status, _, _ = self.session_request(
            "POST", "/api/control/channel", {"name": "ch", "enabled": False},
            {"X-KWSA-UI": "1"}, origin=True,
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.gateway.config.channel_enabled("ch"))

    def test_panel_logout_revokes_session(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        status, headers, _ = self.session_request(
            "POST", "/api/logout", {}, {"X-KWSA-UI": "1"}, origin=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("set-cookie", ""))
        status, _, body = self.session_request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "login_required")

    def test_login_throttle_uses_peer_ip_not_forwarded_header(self):
        self.start(auth={"enabled": True, "login_max_failures": 2,
                         "login_window_s": 30})
        self.assertEqual(self.login("bad-one")[0], 401)
        self.assertEqual(self.login("bad-two")[0], 401)
        status, headers, body = self.request(
            "POST", "/api/login", {"username": "admin", "password": "bad-three"},
            {"Origin": self.base, "Content-Type": "application/json",
             "X-Forwarded-For": "203.0.113.77"},
        )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "login_throttled")
        self.assertGreater(int(headers.get("retry-after", "0")), 0)

    def test_external_password_change_revokes_live_session(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        rotated = PanelAuth(self.dir / "panel_auth.json")
        self.assertTrue(rotated.load())
        rotated.set_password("rotated-panel-password-2026")
        status, _, body = self.session_request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "login_required")
        self.assertEqual(self.login("rotated-panel-password-2026")[0], 200)

    def test_health_does_not_leak_panel_data(self):
        self.start(auth={"enabled": True})
        status, _, body = self.request("GET", "/health")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["panel_auth"], "enabled")
        for key in ("channels", "official", "today", "config_path", "recent",
                    "catalog"):
            self.assertNotIn(key, data)

    def test_non_loopback_requires_auth_and_no_plaintext_password(self):
        path = _write_config(self.dir, self.upstream_base, self.ledger)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["server"]["host"] = "0.0.0.0"
        doc["auth"]["enabled"] = False
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(path, environ=self.env)

        doc["auth"]["enabled"] = True
        path.write_text(json.dumps(doc), encoding="utf-8")
        cfg = Config.load(path, environ=self.env)
        self.assertTrue(cfg.auth["enabled"])

        empty = PanelAuth(self.dir / "no-password.json")
        with self.assertRaises(AuthError):
            empty.ensure_credentials(allow_generate=False)

        self.start(auth={"enabled": True})
        stored = (self.dir / "panel_auth.json").read_text(encoding="utf-8")
        self.assertNotIn(self.panel_password, stored)
        self.assertIn("scrypt$", stored)
        self.panel.set_password("new-panel-password-2026")
        self.assertFalse(self.panel.verify("admin", self.panel_password))
        self.assertTrue(self.panel.verify("admin", "new-panel-password-2026"))
        status, _, body = self.session_request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "login_required")

    def test_data_plane_key_gate_and_per_key_limit(self):
        self.start(data_plane={"require_key": True})
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["code"], "client_key_missing")

        status, _, body = self.request(
            "POST", "/api/security/keys",
            {"action": "create", "name": "limit", "rps": 1, "burst": 1},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 201)
        created = json.loads(body)
        secret = created["secret"]
        key_id = created["key"]["id"]
        self.assertTrue(secret.startswith("kwsl_"))
        self.assertNotIn(secret, self.gateway.audit.path.read_text(encoding="utf-8"))

        auth = {"Authorization": "Bearer " + secret}
        status, _, _ = self.request("GET", "/v1/models", headers=auth)
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", "/v1/models", headers=auth)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "key_rate_limited")
        self.assertGreater(int(headers.get("retry-after", "0")), 0)

        status, header, body = self.request("GET", "/v1/models",
                                            headers={"Authorization": "Bearer bad"})
        self.assertEqual(status, 401)
        self.assertIn("bearer", header.get("www-authenticate", "").lower())

        status, _, body = self.request("GET", "/api/security/keys")
        self.assertEqual(status, 200)
        listed = json.loads(body)
        self.assertEqual(len(listed["keys"]), 1)
        self.assertNotIn(secret, body.decode("utf-8"))

        status, _, _ = self.request(
            "POST", "/api/security/keys",
            {"action": "revoke", "id": key_id}, {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/v1/models")[0], 503)
        events = [row["event"] for row in self.gateway.audit.tail(limit=20)]
        self.assertIn("client_key_created", events)
        self.assertIn("client_key_revoked", events)

    def test_panel_password_change_revokes_sessions_and_audits(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        old_cookie = self.cookie
        new_password = "new-panel-password-2026"
        status, headers, body = self.session_request(
            "POST", "/api/auth/password",
            {"current_password": self.panel_password, "new_password": new_password,
             "confirm_password": new_password},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sessions_revoked"], 1)
        new_cookie = headers.get("set-cookie", "").split(";", 1)[0]
        self.assertTrue(new_cookie.startswith("kwsa_lite_session="))
        self.assertNotEqual(new_cookie, old_cookie)
        self.assertTrue(self.panel.verify("admin", new_password))
        self.assertFalse(self.panel.verify("admin", self.panel_password))

        self.cookie = old_cookie
        status, _, _ = self.session_request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.cookie = new_cookie
        status, _, _ = self.session_request("GET", "/api/status")
        self.assertEqual(status, 200)

        status, _, body = self.session_request("GET", "/api/security/audit?limit=20")
        self.assertEqual(status, 200)
        events = [row["event"] for row in json.loads(body)["events"]]
        self.assertIn("login_success", events)
        self.assertIn("password_changed", events)

        status, _, body = self.session_request(
            "POST", "/api/auth/password",
            {"current_password": "wrong-current", "new_password": "another-password-2026",
             "confirm_password": "another-password-2026"},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_current_password")
        events = [row["event"] for row in self.gateway.audit.tail(limit=20)]
        self.assertIn("password_change_failed", events)
        self.assertEqual(self.panel_password, "correct-horse-battery-2026")
        self.panel_password = new_password

    def test_security_ui_markup_and_key_store(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        status, _, body = self.session_request("GET", "/")
        self.assertEqual(status, 200)
        for marker in (b'id="securityDrawer"', b'id="passwordForm"',
                       b'id="keyForm"', b'id="auditRows"',
                       b'/api/auth/password', b'/api/security/keys'):
            self.assertIn(marker, body)
        status, _, body = self.session_request("GET", "/api/security/keys")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["require_key"])
        status, _, body = self.session_request(
            "POST", "/api/security/keys", {"action": "create", "name": "ui-key"},
            {"X-KWSA-UI": "1"},
        )
        self.assertEqual(status, 201)
        self.assertIn("secret", json.loads(body))

    def test_trusted_https_proxy_sets_secure_cookie_and_audit_ip(self):
        self.start(auth={"enabled": True, "trust_proxy": True})
        status, headers, body = self.request(
            "POST", "/api/login",
            {"username": "admin", "password": self.panel_password},
            {"Origin": self.base, "Content-Type": "application/json",
             "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.8"},
        )
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers.get("set-cookie", ""))
        self.assertTrue(json.loads(body)["cookie_secure"])
        rows = self.gateway.audit.tail(limit=5)
        self.assertEqual(rows[0]["event"], "login_success")
        self.assertEqual(rows[0]["ip"], "198.51.100.8")
        self.assertEqual(rows[0]["peer_ip"], "127.0.0.1")

    def test_same_origin_fetch_without_referer_is_allowed(self):
        self.start(auth={"enabled": True})
        self.assertEqual(self.login()[0], 200)
        status, _, _ = self.session_request(
            "POST", "/api/control/channel", {"name": "ch", "enabled": False},
            {"X-KWSA-UI": "1", "Sec-Fetch-Site": "same-origin"},
            origin=False,
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.gateway.config.channel_enabled("ch"))

    def test_decision_endpoint_requires_payload(self):
        self.start()
        doc = json.loads(Path(self.gateway.config.path).read_text(encoding="utf-8"))
        doc["channels"]["dec"] = {
            "base_url": self.upstream_base + "/v1",
            "models": ["d1"],
            "aliases": {"d": "d1"},
            "endpoint": "/v1/systemone",
            "decision": True,
        }
        Path(self.gateway.config.path).write_text(json.dumps(doc), encoding="utf-8")
        self.gateway.refresh()
        status, _, body = self.request("POST", "/v1/chat/completions", {"model": "d"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "decision_payload_required")
        status, _, _ = self.request("POST", "/v1/chat/completions", {
            "model": "d", "state": {"x": 1}, "questions": {}, "messages": [],
        })
        self.assertEqual(status, 200)
        sent = self.state.requests[-1]
        self.assertTrue(sent["path"].endswith("/v1/systemone"))
        payload = json.loads(sent["body"].decode())
        self.assertEqual(payload["model"], "d1")
        self.assertNotIn("messages", payload)


if __name__ == "__main__":
    unittest.main()
