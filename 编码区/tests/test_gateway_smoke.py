#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gateway_smoke.py - HUB NEO 网关核心契约冒烟测试
"""

import unittest
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from gateway import validate_proxy_headers, inject_trace_context, is_port_allowed

class TestGatewaySmoke(unittest.TestCase):
    def test_proxy_headers_complete(self):
        headers = {
            "Host": "api.hub-neo.local",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "10.0.0.1"
        }
        ok, missing = validate_proxy_headers(headers)
        self.assertTrue(ok)
        self.assertEqual(len(missing), 0)

    def test_proxy_headers_missing(self):
        headers = {
            "Host": "api.hub-neo.local"
        }
        ok, missing = validate_proxy_headers(headers)
        self.assertFalse(ok)
        self.assertIn("x-forwarded-for", missing)
        self.assertIn("x-real-ip", missing)

    def test_trace_context_injection(self):
        headers = {"Host": "localhost"}
        res = inject_trace_context(headers)
        self.assertIn("X-Request-Id", res)
        self.assertTrue(len(res["X-Request-Id"]) > 0)

    def test_forbidden_port(self):
        self.assertFalse(is_port_allowed(8792), "8792 必须被永久禁止")
        self.assertTrue(is_port_allowed(8888), "8888 是 HUB NEO 标准服务端口")
        self.assertTrue(is_port_allowed(8798), "8798 是 HUB NEO 合法备用端口")

    def test_ui_assets_present(self):
        from server import UI_DIR, POST_WHITELIST
        self.assertTrue(UI_DIR.exists(), "前端 UI 静态目录必须存在")
        index_html = UI_DIR / "index.html"
        self.assertTrue(index_html.exists(), "index.html 必须存在于 ui 目录")
        self.assertIn("/api/models/toggle", POST_WHITELIST)
        self.assertIn("/api/upstream/switch", POST_WHITELIST)
        self.assertIn("/api/accounts/checkin", POST_WHITELIST)
        self.assertIn("/api/visibility", POST_WHITELIST)
        self.assertIn("/api/accounts/inspect", POST_WHITELIST)
        self.assertIn("/api/egress/proxy-url", POST_WHITELIST)
        self.assertIn("/api/official/quota-snapshot", POST_WHITELIST)

    def test_write_whitelist_exact(self):
        from server import is_write_allowed
        self.assertTrue(is_write_allowed("/api/models/toggle_bulk"))
        self.assertTrue(is_write_allowed("/api/upstream/switch"))
        self.assertTrue(is_write_allowed("/api/keys/32/realm"))
        self.assertFalse(is_write_allowed("/api/keys/32/delete"))
        self.assertFalse(is_write_allowed("/api/keys/../../models/toggle"))
        self.assertFalse(is_write_allowed("/api/custom"))
        self.assertFalse(is_write_allowed("/api/models/toggle/extra"))

    def test_sensitive_reads_blocked(self):
        from server import is_read_blocked
        self.assertTrue(is_read_blocked("/api/keys/32/reveal"))
        self.assertTrue(is_read_blocked("/api/vault"))
        self.assertTrue(is_read_blocked("/api/admin/anything"))
        self.assertFalse(is_read_blocked("/api/keys"))
        self.assertFalse(is_read_blocked("/api/security/audit"))
        self.assertFalse(is_read_blocked("/api/accounts/health"))

if __name__ == "__main__":
    unittest.main()
