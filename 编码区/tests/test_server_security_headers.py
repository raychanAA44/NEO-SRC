#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_server_security_headers.py —— 测试安全响应头与登录熔断。"""

import json
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server
from core import hardening, adminauth


class TestSecurityHardeningUnit(unittest.TestCase):
    """单测 hardening 与 adminauth 模块。"""

    def setUp(self):
        adminauth.reset_fails()

    def test_hash_and_verify_password(self):
        pw = "SecretPass123!"
        hashed = adminauth.hash_password(pw)
        self.assertTrue(hashed.startswith("scrypt$"))
        self.assertTrue(adminauth.verify_password(pw, hashed))
        self.assertFalse(adminauth.verify_password("WrongPass", hashed))
        self.assertFalse(adminauth.verify_password("", hashed))

    def test_bruteforce_lock_logic(self):
        ip = "192.0.2.1"
        self.assertEqual(adminauth.locked_for(ip), 0)

        # 4 次失败不锁
        for _ in range(4):
            adminauth.note_fail(ip)
            self.assertEqual(adminauth.locked_for(ip), 0)

        # 第 5 次触发锁定 ≥60s
        delay = adminauth.note_fail(ip)
        self.assertGreaterEqual(delay, 59)
        self.assertGreaterEqual(adminauth.locked_for(ip), 59)

        # 解锁
        adminauth.reset_fails(ip)
        self.assertEqual(adminauth.locked_for(ip), 0)

    def test_session_lifecycle(self):
        sid = adminauth.new_session()
        self.assertTrue(adminauth.verify_session(sid))
        self.assertFalse(adminauth.verify_session("non-existent-sid"))
        adminauth.drop_session(sid)
        self.assertFalse(adminauth.verify_session(sid))


class TestServerSecurityHeadersHTTP(unittest.TestCase):
    """集成测试网关 HTTP 响应头。"""

    @classmethod
    def setUpClass(cls):
        import socket
        adminauth.reset_fails()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = server._create_server(cls.port)
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        adminauth.reset_fails()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.headers, resp.read()

    def _post_json(self, path: str, data: dict):
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, resp.headers, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, json.loads(exc.read().decode("utf-8"))

    def test_health_has_security_headers(self):
        status, headers, body = self._get("/health")
        self.assertEqual(status, 200)

        # 验证 6 大安全响应头
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertIn("max-age=31536000", headers.get("strict-transport-security", ""))
        self.assertIn("camera=()", headers.get("permissions-policy", ""))
        self.assertIn("default-src 'self'", headers.get("content-security-policy", ""))

        data = json.loads(body.decode("utf-8"))
        self.assertIn("security_headers", data)
        self.assertIn("admin_auth", data)

    def test_api_pricing_has_security_headers(self):
        status, headers, _ = self._get("/api/pricing")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")

    def test_admin_auth_endpoints(self):
        adminauth.reset_fails()
        # 1. 查状态
        status, _, body = self._get("/api/admin/auth-status")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data.get("ok"))

        # 2. 模拟设置临时哈希密码
        original_hash = adminauth.ADMIN_PASS_HASH
        original_user = adminauth.ADMIN_USER
        adminauth.ADMIN_USER = "admin_test"
        adminauth.ADMIN_PASS_HASH = adminauth.hash_password("NeoSafePass99#")

        try:
            # 错误密码 -> 401
            code, _, err = self._post_json("/api/admin/login", {"username": "admin_test", "password": "wrong"})
            self.assertEqual(code, 401)
            self.assertEqual(err.get("error", {}).get("code"), "invalid_auth")

            # 正确密码 -> 200 + session_id
            code, _, succ = self._post_json("/api/admin/login", {"username": "admin_test", "password": "NeoSafePass99#"})
            self.assertEqual(code, 200)
            self.assertTrue(succ.get("ok"))
            self.assertTrue(bool(succ.get("session_id")))
        finally:
            adminauth.ADMIN_USER = original_user
            adminauth.ADMIN_PASS_HASH = original_hash
            adminauth.reset_fails()


if __name__ == "__main__":
    unittest.main()
