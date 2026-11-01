#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_server_upstream_switch.py - 上游探针与一键回退回归。

覆盖：
- /health 暴露云端/本地目标与最近探针；
- 探针失败累计连续失败次数；
- 切换需要回环同源 UI 头或 admin token；
- 健康目标可切换，后续反代改走新上游；
- 本地不可达时拒绝切换，当前上游保持不变。
"""

import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import server


class _MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.last_auth = self.headers.get("authorization", "")
        if self.path == "/health":
            if getattr(self.server, "fail_health_once", False):
                self.server.fail_health_once = False
                self._send(503, {"status": "down"})
            elif self.server.healthy:
                self._send(200, {"status": "ok", "profile": "mock"})
            else:
                self._send(503, {"status": "down"})
        elif self.path == "/api/status":
            self._send(200, {"marker": self.server.marker})
        elif self.path == "/v1/models":
            self._send(401, {"error": {"message": "auth required"}})
        elif self.path.startswith("/api/models"):
            self._send(200, {"marker": self.server.marker})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _start_mock(marker: str):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
    srv.marker = marker
    srv.healthy = True
    srv.fail_health_once = False
    threading.Thread(target=srv.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv


class TestServerUpstreamSwitch(unittest.TestCase):
    def setUp(self):
        self.old = {
            "UPSTREAM": server.UPSTREAM,
            "UPSTREAM_CLOUD": server.UPSTREAM_CLOUD,
            "UPSTREAM_LOCAL": server.UPSTREAM_LOCAL,
            "TOKEN": server.TOKEN,
            "LOCAL_TOKEN": server.LOCAL_TOKEN,
            "PORT": server.PORT,
            "PROBE_INTERVAL": server.UPSTREAM_PROBE_INTERVAL,
        }
        self.cloud = _start_mock("cloud")
        self.local = _start_mock("local")
        self.cloud_url = "http://127.0.0.1:%d" % self.cloud.server_address[1]
        self.local_url = "http://127.0.0.1:%d" % self.local.server_address[1]

        server.UPSTREAM_CLOUD = self.cloud_url
        server.UPSTREAM_LOCAL = self.local_url
        server.UPSTREAM = self.cloud_url
        server.TOKEN = "cloud-token"
        server.LOCAL_TOKEN = "local-token"
        server.UPSTREAM_PROBE_INTERVAL = 30
        with server._PROBE_LOCK:
            server._PROBE_STATE.clear()
            server._PROBE_STATE.update({
                "ok": None,
                "status": "checking",
                "target": server.UPSTREAM,
                "role": "cloud",
                "checked_at": None,
                "latency_ms": None,
                "http_status": None,
                "error": "",
                "consecutive_failures": 0,
            })

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        server.PORT = self.gateway.server_address[1]
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.origin = self.base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        for srv in (self.cloud, self.local):
            srv.shutdown()
            srv.server_close()
        server.UPSTREAM = self.old["UPSTREAM"]
        server.UPSTREAM_CLOUD = self.old["UPSTREAM_CLOUD"]
        server.UPSTREAM_LOCAL = self.old["UPSTREAM_LOCAL"]
        server.TOKEN = self.old["TOKEN"]
        server.LOCAL_TOKEN = self.old["LOCAL_TOKEN"]
        server.PORT = self.old["PORT"]
        server.UPSTREAM_PROBE_INTERVAL = self.old["PROBE_INTERVAL"]

    def _request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers=headers or {},
        )
        try:
            with self.opener.open(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _switch(self, target, headers=None):
        body = json.dumps({"target": target}).encode("utf-8")
        base_headers = {
            "content-type": "application/json",
            "x-hub-ui": "1",
            "origin": self.origin,
        }
        base_headers.update(headers or {})
        return self._request("POST", "/api/upstream/switch", body, base_headers)

    def test_health_reports_targets_and_probe(self):
        probe = server.probe_upstream(self.cloud_url)
        self.assertTrue(probe["ok"])

        status, data = self._request("GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(data["upstream"], self.cloud_url)
        self.assertEqual(data["upstream_role"], "cloud")
        self.assertEqual(data["upstream_cloud"], self.cloud_url)
        self.assertEqual(data["upstream_local"], self.local_url)
        self.assertTrue(data["upstream_health"]["ok"])
        self.assertEqual(data["upstream_health"]["target"], self.cloud_url)

    def test_probe_retries_transient_failure(self):
        self.cloud.fail_health_once = True

        probe = server.probe_upstream(self.cloud_url)

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["attempts"], 2)
        self.assertEqual(probe["consecutive_failures"], 0)

    def test_probe_failure_increments_consecutive_failures(self):
        dead = "http://127.0.0.1:%d" % _free_port()
        server.UPSTREAM = dead
        first = server.probe_upstream(dead)
        second = server.probe_upstream(dead)

        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(second["consecutive_failures"], 2)
        self.assertEqual(server.upstream_state()["upstream_health"]["status"], "down")

    def test_switch_requires_ui_auth(self):
        server.TOKEN = "test-token"
        body = json.dumps({"target": "cloud"}).encode("utf-8")

        status, _ = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json"})
        self.assertEqual(status, 403)

        status, _ = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json", "x-hub-ui": "1"})
        self.assertEqual(status, 403)

        status, _ = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json",
             "x-hub-ui": "1", "origin": "http://evil.example"})
        self.assertEqual(status, 403)

        status, data = self._switch("cloud")
        self.assertEqual(status, 200)
        self.assertEqual(data["upstream"], self.cloud_url)

    def test_tokenless_cross_origin_is_blocked(self):
        server.TOKEN = ""
        body = json.dumps({"target": "cloud"}).encode("utf-8")

        status, _ = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json", "origin": "http://evil.example"})
        self.assertEqual(status, 403)

        status, _ = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json",
             "x-hub-ui": "1", "origin": "http://evil.example"})
        self.assertEqual(status, 403)

        status, data = self._switch("cloud")
        self.assertEqual(status, 200)
        self.assertEqual(data["upstream"], self.cloud_url)

    def test_switch_to_healthy_local_changes_proxy_target(self):
        status, data = self._switch("local")

        self.assertEqual(status, 200)
        self.assertTrue(data["changed"])
        self.assertEqual(data["upstream"], self.local_url)
        self.assertEqual(data["upstream_role"], "local")

        status, proxied = self._request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(proxied["marker"], "local")
        self.assertEqual(self.local.last_auth, "Bearer local-token")

        status, back = self._switch("cloud")
        self.assertEqual(status, 200)
        self.assertEqual(back["upstream"], self.cloud_url)
        self.assertEqual(self.cloud.last_auth, "Bearer cloud-token")

    def test_unreachable_local_is_rejected_and_keeps_current(self):
        dead = "http://127.0.0.1:%d" % _free_port()
        server.UPSTREAM_LOCAL = dead

        status, data = self._switch("local")

        self.assertEqual(status, 409)
        self.assertIn("目标上游不可达", data["error"]["message"])
        self.assertEqual(server.UPSTREAM, self.cloud_url)
        self.assertEqual(data["upstream"], self.cloud_url)
        self.assertFalse(data["upstream_health"]["ok"])

    def test_force_switch_allows_unreachable_target(self):
        dead = "http://127.0.0.1:%d" % _free_port()
        server.UPSTREAM_LOCAL = dead
        body = json.dumps({"target": "local", "force": True}).encode("utf-8")

        status, data = self._request(
            "POST", "/api/upstream/switch", body,
            {"content-type": "application/json",
             "x-hub-ui": "1", "origin": self.origin})

        self.assertEqual(status, 200)
        self.assertTrue(data["forced"])
        self.assertEqual(data["upstream"], dead)
        self.assertFalse(data["upstream_health"]["ok"])


if __name__ == "__main__":
    unittest.main()

class TestProbePathConfig(unittest.TestCase):
    """NEO-GW-05 / R-1:探针路径可配置化与多协议存活判定。"""

    def setUp(self):
        self._saved = server.PROBE_PATH

    def tearDown(self):
        server.PROBE_PATH = self._saved

    def test_custom_probe_path_used(self):
        """自定义 probe_path:200 即视为可达,不强制 JSON status=ok。"""
        port = _free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _MockUpstream)
        httpd.healthy = True
        httpd.marker = "probe-path"
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            server.PROBE_PATH = "/api/status"
            result = server.probe_upstream(url="http://127.0.0.1:%d" % port, timeout=3)
            self.assertTrue(result["ok"], "自定义探针路径 200 应判定存活:%s" % result)
            self.assertEqual(result.get("probe_path"), "/api/status")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_models_probe_401_counts_alive(self):
        """/v1/models 探针:401(鉴权存活)视为服务可达,不再误报 DOWN。"""
        port = _free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _MockUpstream)
        httpd.healthy = True
        httpd.marker = "models-401"
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            server.PROBE_PATH = "/v1/models"
            result = server.probe_upstream(url="http://127.0.0.1:%d" % port, timeout=3)
            self.assertTrue(result["ok"], "/v1/models 401 应视为鉴权存活:%s" % result)
        finally:
            httpd.shutdown()
            httpd.server_close()
