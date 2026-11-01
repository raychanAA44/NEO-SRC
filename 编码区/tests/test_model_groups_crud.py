#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_model_groups_crud.py —— 04 路由分组新建/删除与策略调参回归。

覆盖：
- POST /api/groups/create 建空分组 → 目录可见 → DELETE 摘除；
- 非法 group_id / 重复创建被拒；
- POST /api/routes/policy 落 control.json 的 routing 段，并即时改限流速率；
- GET /api/routes/policy 回读一致。
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
from core.local_control import LocalControl  # noqa: E402


class TestGroupCrudAndPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.groups_path = root / "model_groups.json"
        self.groups_path.write_text(json.dumps({
            "version": "2.0.0",
            "groups": {
                "auto/existing": {
                    "name": "既有分组",
                    "policy": {"task_lock": True},
                    "members": [],
                }
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.control_path = root / "control.json"
        self.old_groups = server.MODEL_GROUPS_PATH
        self.old_control = server.CONTROL
        self.old_rate = (server.RATE_LIMITER.rate, server.RATE_LIMITER.burst)
        server.MODEL_GROUPS_PATH = str(self.groups_path)
        server.CONTROL = LocalControl(self.control_path, default_channel="open-design")
        server.reload_model_groups(force=True)
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.MODEL_GROUPS_PATH = self.old_groups
        server.CONTROL = self.old_control
        server.RATE_LIMITER.set_rate(*self.old_rate)
        server.reload_model_groups(force=True)

    def _call(self, path, payload=None, method="POST"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_create_and_delete_group(self):
        status, body = self._call("/api/groups/create", {
            "group_id": "auto/code-fast", "name": "快速编程组",
            "endpoint": "/v1/chat/completions"})
        self.assertEqual(status, 200)
        self.assertEqual(body["created"], "auto/code-fast")
        self.assertEqual(body["endpoint"], "/v1/chat/completions")

        status, groups = self._call("/api/models/groups", method="GET")
        ids = [g["group_id"] for g in groups["groups"]]
        self.assertIn("auto/code-fast", ids)
        self.assertIn("auto/existing", ids)

        status, body = self._call("/api/groups/auto%2Fcode-fast", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], "auto/code-fast")
        status, groups = self._call("/api/models/groups", method="GET")
        self.assertNotIn("auto/code-fast", [g["group_id"] for g in groups["groups"]])

    def test_rejects_duplicate_and_illegal_id(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/groups/create", {"group_id": "auto/existing"})
        self.assertEqual(ctx.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/groups/create", {"group_id": "code-fast"})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/groups/auto%2Fmissing", method="DELETE")
        self.assertEqual(ctx.exception.code, 404)

    def test_policy_roundtrip_and_rate_limit(self):
        status, body = self._call("/api/routes/policy", {
            "timeout_s": 90, "max_retries": 2,
            "sticky_ttl_s": 600, "rate_limit_rps": 50})
        self.assertEqual(status, 200)
        self.assertEqual(body["routing"]["timeout_s"], 90.0)
        self.assertEqual(body["rate_limit"]["rps"], 50.0)

        doc = json.loads(self.control_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["routing"], {"timeout_s": 90.0, "max_retries": 2,
                                          "sticky_ttl_s": 600, "rate_limit_rps": 50,
                                          "rate_limit_burst": 150})
        self.assertEqual(server.RATE_LIMITER.rate, 50.0)

        status, body = self._call("/api/routes/policy", method="GET")
        self.assertEqual(body["routing"]["max_retries"], 2)
        self.assertEqual(body["rate_limit"]["rps"], 50.0)

    def test_policy_rejects_out_of_range(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/routes/policy", {"max_retries": 9})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._call("/api/routes/policy", {"timeout_s": 0})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
