#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_custom_model_probe.py —— 渠道「探测模型」回归（/api/custom/probe）。

覆盖：
- 200 名单解析：data[].id 归一、去重、base 已带 /v1 不重复拼；
- 401/403 = auth_error（ok=False，绝不吐模型）；空名单 = no_models；
- 密钥留空 / 头是掩码时回退已落盘真值（编辑态探针）；
- HTTP：回环同源 200，跨站 Origin 403。
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core.custom_upstreams import CustomUpstreamStore  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


class _StubUpstream(BaseHTTPRequestHandler):
    """假上游：按 status / 名单回 /v1/models，并记下收到的鉴权头。"""

    status = 200
    models = ["gpt-x", "gpt-y", "gpt-x"]
    seen = []

    def do_GET(self):                                   # noqa: N802 http.server 契约
        type(self).seen.append({"path": self.path,
                                "auth": self.headers.get("authorization") or "",
                                "team": self.headers.get("x-team") or "",
                                "ua": self.headers.get("user-agent") or "",
                                "referer": self.headers.get("referer") or "",
                                "origin": self.headers.get("origin") or ""})
        if self.path != "/v1/models":
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if type(self).status >= 400:
            self.send_response(type(self).status)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = json.dumps({"object": "list",
                           "data": [{"id": m} for m in type(self).models]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _TagsOnlyUpstream(BaseHTTPRequestHandler):
    """只把名单挂在非 /v1/models 路径上的上游（ollama/tags 一类），
    并附上上下文与价目字段，验证探测会走候选端点且把字段收回来。"""

    seen = []

    def do_GET(self):                                   # noqa: N802
        type(self).seen.append(self.path)
        if self.path != "/api/tags":
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = json.dumps({"models": [{
            "id": "m1", "context_length": 32000, "max_output_tokens": 4096,
            "capabilities": {"tools": True, "reasoning": False},
            "pricing": {"input": 0.5, "output": 1.5, "cache_read": 0.05},
        }]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


class TestChannelModelsProbe(unittest.TestCase):
    def setUp(self):
        _StubUpstream.status = 200
        _StubUpstream.models = ["gpt-x", "gpt-y", "gpt-x"]
        _StubUpstream.seen = []
        self.httpd, self.base = _serve(_StubUpstream)
        self.addCleanup(self._close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "model_upstreams.json"
        self.config.write_text(json.dumps({"schema": 1, "channels": {}}), encoding="utf-8")
        self.env = {}
        self.store = CustomUpstreamStore(self.config, root / "custom_upstreams.json",
                                         environ=self.env, passphrase="unit-test")

    def _close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_lists_models_and_never_doubles_v1(self):
        got = server.channel_models_probe(self.base + "/v1", token="sk-1")
        self.assertTrue(got["ok"])
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["models"], ["gpt-x", "gpt-y"])
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["url"], self.base + "/v1/models")
        self.assertEqual(_StubUpstream.seen[0]["auth"], "Bearer sk-1")

    def test_sends_browser_headers_not_python_urllib(self):
        """回归：Python-urllib 的 UA 会被云盾直接 403（opencode 实测），
        密钥对不对都过不去，所以探针必须带浏览器 UA 与同源头部。"""
        out = server.channel_models_probe(self.base, token="sk-probe")
        self.assertEqual(out["status"], "ok", out.get("error"))
        seen = _StubUpstream.seen[-1]
        self.assertIn("Mozilla/5.0", seen["ua"])
        self.assertNotIn("Python-urllib", seen["ua"])
        self.assertTrue(seen["referer"].startswith(self.base))
        self.assertEqual(seen["origin"], self.base)

    def test_401_is_auth_error_and_leaks_nothing(self):
        _StubUpstream.status = 401
        got = server.channel_models_probe(self.base, token="bad")
        self.assertFalse(got["ok"])
        self.assertEqual(got["status"], "auth_error")
        self.assertEqual(got["models"], [])
        self.assertEqual(got["http_status"], 401)

    def test_empty_list_is_no_models(self):
        _StubUpstream.models = []
        got = server.channel_models_probe(self.base)
        self.assertFalse(got["ok"])
        self.assertEqual(got["status"], "no_models")

    def test_falls_back_to_other_known_endpoints(self):
        _TagsOnlyUpstream.seen = []
        httpd, base = _serve(_TagsOnlyUpstream)
        self.addCleanup(httpd.shutdown)
        got = server.channel_models_probe(base)
        self.assertTrue(got["ok"], got.get("error"))
        self.assertEqual(got["models"], ["m1"])
        self.assertIn("/api/tags", _TagsOnlyUpstream.seen)
        tried = {row["path"]: row for row in got["tried"]}
        self.assertEqual(tried["/v1/models"]["http_status"], 404)
        self.assertEqual(tried["/api/tags"]["status"], "ok")
        row = got["details"][0]
        self.assertEqual(row["context"], 32000)
        self.assertEqual(row["max_out"], 4096)
        self.assertTrue(row["tools"])
        self.assertEqual(row["pricing"], {"in": 0.5, "out": 1.5, "cache": 0.05})
        self.assertIn("pricing", got["fields"])

    def test_rejects_non_http_base(self):
        with self.assertRaises(ValueError):
            server.channel_models_probe("ftp://nope.example")

    def test_probe_auth_falls_back_to_sealed_key_and_headers(self):
        self.store.save({"name": "acme", "base_url": self.base,
                         "api_key": "sk-sealed", "models": ["gpt-x"],
                         "headers": {"x-team": "neo"}})
        token, headers = self.store.probe_auth("acme", "", {"x-team": "●" * 6})
        self.assertEqual(token, "sk-sealed")
        self.assertEqual(headers, {"x-team": "neo"})
        # 表单里现填的密钥/头优先，且掩码不会被当成真值发出去
        token2, headers2 = self.store.probe_auth("acme", "sk-typed", {"x-team": "other"})
        self.assertEqual(token2, "sk-typed")
        self.assertEqual(headers2, {"x-team": "other"})


class TestProbeHttp(unittest.TestCase):
    def setUp(self):
        _StubUpstream.status = 200
        _StubUpstream.models = ["gpt-x"]
        _StubUpstream.seen = []
        self.stub, self.upstream = _serve(_StubUpstream)
        self.addCleanup(self._close_stub)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = root / "model_upstreams.json"
        config.write_text(json.dumps({"schema": 1, "channels": {}}), encoding="utf-8")
        self.old_store, self.old_router = server.CUSTOM_UPSTREAMS, server.MODEL_ROUTER
        server.CUSTOM_UPSTREAMS = CustomUpstreamStore(
            config, root / "custom_upstreams.json", environ={}, passphrase="unit-test")
        server.MODEL_ROUTER = ModelUpstreamRouter(config)
        self.httpd, self.base = _serve(server.HubNeoHandler)
        self.addCleanup(self._restore)

    def _close_stub(self):
        self.stub.shutdown()
        self.stub.server_close()

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.CUSTOM_UPSTREAMS = self.old_store
        server.MODEL_ROUTER = self.old_router

    def _call(self, payload, headers=None):
        req = urllib.request.Request(self.base + "/api/custom/probe",
                                     data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
        req.add_header("content-type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_probe_endpoint_fills_models(self):
        status, body = self._call({"name": "acme", "base_url": self.upstream,
                                   "api_key": "sk-live"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["models"], ["gpt-x"])
        self.assertEqual(_StubUpstream.seen[0]["auth"], "Bearer sk-live")
        # 只读探针：不落盘、不建渠道
        self.assertEqual(server.CUSTOM_UPSTREAMS.list_channels(), [])

    def test_cross_site_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call({"base_url": self.upstream},
                       {"origin": "https://evil.example"})
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
