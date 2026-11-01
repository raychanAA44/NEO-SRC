# -*- coding: utf-8 -*-
"""编码区/tests/test_server_hardening.py —— 审计报告 2026-10-24 P0 项加固回归。

覆盖:
  · P0-1:请求体超限返回 413(HUB_MAX_REQUEST_BYTES 可调)
  · P0-2:JSON 结构化 Access Log 契约字段(timestamp/req_id/client_ip/status/upstream_status/latency_ms)
  · P0-3:写端点计数口径一致性(POST_WHITELIST 与 /health.write_endpoints 唯一正本)
  · P0-4:优雅停机(server.shutdown 后 serve_forever 返回、探针停止)
"""
import io
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
sys.path.insert(0, str(HERE.parent / "src"))

import server  # noqa: E402
from core.local_control import LocalControl  # noqa: E402


class _MockUpstream(BaseHTTPRequestHandler):
    """本地假上游:反代目标,避免测试依赖真实云端。"""

    def do_GET(self):
        payload = json.dumps({"status": "ok", "marker": "hardening"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class _HardenedGateway:
    """起一个真实网关(空闲端口),供加固断言做真实 HTTP 往返。"""

    def __enter__(self):
        self.port = self._free_port()
        self.httpd = server._create_server(self.port)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class TestBodyLimit(unittest.TestCase):
    """P0-1:请求体大小限制。"""

    def test_oversized_body_returns_413(self):
        saved = server.MAX_REQUEST_BYTES
        server.MAX_REQUEST_BYTES = 1024          # 调小便于测试
        try:
            with _HardenedGateway() as gw:
                big = b"x" * 4096
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/visibility" % gw.port,
                    data=big, method="POST",
                    headers={"content-type": "application/json",
                             "user-agent": "Mozilla/5.0 Chrome/130"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, 413, "超限请求体必须 413")
        finally:
            server.MAX_REQUEST_BYTES = saved

    def test_normal_body_still_passes(self):
        saved = server.MAX_REQUEST_BYTES
        saved_up = server.UPSTREAM
        saved_control = server.CONTROL
        tmp = tempfile.TemporaryDirectory()
        try:
            server.MAX_REQUEST_BYTES = 1024 * 1024
            # 独立 control.json：本测试只验「正常体积不被 413 误伤」，
            # 不碰生产控制面，也不触发真实账号签到的网络等待。
            server.CONTROL = LocalControl(Path(tmp.name) / "control.json",
                                          default_channel="open-design")
            mock = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
            threading.Thread(target=mock.serve_forever, daemon=True).start()
            server.UPSTREAM = "http://127.0.0.1:%d" % mock.server_address[1]
            try:
                with _HardenedGateway() as gw:
                    # 用本地控制面写端点：快、无外部副作用。
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/api/budgets" % gw.port,
                        data=json.dumps({"key": "global", "amount": None}).encode(),
                        method="POST",
                        headers={"content-type": "application/json",
                                 "user-agent": "Mozilla/5.0 Chrome/130"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        self.assertEqual(r.status, 200, "正常小请求不应被 413 误伤")
            finally:
                mock.shutdown()
                mock.server_close()
        finally:
            server.MAX_REQUEST_BYTES = saved
            server.UPSTREAM = saved_up
            server.CONTROL = saved_control
            tmp.cleanup()


class TestAccessLog(unittest.TestCase):
    """P0-2:JSON 结构化 Access Log 字段完整性(经 _proxy 的真实反代往返)。"""

    def test_access_log_json_fields(self):
        saved_up = server.UPSTREAM
        try:
            mock = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
            threading.Thread(target=mock.serve_forever, daemon=True).start()
            server.UPSTREAM = "http://127.0.0.1:%d" % mock.server_address[1]
            try:
                with _HardenedGateway() as gw:
                    captured = io.StringIO()
                    real_stderr = sys.stderr
                    sys.stderr = captured
                    try:
                        req = urllib.request.Request(
                            "http://127.0.0.1:%d/v1/chat/completions" % gw.port,
                            data=b'{"model":"mock-model","messages":[]}',
                            method="POST",
                            headers={"content-type": "application/json",
                                     "user-agent": "Mozilla/5.0 Chrome/130"})
                        try:
                            urllib.request.urlopen(req, timeout=10)
                        except urllib.error.HTTPError:
                            pass                       # 任意上游回执都会产生 access log
                    finally:
                        sys.stderr = real_stderr
                    lines = [l for l in captured.getvalue().splitlines()
                             if l.startswith("{")]
                    self.assertTrue(lines, "反代响应应输出 JSON access log")
                    entry = json.loads(lines[-1])
                    for field in ("timestamp", "req_id", "client_ip", "method", "path",
                                  "status", "upstream_status", "latency_ms"):
                        self.assertIn(field, entry, "契约字段 %s 缺失:%s" % (field, entry))
            finally:
                mock.shutdown()
                mock.server_close()
        finally:
            server.UPSTREAM = saved_up


class TestWriteEndpointCount(unittest.TestCase):
    """P0-3:写端点计数口径一致性。"""

    def test_health_count_matches_whitelist(self):
        self.assertEqual(server.POST_WHITELIST.__len__() + 1, 15,
                         "白名单 14 项 + realm = 15,与 README/health 口径一致")
        for p in server.POST_WHITELIST:
            self.assertTrue(server.is_write_allowed(p), "白名单端点必须放行:%s" % p)
        self.assertTrue(server.is_write_allowed("/api/keys/k1/realm"), "realm 变更必须放行")
        self.assertFalse(server.is_write_allowed("/api/anything_else"), "白名单外必须拒绝")

    def test_readme_count_claim_synced(self):
        readme = (HERE.parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("14 个固定写端点", readme, "README 计数应与实现同步")


class TestGracefulShutdown(unittest.TestCase):
    """P0-4:优雅停机。"""

    def test_shutdown_stops_serving(self):
        with _HardenedGateway() as gw:
            # shutdown() 置位后 serve_forever 应在轮询间隔内返回
            stopper = threading.Thread(target=gw.httpd.shutdown, daemon=True)
            stopper.start()
            self.assertTrue(gw.thread.join(timeout=5.0) is not None or not gw.thread.is_alive(),
                            "shutdown 后 serve_forever 必须可退出")
            self.assertFalse(gw.thread.is_alive())


class TestRateLimiting(unittest.TestCase):
    """安全建议1: HTTP 429 令牌桶限流闸门"""

    def test_burst_exhaustion_returns_429(self):
        # 临时将 rate limiter 调整为极低容量以便触发 429
        saved_rate = server.RATE_LIMITER.rate
        saved_burst = server.RATE_LIMITER.burst
        server.RATE_LIMITER.rate = 1.0
        server.RATE_LIMITER.burst = 2.0
        server.RATE_LIMITER.reset()
        try:
            with _HardenedGateway() as gw:
                # 连续发 3 个 /api/status 请求
                for i in range(2):
                    req = urllib.request.Request("http://127.0.0.1:%d/api/status" % gw.port)
                    with urllib.request.urlopen(req, timeout=5) as res:
                        self.assertEqual(res.status, 200)

                # 第 3 次应触发 429
                req = urllib.request.Request("http://127.0.0.1:%d/api/status" % gw.port)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 429)
                self.assertIn("Retry-After", ctx.exception.headers)

                # 验证 /health 依然放行（白名单豁免）
                health_req = urllib.request.Request("http://127.0.0.1:%d/health" % gw.port)
                with urllib.request.urlopen(health_req, timeout=5) as res:
                    self.assertEqual(res.status, 200)
                    data = json.loads(res.read().decode("utf-8"))
                    self.assertIn("rate_limit", data)
                    self.assertIn("conn_pool", data)
                    self.assertIn("prompt_opt", data)
        finally:
            server.RATE_LIMITER.rate = saved_rate
            server.RATE_LIMITER.burst = saved_burst
            server.RATE_LIMITER.reset()


if __name__ == "__main__":
    unittest.main()
