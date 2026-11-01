#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_accounts_crypt.py —— 账号凭据加密落库（复用保险仓）与删除入口。"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core import accounts


class _DummyLedger:
    def __init__(self, db_path):
        self.db_path = db_path

    def connect(self):
        return sqlite3.connect(self.db_path)


class TestEncryptedAtRest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.ledger = _DummyLedger(self.tmp.name)
        accounts.ensure_accounts_table(self.ledger)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _raw(self, acc_id):
        con = sqlite3.connect(self.tmp.name)
        try:
            row = con.execute("SELECT creds_encrypted FROM accounts WHERE id = ?",
                              (acc_id,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def test_plaintext_token_never_reaches_disk(self):
        token = "sk-plain-never-on-disk-0123456789"
        res = accounts.import_text(self.ledger, "workbuddy", token, name="crypt-1")
        self.assertTrue(res["ok"])
        blob = self._raw(res["id"])
        self.assertNotIn(token, blob, "明文令牌不得出现在库内")
        doc = json.loads(blob)
        self.assertEqual(doc.get("enc"), accounts.CREDS_FORMAT)
        self.assertEqual({"salt", "nonce", "ct", "tag"} - set(doc["blob"]), set())

    def test_roundtrip_unseals_and_masks(self):
        token = "sk-plain-never-on-disk-0123456789"
        res = accounts.import_text(self.ledger, "workbuddy", token, name="crypt-2")
        acc = accounts.list_accounts(self.ledger)[0]
        self.assertEqual(acc["name"], "crypt-2")
        self.assertEqual(acc["token_masked"], accounts.mask_token(token))
        self.assertEqual(accounts.open_creds(self._raw(res["id"]))["token"], token)

    def test_legacy_plaintext_row_still_readable(self):
        """历史明文行（旧格式）仍能读，不因升级丢账号。"""
        token = "legacy-token-abcdefghijklmnop"
        con = sqlite3.connect(self.tmp.name)
        try:
            con.execute(
                "INSERT INTO accounts (channel, realm, name, creds_encrypted, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("trae", "default", "legacy-1",
                 json.dumps({"token": token, "extra": {}}), time.time()))
            con.commit()
        finally:
            con.close()
        acc = accounts.list_accounts(self.ledger)[0]
        self.assertEqual(acc["name"], "legacy-1")
        self.assertEqual(acc["token_masked"], accounts.mask_token(token))

    def test_broken_ciphertext_reads_empty_not_garbage(self):
        res = accounts.import_text(self.ledger, "trae", "sk-broken-cipher-0123456789", name="c-3")
        doc = json.loads(self._raw(res["id"]))
        doc["blob"]["tag"] = doc["blob"]["tag"][:-4] + "AAAA"
        con = sqlite3.connect(self.tmp.name)
        try:
            con.execute("UPDATE accounts SET creds_encrypted = ? WHERE id = ?",
                        (json.dumps(doc), res["id"]))
            con.commit()
        finally:
            con.close()
        acc = accounts.list_accounts(self.ledger)[0]
        self.assertEqual(acc["token_masked"], "***", "解不开就回空，不猜")

    def test_migration_seals_plaintext_rows_once(self):
        """启动迁移：明文行转密文，密文行与已解封值不动，重复跑是幂等的。"""
        con = sqlite3.connect(self.tmp.name)
        try:
            con.execute(
                "INSERT INTO accounts (channel, realm, name, creds_encrypted, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("trae", "default", "legacy-json",
                 json.dumps({"token": "legacy-token-json-0123456789", "extra": {}}), time.time()))
            con.execute(
                "INSERT INTO accounts (channel, realm, name, creds_encrypted, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("trae", "default", "legacy-raw", "raw-token-bare-0123456789", time.time()))
            con.commit()
        finally:
            con.close()
        fresh = accounts.import_text(self.ledger, "trae",
                                     "sk-already-sealed-0123456789", name="sealed-1")["id"]
        sealed_before = self._raw(fresh)

        out = accounts.migrate_plaintext_creds(self.ledger)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["migrated"]), 2)
        self.assertEqual(out["failed"], [])
        self.assertEqual(self._raw(fresh), sealed_before, "已是密文的行不重写")

        tokens = {a["name"]: accounts.open_creds(self._raw(a["id"]))["token"]
                  for a in accounts.list_accounts(self.ledger)}
        self.assertEqual(tokens["legacy-json"], "legacy-token-json-0123456789")
        self.assertEqual(tokens["legacy-raw"], "raw-token-bare-0123456789")
        for row in ("legacy-json", "legacy-raw"):
            acc = [a for a in accounts.list_accounts(self.ledger) if a["name"] == row][0]
            self.assertEqual(json.loads(self._raw(acc["id"]))["enc"], accounts.CREDS_FORMAT)

        again = accounts.migrate_plaintext_creds(self.ledger)
        self.assertEqual(again["migrated"], [], "第二次跑不应再改任何行")

    def test_delete_removes_exactly_one_row(self):
        keep = accounts.import_text(self.ledger, "trae", "sk-keep-row-0123456789", name="keep")
        gone = accounts.import_text(self.ledger, "trae", "sk-gone-row-0123456789", name="gone")
        out = accounts.delete_account(self.ledger, gone["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["deleted"], 1)
        names = [a["name"] for a in accounts.list_accounts(self.ledger)]
        self.assertEqual(names, ["keep"])
        missing = accounts.delete_account(self.ledger, gone["id"])
        self.assertFalse(missing["ok"])


if __name__ == "__main__":
    unittest.main()
