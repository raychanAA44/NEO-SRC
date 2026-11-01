#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_egress_overrides_and_probe.py —— OC 桥 / 分渠道代理 / 高阶调参回归。

覆盖：
- control.json 渠道出口三态（inherit / DIRECT / 独立 URL）与回退；
- 双腿探针真实打本地回显端点，返回 IP 与耗时；
- OC Bridge Token 密文落盘、只回掩码、可清除；
- `/api/egress/probe`、`/api/oc-bridge/token`、`/api/prompt-opt/mode`、
  `/api/rate-limit/config`、`/api/pricing/single` 真机口径。
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
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.oc_bridge import OcBridgeStore  # noqa: E402


class _Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        payload = json.dumps({"ip": "203.0.113.9"}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


class TestControlEgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "control.json"
        self.ctl = LocalControl(self.path, default_channel="open-design")

    def test_channel_override_three_states(self):
        self.ctl.set_egress("http://global:7897")
        self.assertEqual(self.ctl.egress_proxy("acme"), "http://global:7897")
        self.ctl.set_channel_egress("acme", "direct")
        self.assertEqual(self.ctl.egress_proxy("acme"), "")
        self.assertEqual(self.ctl.egress_proxy("other"), "http://global:7897")
        self.ctl.set_channel_egress("acme", "proxy", "http://acme:8080")
        self.assertEqual(self.ctl.egress_proxy("acme"), "http://acme:8080")
        payload = self.ctl.egress_payload("acme")
        self.assertEqual(payload["channel_override"], "http://acme:8080")
        self.assertEqual(payload["channel_proxy_url"], "http://acme:8080")
        self.ctl.set_channel_egress("acme", "inherit")
        self.assertEqual(self.ctl.egress_proxy("acme"), "http://global:7897")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["egress"]["channel_overrides"], {})

    def test_invalid_override_rejected(self):
        with self.assertRaises(ValueError):
            self.ctl.set_channel_egress("acme", "proxy", "socks5://x:1")
        with self.assertRaises(ValueError):
            self.ctl.set_channel_egress("acme", "weird")


class TestOcBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "oc_bridge.json"
        self.store = OcBridgeStore(self.path, passphrase="unit-test")

    def test_token_roundtrip_and_mask(self):
        result = self.store.set_token("oc-bridge-secret-token")
        self.assertTrue(result["configured"])
        self.assertNotIn("oc-bridge-secret-token",
                         self.path.read_text(encoding="utf-8"))
        public = self.store.public()
        self.assertTrue(public["has_token"])
        self.assertNotIn("secret", public["token_masked"])
        self.assertEqual(self.store.get_token(), "oc-bridge-secret-token")
        self.store.set_token("")
        self.assertFalse(self.store.public()["has_token"])

    def test_short_token_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_token("short")


class TestEgressAndPolicyHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.control_path = root / "control.json"
        self.old_control = server.CONTROL
        self.old_oc = server.OC_BRIDGE
        self.old_probe = server.EGRESS_PROBE_URL
        self.old_ledger = server.LEDGER
        self.old_mode = server.PROMPT_OPTIMIZER.mode
        self.old_rate = (server.RATE_LIMITER.rate, server.RATE_LIMITER.burst)
        server.CONTROL = LocalControl(self.control_path, default_channel="open-design")
        server.OC_BRIDGE = OcBridgeStore(root / "oc_bridge.json", passphrase="unit-test")
        pricing = root / "pricing.json"
        pricing.write_text(json.dumps({
            "unit": "USD_per_mtok", "usd_cny": 7.2,
            "upstreams": {"open-design": {"models": {
                "glm-5.3-flash": {"in": 0.1, "cache": 0.01, "out": 0.2}}}}},
        ), encoding="utf-8")
        self.ledger = LocalLedger(root / "ledger.db", pricing)
        server.LEDGER = self.ledger
        self.echo = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        threading.Thread(target=self.echo.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        server.EGRESS_PROBE_URL = "http://127.0.0.1:%d/ip" % self.echo.server_address[1]
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.echo.shutdown()
        self.echo.server_close()
        server.CONTROL = self.old_control
        server.OC_BRIDGE = self.old_oc
        server.EGRESS_PROBE_URL = self.old_probe
        server.LEDGER = self.old_ledger
        server.PROMPT_OPTIMIZER.set_mode(self.old_mode)
        server.RATE_LIMITER.set_rate(*self.old_rate)
        server.reset_budget_cache()

    def _call(self, path, payload=None, method="POST"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_probe_returns_real_ip_and_latency(self):
        status, body = self._call("/api/egress/probe", {"channel": "acme"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["direct"]["ip"], "203.0.113.9")
        self.assertEqual(body["proxy"]["ip"], "203.0.113.9")
        self.assertIsNotNone(body["direct"]["elapsed_ms"])
        self.assertFalse(body["proxy_configured"])

    def test_channel_override_endpoint(self):
        status, body = self._call("/api/egress/channel-override", {
            "channel": "acme", "mode": "proxy", "url": "http://acme:8080"})
        self.assertEqual(status, 200)
        self.assertEqual(body["effective"], "http://acme:8080")
        status, body = self._call("/api/egress/channel-override", {
            "channel": "acme", "mode": "direct"})
        self.assertEqual(body["effective"], "")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/egress/channel-override", {
                "channel": "acme", "mode": "proxy", "url": "socks5://x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_oc_bridge_token_endpoint(self):
        status, body = self._call("/api/oc-bridge/token", {"token": "oc-secret-token-1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["configured"])
        status, body = self._call("/api/oc-bridge/status", method="GET")
        self.assertTrue(body["has_token"])
        self.assertNotIn("secret-token-1", json.dumps(body))
        status, body = self._call("/api/oc-bridge/token", {"clear": True})
        self.assertFalse(body["configured"])

    def test_prompt_mode_and_rate_limit_endpoints(self):
        status, body = self._call("/api/prompt-opt/mode", {"mode": "enforce"})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "enforce")
        self.assertEqual(server.PROMPT_OPTIMIZER.mode, "enforce")
        saved = json.loads(self.control_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["prompt_opt"]["mode"], "enforce")

        status, body = self._call("/api/rate-limit/config", {"rps": 42, "burst": 60})
        self.assertEqual(status, 200)
        self.assertEqual(body["rate_limit"]["rps"], 42.0)
        self.assertEqual(server.RATE_LIMITER.rate, 42.0)
        self.assertEqual(server.RATE_LIMITER.burst, 60.0)

    def test_pricing_single_edit(self):
        status, body = self._call("/api/pricing/single", {
            "channel": "open-design", "model": "glm-5.3-flash",
            "in_cny": 7.2, "cache_cny": 0.72, "out_cny": 14.4})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "glm-5.3-flash")
        doc = self.ledger.pricing_doc()
        row = doc["upstreams"]["open-design"]["models"]["glm-5.3-flash"]
        self.assertAlmostEqual(row["in"], 1.0, places=6)
        self.assertAlmostEqual(row["out"], 2.0, places=6)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/pricing/single", {"channel": "open-design",
                                               "model": "glm-5.3-flash", "in_cny": -1})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
