#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_custom_upstreams_api.py —— 06 渠道在线接入（/api/custom 增删改）回归。

覆盖：
- 渠道元数据写 model_upstreams.json，密钥只以密文落 data 仓；
- 列表接口只出掩码，配置文件里不出现明文密钥；
- 路由表 reload 后能按模型名改道到新渠道，且带出自定义出站头；
- HTTP：POST / GET / DELETE 闭环，跨站 Origin 被 403 拒；
- 删除后配置与路由表同步摘除。
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core.custom_upstreams import CustomUpstreamStore, env_name_for  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


def _seed_config(path: Path):
    path.write_text(json.dumps({
        "schema": 1,
        "default_channel": "open-design",
        "channels": {
            "open-design": {"base_url": "https://default.example/v1",
                            "token_env": "HUB_MODEL_TOKEN",
                            "pricing_channel": "open-design"},
            "ollama": {"base_url": "https://ollama.example/v1",
                       "models": ["clef:27b"], "endpoint": "/v1/systemone"}
        },
    }, ensure_ascii=False), encoding="utf-8")


class TestCustomUpstreamStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "model_upstreams.json"
        self.keys = root / "custom_upstreams.json"
        _seed_config(self.config)
        self.env = {}
        self.store = CustomUpstreamStore(self.config, self.keys,
                                         environ=self.env, passphrase="unit-test")

    def test_save_masks_and_never_leaks_key(self):
        result = self.store.save({
            "name": "acme",
            "base_url": "https://acme.example/v1/",
            "api_key": "sk-acme-super-secret",
            "models": "gpt-x, gpt-y\ngpt-x",
            "headers": {"x-api-version": "2026-01-01"},
            "note": "ACME 测试渠道",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["channel"], "acme")
        self.assertEqual(result["models"], 2)

        doc = json.loads(self.config.read_text(encoding="utf-8"))
        entry = doc["channels"]["acme"]
        self.assertEqual(entry["base_url"], "https://acme.example/v1")
        self.assertTrue(entry["custom"])
        self.assertEqual(entry["token_env"], env_name_for("acme"))
        self.assertNotIn("sk-acme-super-secret", self.config.read_text(encoding="utf-8"))
        raw_keys = self.keys.read_text(encoding="utf-8")
        self.assertNotIn("sk-acme-super-secret", raw_keys)

        rows = self.store.list_channels()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "acme")
        self.assertEqual(row["key_masked"][:2], "sk")
        self.assertNotIn("super-secret", row["key_masked"])
        self.assertEqual(row["header_names"], ["x-api-version"])
        self.assertEqual(set(row["headers"].values()), {"●" * 6})

    def test_router_reload_routes_new_channel(self):
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "api_key": "sk-acme", "models": ["gpt-x"],
                         "headers": {"x-api-version": "2026"}})
        router = ModelUpstreamRouter(self.config, default_upstream="https://default.example/v1",
                                     default_channel="open-design", default_token="tok",
                                     environ=self.env)
        route = router.resolve("custom/acme/gpt-x")
        self.assertEqual(route.channel, "acme")
        self.assertEqual(route.url, "https://acme.example/v1/chat/completions")
        self.assertEqual(route.token, "sk-acme")
        self.assertEqual(route.safe_headers(), {"x-api-version": "2026"})

    def test_update_keeps_masked_headers_and_missing_key(self):
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "api_key": "sk-acme", "models": ["gpt-x"],
                         "headers": {"authorization": "Bearer real", "x-team": "neo"}})
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "api_key": "", "models": ["gpt-y"],
                         "headers": {"authorization": "●" * 6, "x-team": "neo2"}})
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        entry = doc["channels"]["acme"]
        self.assertEqual(entry["headers"]["authorization"], "Bearer real")
        self.assertEqual(entry["headers"]["x-team"], "neo2")
        self.assertEqual(entry["models"], ["gpt-y"])
        # 留空 = 沿用旧密钥
        env = {}
        store = CustomUpstreamStore(self.config, self.keys, environ=env,
                                    passphrase="unit-test")
        self.assertEqual(store.load_into_environ(), 1)
        self.assertEqual(env[env_name_for("acme")], "sk-acme")

    def test_delete_removes_channel_and_key(self):
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "api_key": "sk-acme", "models": ["gpt-x"]})
        result = self.store.delete("acme")
        self.assertEqual(result["deleted"], "acme")
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("acme", doc["channels"])
        self.assertIn("open-design", doc["channels"])       # 内置渠道零改动
        with self.assertRaises(ValueError):
            self.store.delete("acme")

    def test_refuses_builtin_overwrite_and_delete(self):
        """内置渠道只读：同名保存/删除必须被拒，绝不破坏生产路由。"""
        with self.assertRaises(ValueError):
            self.store.save({"name": "open-design",
                             "base_url": "https://evil.example/v1",
                             "models": ["m"]})
        with self.assertRaises(ValueError):
            self.store.delete("open-design")
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["channels"]["open-design"]["base_url"],
                         "https://default.example/v1")

    def test_note_preserved_when_omitted(self):
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "models": ["gpt-x"], "note": "旧备注"})
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "models": ["gpt-x"]})
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["channels"]["acme"]["note"], "旧备注")

    def test_save_without_key_preserves_env_token(self):
        """表单留空不得用空串盖掉 secrets.json 注入的 token。"""
        self.env[env_name_for("acme")] = "token-from-secrets"
        self.store.save({"name": "acme", "base_url": "https://acme.example/v1",
                         "models": ["gpt-x"]})
        self.assertEqual(self.env[env_name_for("acme")], "token-from-secrets")
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(doc["channels"]["acme"]["custom"])

    def test_rejects_illegal_payload(self):
        for payload in (
            {"name": "Bad Name", "base_url": "https://x.example", "models": ["m"]},
            {"name": "ok", "base_url": "ftp://x.example", "models": ["m"]},
            {"name": "ok", "base_url": "https://x.example", "models": []},
            {"name": "ok", "base_url": "https://x.example", "models": ["m"],
             "headers": {"Host": "evil"}},
        ):
            with self.assertRaises(ValueError):
                self.store.save(payload)


class TestCustomUpstreamHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config, keys = root / "model_upstreams.json", root / "custom_upstreams.json"
        _seed_config(config)
        self.old_store = server.CUSTOM_UPSTREAMS
        self.old_router = server.MODEL_ROUTER
        self.old_control = server.CONTROL
        server.CUSTOM_UPSTREAMS = CustomUpstreamStore(config, keys,
                                                      environ={}, passphrase="unit-test")
        server.MODEL_ROUTER = ModelUpstreamRouter(
            config, default_upstream="https://default.example/v1",
            default_channel="open-design", default_token="tok")
        server.CONTROL = LocalControl(root / "control.json", default_channel="open-design")
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.CUSTOM_UPSTREAMS = self.old_store
        server.MODEL_ROUTER = self.old_router
        server.CONTROL = self.old_control

    def _call(self, path, payload=None, method="POST", headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_full_crud_cycle(self):
        status, body = self._call("/api/custom", {
            "name": "acme", "base_url": "https://acme.example/v1",
            "api_key": "sk-acme", "models": "gpt-x,gpt-y",
            "headers": {"x-api-version": "2026"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["router_reloaded"])

        status, rows = self._call("/api/custom", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows["items"]), 1)
        self.assertTrue(rows["items"][0]["has_key"])

        status, body = self._call("/api/custom/acme", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], "acme")
        status, rows = self._call("/api/custom", method="GET")
        self.assertEqual(rows["items"], [])

    def test_cross_site_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/custom", {"name": "acme", "base_url": "https://a.example",
                                       "models": ["m"]},
                       headers={"Origin": "http://evil.example"})
        self.assertEqual(ctx.exception.code, 403)

    def test_bad_payload_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/custom", {"name": "acme", "base_url": "nope",
                                       "models": ["m"]})
        self.assertEqual(ctx.exception.code, 400)

    def test_list_overlays_control_enabled(self):
        self._call("/api/custom", {"name": "acme", "base_url": "https://acme.example/v1",
                                   "api_key": "sk-acme", "models": ["gpt-x"]})
        server.CONTROL.toggle_visibility("acme", True, "audit")
        status, rows = self._call("/api/custom", method="GET")
        self.assertFalse(rows["items"][0]["enabled"])

    def test_delete_with_body_keeps_keepalive_in_sync(self):
        """DELETE 带 body 时必须读干净，否则同连接下一请求会串包。"""
        import http.client
        from urllib.parse import urlsplit

        self._call("/api/custom", {"name": "acme", "base_url": "https://acme.example/v1",
                                   "api_key": "sk-acme", "models": ["gpt-x"]})
        parts = urlsplit(self.base)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            payload = json.dumps({"reason": "cleanup"}).encode("utf-8")
            conn.request("DELETE", "/api/custom/acme", body=payload,
                         headers={"content-type": "application/json",
                                  "content-length": str(len(payload))})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 200)
            conn.request("GET", "/api/custom")
            second = conn.getresponse()
            doc = json.loads(second.read().decode("utf-8"))
            self.assertEqual(second.status, 200)
            self.assertEqual(doc["items"], [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
