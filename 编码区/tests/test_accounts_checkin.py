#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_accounts_checkin.py —— 测试极简账号池与单向只读签到引擎。"""

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
import sys

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core import accounts
from core.local_ledger import LocalLedger
import server


class _DummyLedger:
    def __init__(self, db_path):
        self.db_path = db_path

    def connect(self):
        return sqlite3.connect(self.db_path)


class TestAccountsUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.ledger = _DummyLedger(self.tmp.name)
        accounts.ensure_accounts_table(self.ledger)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_import_and_mask_token(self):
        # 1. 贴 JSON 导入
        raw_json = json.dumps({
            "token": "sk-1234567890abcdef1234567890",
            "name": "wb-test-01",
            "realm": "cn"
        })
        res1 = accounts.import_text(self.ledger, "workbuddy", raw_json)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["token_masked"], "sk-1...7890")

        # 2. 贴 Bearer 纯文本导入
        raw_bearer = "Bearer token_abcdef_9876543210_xyz_123456"
        res2 = accounts.import_text(self.ledger, "trae", raw_bearer)
        self.assertTrue(res2["ok"])

        # 3. 列表核对
        accs = accounts.list_accounts(self.ledger)
        self.assertEqual(len(accs), 2)
        self.assertEqual(accs[0]["name"], "wb-test-01")
        self.assertEqual(accs[0]["status"], "active")

    def test_workbuddy_checkin_success(self):
        res = accounts.import_text(self.ledger, "workbuddy", "dummy_token_12345678901234567890")
        acc_id = res["id"]

        mock_ok = {"ok": True, "code": 0, "msg": "签到成功 +50 积分"}
        out = accounts.checkin_account(self.ledger, acc_id, mock_result=mock_ok)
        self.assertTrue(out["ok"])
        self.assertIn("签到成功", out["msg"])

        # 核对 last_checkin 写入
        accs = accounts.list_accounts(self.ledger)
        self.assertGreater(accs[0]["last_checkin"], 0)

    def test_trae_checkin_with_credits_readback(self):
        res = accounts.import_text(self.ledger, "trae", "dummy_token_trae_1234567890123456")
        acc_id = res["id"]

        mock_trae = {"ok": True, "code": 0, "msg": "success", "credits": 1250.0}
        out = accounts.checkin_account(self.ledger, acc_id, mock_result=mock_trae)
        self.assertTrue(out["ok"])
        self.assertEqual(out["credits"], 1250.0)

        accs = accounts.list_accounts(self.ledger)
        self.assertEqual(accs[0]["credits"], 1250.0)

    def test_failed_checkin_leaves_last_checkin_untouched(self):
        res = accounts.import_text(self.ledger, "workbuddy", "dummy_token_bad_123456789012345")
        acc_id = res["id"]

        # 模拟业务码非 0 失败
        mock_fail = {"ok": False, "code": 10001, "reason": "活动未开启或已过期"}
        out = accounts.checkin_account(self.ledger, acc_id, mock_result=mock_fail)
        self.assertFalse(out["ok"])

        # 铁律守门：失败决不能写 last_checkin
        accs = accounts.list_accounts(self.ledger)
        self.assertEqual(accs[0]["last_checkin"], 0.0)
        self.assertEqual(accs[0]["status"], "checkin_failed")

    def test_expired_token_never_refreshes(self):
        res = accounts.import_text(self.ledger, "trae", "dummy_token_expired_1234567890123")
        acc_id = res["id"]

        # 模拟 Token 失效分支
        mock_expired = {
            "ok": False,
            "status": "NEED_REFRESH",
            "reason": "HTTP 401 Unauthorized, please re-import token"
        }
        out = accounts.checkin_account(self.ledger, acc_id, mock_result=mock_expired)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "NEED_REFRESH")

        # 状态标为 need_refresh，绝不发起自动换取 refresh_token 流程
        accs = accounts.list_accounts(self.ledger)
        self.assertEqual(accs[0]["status"], "need_refresh")
        self.assertEqual(accs[0]["last_checkin"], 0.0)


class TestAccountsHTTP(unittest.TestCase):
    """HTTP 层只验端点接线：账本换成一次性临时库，签到打桩不出网。

    以前这组用例直接打进程内的真实 LEDGER（生产 MySQL），前一步
    /api/accounts/health 在真实账号上会把连接拖过客户端 3s 超时，
    后面的 inspect 就当作超时失败——是测试隔离问题，不是端点回归。
    """

    @classmethod
    def setUpClass(cls):
        import socket
        cls.tmpdir = tempfile.mkdtemp(prefix="hubneo-accounts-http-")
        cls.old_ledger = server.LEDGER
        cls.ledger = LocalLedger(Path(cls.tmpdir) / "ledger.db")
        accounts.ensure_accounts_table(cls.ledger)
        server.LEDGER = cls.ledger
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
        server.LEDGER = cls.old_ledger
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _post(self, path: str, data: dict, timeout: float = 3):
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_http_import_and_inspect_and_checkin(self):
        # 1. 导入账号
        st, data = self._post("/api/accounts/import_text", {
            "channel": "workbuddy",
            "realm": "cn",
            "name": "http-test-acc",
            "raw_text": "Bearer eyJhbGciOiJIUzI1NiJ9.test_payload.signature_here_12345"
        })
        self.assertEqual(st, 200)
        self.assertTrue(data.get("ok"))
        acc_id = data.get("id")

        # 2. 查询账号健康
        st, health = self._get("/api/accounts/health")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(health.get("total_accounts", 0), 1)

        # 3. 探针端点 inspect
        st, inspect_data = self._post("/api/accounts/inspect", {})
        self.assertEqual(st, 200)
        self.assertTrue(inspect_data.get("ok"))

        # 4. 签到端点 checkin：只验接线，官方端点用桩顶掉，不打真实渠道。
        stub = {"ok": True, "status": "checked", "msg": "签到成功 +50 积分", "credits": 50.0}
        with mock.patch.object(accounts, "checkin_account", return_value=stub):
            st, checkin_res = self._post("/api/accounts/checkin", {"account_id": acc_id}, timeout=5)
        self.assertEqual(st, 200)
        self.assertTrue(checkin_res.get("ok"))
        self.assertEqual(checkin_res.get("msg"), "签到成功 +50 积分")

    def test_http_delete_account(self):
        st, data = self._post("/api/accounts/import_text", {
            "channel": "trae", "name": "http-del-acc",
            "raw_text": "sk-http-delete-me-0123456789"})
        self.assertEqual(st, 200)
        acc_id = data["id"]

        st, out = self._post("/api/accounts/delete", {"id": acc_id})
        self.assertEqual(st, 200)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("deleted"), 1)

        _, health = self._get("/api/accounts/health")
        self.assertFalse(any(a["id"] == acc_id for a in health["accounts"]))

        # 再删同一行：如实报不存在，不静默成功
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/accounts/delete", {"id": acc_id})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
