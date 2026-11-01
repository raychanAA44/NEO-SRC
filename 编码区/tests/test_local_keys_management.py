#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_keys_management.py —— 07 KEY 签发 / 启停 / 吊销 与安全门禁回归。

覆盖：
- 签发只回一次明文，落盘只有 SHA-256 与掩码；
- 列表 / 启停 / 改 Realm / 吊销全链路口径；
- 改管理员口令：旧口令校验、scrypt 落盘、明文不落盘；
- 锁定 IP 清单可读，一键解封清空熔断。
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
from core import adminauth  # noqa: E402
from core.local_keys import KEY_PREFIX, LocalKeyStore  # noqa: E402


class TestLocalKeyStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "local_keys.json"
        self.store = LocalKeyStore(self.path)

    def test_issue_returns_plaintext_once_and_stores_hash(self):
        result = self.store.issue("oc-bridge", realm="opencode", note="桥接")
        token = result["key"]
        self.assertTrue(token.startswith(KEY_PREFIX))
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(token, raw)
        self.assertNotIn("oc-bridge", raw.replace('"oc-bridge"', ""))  # 名称可留，明文不可留

        rows = self.store.list_public()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["realm"], "opencode")
        self.assertFalse(rows[0]["key_masked"].endswith(token[-8:]))
        self.assertEqual(self.store.verify(token)["id"], result["issued"])

    def test_toggle_realm_and_revoke(self):
        issued = self.store.issue("k1")
        key_id = issued["issued"]
        self.assertTrue(self.store.toggle(key_id, False)["enabled"] is False)
        self.assertIsNone(self.store.verify(issued["key"]))
        self.assertTrue(self.store.toggle(key_id, True)["enabled"] is True)
        self.assertEqual(self.store.set_realm(key_id, "custom:acme")["realm"], "custom:acme")
        self.store.revoke(key_id)
        self.assertEqual(self.store.list_public(), [])
        self.assertIsNone(self.store.verify(issued["key"]))
        with self.assertRaises(ValueError):
            self.store.revoke(key_id)

    def test_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            self.store.issue("  ")


class TestKeysAndSecurityHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.old_keys = server.LOCAL_KEYS
        self.old_hash = adminauth.ADMIN_PASS_HASH
        self.old_password_path = adminauth.PASSWORD_PATH
        server.LOCAL_KEYS = LocalKeyStore(root / "local_keys.json")
        adminauth.ADMIN_PASS_HASH = ""
        adminauth.PASSWORD_PATH = root / "admin_auth.json"
        adminauth.reset_fails()
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.LOCAL_KEYS = self.old_keys
        adminauth.ADMIN_PASS_HASH = self.old_hash
        adminauth.PASSWORD_PATH = self.old_password_path
        adminauth.reset_fails()

    def _call(self, path, payload=None, method="POST"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_issue_list_toggle_revoke_cycle(self):
        status, body = self._call("/api/keys", {"name": "oc-bridge", "realm": "custom:opencode"})
        self.assertEqual(status, 200)
        token, key_id = body["key"], body["issued"]

        status, rows = self._call("/api/keys", method="GET")
        self.assertEqual(status, 200)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["id"], key_id)
        self.assertNotIn(token, json.dumps(rows, ensure_ascii=False))

        status, body = self._call("/api/keys/%s/toggle" % key_id, {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])

        status, body = self._call("/api/keys/%s/realm" % key_id, {"realm": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(body["realm"], "all")

        status, body = self._call("/api/keys/%s" % key_id, method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body["revoked"], key_id)
        status, rows = self._call("/api/keys", method="GET")
        self.assertEqual(rows, [])

    def test_password_rotation_requires_old(self):
        status, body = self._call("/api/admin/password", {"new_password": "first-pass-1"})
        self.assertEqual(status, 200)
        saved = json.loads(adminauth.PASSWORD_PATH.read_text(encoding="utf-8"))
        self.assertTrue(saved["password_hash"].startswith("scrypt$"))
        self.assertNotIn("first-pass-1", adminauth.PASSWORD_PATH.read_text(encoding="utf-8"))

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/admin/password", {"old_password": "wrong",
                                              "new_password": "second-pass-2"})
        self.assertEqual(ctx.exception.code, 401)

        status, body = self._call("/api/admin/password", {
            "old_password": "first-pass-1", "new_password": "second-pass-2"})
        self.assertEqual(status, 200)
        self.assertTrue(adminauth.verify_password("second-pass-2", adminauth.ADMIN_PASS_HASH))

    def test_password_change_invalidates_sessions(self):
        sid = adminauth.new_session()
        self.assertTrue(adminauth.verify_session(sid))
        adminauth.set_password("session-kill-1", path=Path(self.tmp.name) / "admin_auth.json")
        self.assertFalse(adminauth.verify_session(sid))

    def test_locked_ips_and_unlock(self):
        for _ in range(adminauth.MAX_FAILS):
            adminauth.note_fail("10.9.8.7")
        status, body = self._call("/api/admin/locked-ips", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(any(row["ip"] == "10.9.8.7" for row in body["locked"]))

        status, body = self._call("/api/admin/unlock?ip=10.9.8.7", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["ip"], "10.9.8.7")
        status, body = self._call("/api/admin/locked-ips", method="GET")
        self.assertFalse(any(row["ip"] == "10.9.8.7" for row in body["locked"]))

        for _ in range(adminauth.MAX_FAILS):
            adminauth.note_fail("10.9.8.8")
        status, body = self._call("/api/admin/unlock", method="GET")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["reset_count"], 1)
        status, body = self._call("/api/admin/locked-ips", method="GET")
        self.assertEqual(body["locked"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
