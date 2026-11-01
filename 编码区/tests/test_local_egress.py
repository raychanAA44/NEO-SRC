#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_egress.py - 出站代理按 control.json 生效的回归。

覆盖：空代理走默认直连、同 URL 复用同一 opener、换 URL 换 opener、
带代理时确实走 ProxyHandler、直连路径能真实取到本地 mock 上游。
"""

import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core import egress  # noqa: E402


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        pass


class TestEgress(unittest.TestCase):
    def test_empty_proxy_uses_default_path(self):
        self.assertIsNone(egress.opener_for(""))
        self.assertIsNone(egress.opener_for("   "))
        self.assertIsNone(egress.opener_for(None))

    def test_opener_cached_per_url(self):
        first = egress.opener_for("http://127.0.0.1:7890")
        self.assertIs(first, egress.opener_for("http://127.0.0.1:7890"),
                      "同 URL 必须复用 opener，不能每次重建")
        self.assertIsNot(first, egress.opener_for("http://127.0.0.1:7891"),
                         "换代理必须换 opener")
        handlers = [h for h in first.handlers
                    if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(handlers, "代理 opener 必须带 ProxyHandler")
        self.assertEqual(handlers[0].proxies,
                         {"http": "http://127.0.0.1:7890",
                          "https": "http://127.0.0.1:7890"})

    def test_open_url_direct_reaches_local_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/x" % server.server_address[1]
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with egress.open_url(req, timeout=5, proxy_url="") as res:
            self.assertEqual(res.status, 200)
            self.assertIn(b'"ok"', res.read())

    def test_dead_proxy_breaks_non_local_request(self):
        """设了死代理时外部请求必须报错，不能静默直连（回环地址按 urllib 规则 bypass）。"""
        req = urllib.request.Request("http://example.invalid/x")
        with self.assertRaises(Exception):
            with egress.open_url(req, timeout=3, proxy_url="http://127.0.0.1:9") as res:
                res.read()


if __name__ == "__main__":
    unittest.main()
