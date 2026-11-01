#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_system_settings.py —— 面板设置后端：配置重载 / 缓存刷新 / 历史日志清理。"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server


class TestLogsClear(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runlogs_"))
        for i in range(8):
            path = self.tmp / ("run-2026102%d-00000%d.log" % (i, i))
            path.write_text("x" * (100 + i), encoding="utf-8")
            # 明确 mtime 顺序：数字小的更旧
            stamp = time.time() - (100 - i) * 10
            import os
            os.utime(path, (stamp, stamp))
        (self.tmp / "keep-me.txt").write_text("not a run log", encoding="utf-8")

    def test_keeps_newest_and_ignores_other_files(self):
        out = server.system_logs_clear(keep=5, log_dir=self.tmp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 3)
        self.assertEqual(out["kept"], 5)
        self.assertGreater(out["freed_bytes"], 0)
        left = sorted(p.name for p in self.tmp.glob("run-*.log"))
        self.assertEqual(len(left), 5)
        self.assertTrue((self.tmp / "keep-me.txt").exists(), "非 run-*.log 一律不动")
        newest = sorted(left)[-1]
        self.assertIn(newest, left)

    def test_keep_all_is_noop(self):
        out = server.system_logs_clear(keep=50, log_dir=self.tmp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 0)
        self.assertEqual(len(list(self.tmp.glob("run-*.log"))), 8)

    def test_missing_dir_reports_error_not_crash(self):
        out = server.system_logs_clear(keep=5, log_dir=self.tmp / "nope")
        self.assertTrue(out["ok"], "空目录没有可清的行，不算失败")
        self.assertEqual(out["removed"], 0)


class TestCacheClear(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "QUOTA": dict(server._QUOTA_CACHE),
            "LIST": dict(server._MODEL_LIST_CACHE),
            "NAMES": dict(server._MODEL_NAMES_CACHE),
            "BUDGET": dict(server._BUDGET_STATE),
        }
        self.addCleanup(self._restore)

    def _restore(self):
        server._QUOTA_CACHE.clear(); server._QUOTA_CACHE.update(self.saved["QUOTA"])
        server._MODEL_LIST_CACHE.clear(); server._MODEL_LIST_CACHE.update(self.saved["LIST"])
        server._MODEL_NAMES_CACHE.clear(); server._MODEL_NAMES_CACHE.update(self.saved["NAMES"])
        server._BUDGET_STATE.clear(); server._BUDGET_STATE.update(self.saved["BUDGET"])

    def test_clears_quota_and_budget_caches(self):
        server._QUOTA_CACHE["demo"] = (time.time(), {"source": "live"})
        server._MODEL_LIST_CACHE.update({"at": time.time(), "models": ["m1"]})
        server._BUDGET_STATE.update({"at": time.time(), "day": "2026-10-25",
                                     "total": 9.9, "channels": {"x": 9.9}})
        out = server.system_cache_clear()
        self.assertTrue(out["ok"])
        self.assertEqual(out["cleared"]["quota_channels"], 1)
        self.assertEqual(server._QUOTA_CACHE, {})
        self.assertIsNone(server._MODEL_LIST_CACHE["models"])
        self.assertEqual(server._BUDGET_STATE["total"], 0.0)
        self.assertEqual(server._BUDGET_STATE["channels"], {})


class TestSettingsHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = server._create_server(cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, data: dict, timeout: float = 20):
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_cache_clear_endpoint(self):
        status, body = self._post("/api/system/cache/clear", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("quota_channels", body["cleared"])

    def test_reload_endpoint_reports_channels(self):
        status, body = self._post("/api/system/reload", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("channels", body)
        self.assertIn("cache", body)

    def test_logs_clear_keep_all(self):
        status, body = self._post("/api/system/logs/clear", {"keep": 10 ** 6})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["removed"], 0)


if __name__ == "__main__":
    unittest.main()
