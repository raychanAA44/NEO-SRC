#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_vendor_session.py —— 浏览器会话只读探针：URI 口径 + Chromium 系解密。

回归点（2026-10-26）：
1. `sqlite_ro_uri` 必须给 `file:///C:/...`（少个斜杠 sqlite 就报 unable to open database file）；
2. Chromium 的 `v10/v11` cookie 用 AES-256-GCM 解，`v20` 如实报不可解；
3. `read_cookies` 在 Windows 路径带空格时也能读到未加密条目。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import vendor_session  # noqa: E402


class TestSqliteUri(unittest.TestCase):
    def test_windows_absolute_path_is_three_slashes(self):
        uri = vendor_session.sqlite_ro_uri(Path("C:/Users/a b/App Data/Cookies"))
        self.assertTrue(uri.startswith("file:///C:/"), uri)
        self.assertIn("App%20Data", uri)          # 空格必须转义
        self.assertTrue(uri.endswith("?mode=ro&immutable=1"), uri)


class TestChromiumValue(unittest.TestCase):
    def _key(self):
        return bytes(range(32))

    def test_v10_roundtrip(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key, nonce = self._key(), bytes(range(12))
        blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, b"sess-abc", None)
        self.assertEqual(vendor_session._chromium_value(blob, key), "sess-abc")

    def test_v20_is_reported_not_guessed(self):
        with self.assertRaises(vendor_session.ProfileUnavailable) as ctx:
            vendor_session._chromium_value(b"v20" + b"\x00" * 20, self._key())
        self.assertIn("v20", str(ctx.exception))

    def test_empty_blob_is_empty_string(self):
        self.assertEqual(vendor_session._chromium_value(b"", self._key()), "")


class TestChromiumRead(unittest.TestCase):
    """临时库 + 明文 value 列：不依赖 DPAPI，验证库定位与域名过滤。"""

    def _make_db(self, root: Path, rows) -> Path:
        db = root / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE cookies (name TEXT, value TEXT, encrypted_value BLOB,"
                    " host_key TEXT, last_access_utc INTEGER)")
        con.executemany("INSERT INTO cookies VALUES (?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return db

    def test_reads_plain_values_and_filters_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_db(root, [
                ("session_token", "plain-sess", b"", ".open-design.ai", 2),
                ("theme", "dark", b"", ".open-design.ai", 1),
                ("cookie", "other", b"", ".example.com", 3),
            ])
            got = vendor_session.read_cookies_chromium(
                "open-design", names=("session_token",), root=root)
            self.assertEqual(got, {"session_token": "plain-sess"})

    def test_missing_host_raises_profile_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_db(root, [("session_token", "x", b"", ".example.com", 1)])
            with self.assertRaises(vendor_session.ProfileUnavailable):
                vendor_session.read_cookies_chromium("open-design", root=root)

    def test_explicit_root_skips_other_browsers(self):
        """显式 root 时 read_cookies 只读那个 profile，不去扫本机浏览器。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(vendor_session.ProfileUnavailable):
                vendor_session.read_cookies("open-design", root=tmp)


if __name__ == "__main__":
    unittest.main()
