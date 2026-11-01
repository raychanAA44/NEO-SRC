#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_upstream_custom_headers.py —— 第三方订阅自定义出站头（OpenCode 等）。

覆盖：
  · `model_upstreams.json` 的 `headers` 能被解析成 `ModelRoute.headers`；
  · gateway 保留头（Host / Content-Length）不会被冒充，`safe_headers()` 过滤；
  · `summary()` 只出掩码，不泄露头值；
  · 真实一条 POST /v1/chat/completions 打到 mock 上游时，自定义头确实跟着出站。
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


class _MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        _MockUpstream.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _routes_payload(base_url, headers):
    return {
        "schema": 1,
        "default_channel": "opencode",
        "channels": {
            "opencode": {
                "base_url": base_url,
                "token_env": "HUB_OPENCODE_TOKEN",
                "pricing_channel": "opencode",
                "headers": headers,
                "models": ["glm-5.3"],
            },
        },
    }


class TestRouterHeaders(unittest.TestCase):
    """纯解析层：headers 进得来、滤得掉、掩得住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hdr_")
        self.path = Path(self.tmp) / "model_upstreams.json"
        self.path.write_text(json.dumps(_routes_payload(
            "https://opencode.example/v1",
            {"x-opencode-token": "tok-live-SECRET", "x-api-key": "key-2",
             "Host": "evil.example", "Content-Length": "999"})), encoding="utf-8")
        self.router = ModelUpstreamRouter(self.path, default_upstream="https://default/v1",
                                          default_channel="opencode", default_token="")

    def test_route_carries_headers(self):
        route = self.router.resolve("glm-5.3")
        self.assertEqual(route.channel, "opencode")
        self.assertEqual(route.headers["x-opencode-token"], "tok-live-SECRET")
        self.assertEqual(route.headers["x-api-key"], "key-2")

    def test_reserved_headers_never_injected(self):
        route = self.router.resolve("glm-5.3")
        safe = route.safe_headers()
        self.assertIn("x-opencode-token", safe)
        self.assertNotIn("Host", safe)
        self.assertNotIn("host", safe)
        self.assertNotIn("Content-Length", safe)

    def test_summary_masks_header_values(self):
        row = self.router.summary()["channels"][0]
        self.assertEqual(sorted(row["header_names"]), ["Content-Length", "Host",
                                                       "x-api-key", "x-opencode-token"])
        self.assertNotIn("tok-live-SECRET", json.dumps(row))
        self.assertIn("●●", row["headers"]["x-opencode-token"])

    def test_public_route_snapshot_is_masked(self):
        route = self.router.resolve("glm-5.3")
        self.assertNotIn("tok-live-SECRET", json.dumps(route.public()))

    def test_env_header_is_resolved_and_masked(self):
        path = Path(self.tmp) / "env_routes.json"
        path.write_text(json.dumps(_routes_payload(
            "https://opencode.example/v1",
            {"x-opencode-token": "env:HUB_OPENCODE_TOKEN", "x-api-key": "literal-key"})),
            encoding="utf-8")
        router = ModelUpstreamRouter(
            path, default_upstream="https://default/v1", default_channel="opencode",
            default_token="", environ={"HUB_OPENCODE_TOKEN": "tok-env"})
        route = router.resolve("glm-5.3")
        self.assertEqual(route.headers["x-opencode-token"], "tok-env")
        self.assertEqual(route.headers["x-api-key"], "literal-key")
        row = router.summary()["channels"][0]
        self.assertEqual(row["header_env"], {"x-opencode-token": "HUB_OPENCODE_TOKEN"})
        self.assertNotIn("tok-env", json.dumps(row))

    def test_missing_env_header_is_not_injected(self):
        path = Path(self.tmp) / "missing_env_routes.json"
        path.write_text(json.dumps(_routes_payload(
            "https://opencode.example/v1",
            {"x-opencode-token": "env:HUB_OPENCODE_TOKEN_MISSING"})), encoding="utf-8")
        router = ModelUpstreamRouter(
            path, default_upstream="https://default/v1", default_channel="opencode",
            default_token="", environ={})
        route = router.resolve("glm-5.3")
        self.assertNotIn("x-opencode-token", route.headers)
        self.assertNotIn("x-opencode-token", route.safe_headers())
        row = router.summary()["channels"][0]
        self.assertEqual(row["header_env"], {"x-opencode-token": "HUB_OPENCODE_TOKEN_MISSING"})


class TestOutboundInjection(unittest.TestCase):
    """端到端：8888 handler 真的把自定义头带到了上游。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hdre2e_")
        tmp = Path(self.tmp)
        _MockUpstream.seen = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
        threading.Thread(target=self.upstream.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        base = "http://127.0.0.1:%d" % self.upstream.server_address[1]

        routes = tmp / "model_upstreams.json"
        routes.write_text(json.dumps(_routes_payload(
            base + "/v1", {"x-opencode-token": "tok-live-SECRET"})), encoding="utf-8")

        self.old = {
            "MODEL_ROUTER": server.MODEL_ROUTER,
            "LEDGER": server.LEDGER,
            "CONTROL": server.CONTROL,
            "MODEL_UPSTREAM": server.MODEL_UPSTREAM,
            "MODEL_TOKEN": server.MODEL_TOKEN,
            "TOKEN": server.TOKEN,
            "LEDGER_MODE": server.LEDGER_MODE,
        }

        def restore():
            for key, value in self.old.items():
                setattr(server, key, value)
            server.reset_budget_cache()

        self.addCleanup(restore)
        server.MODEL_ROUTER = ModelUpstreamRouter(
            routes, default_upstream=base + "/v1", default_channel="opencode",
            default_token="", environ={})
        server.LEDGER = LocalLedger(tmp / "ledger.db", tmp / "pricing.json")
        server.CONTROL = LocalControl(tmp / "control.json", default_channel="opencode")
        server.MODEL_UPSTREAM = base + "/v1"
        server.MODEL_TOKEN = ""
        server.TOKEN = ""
        server.LEDGER_MODE = "local"
        server.reset_budget_cache()

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]

    def test_custom_header_reaches_upstream(self):
        body = json.dumps({"model": "glm-5.3", "messages": [
            {"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     method="POST",
                                     headers={"content-type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            json.loads(resp.read().decode())
        self.assertTrue(_MockUpstream.seen, "上游一次都没被打到")
        headers = _MockUpstream.seen[-1]
        self.assertEqual(headers.get("x-opencode-token"), "tok-live-SECRET",
                         "自定义出站头必须原样注入: %s" % sorted(headers))
        self.assertNotIn("evil", headers.get("host", ""))


if __name__ == "__main__":
    unittest.main()
