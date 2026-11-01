#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_server_model_groups.py - 8888 网关模型分组查询、热重载与反代验收。

覆盖：
- /api/models 原样反代与 X-Forwarded-* / trace 头注入；
- /api/models/groups 本地查询；
- GET 自动热加载与 POST /api/models/groups/reload 强制热加载；
- 配置损坏时 503 且保留旧缓存；
- reload 的 loopback + admin token 鉴权；
- /health 分组状态与 HEAD 支持。
"""

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# 单测的 clear() 只清内存台账，不碰运维区/data 的生产轨迹库。
os.environ.setdefault("HUB_DISPATCH_PERSIST", "0")
import server

GROUP = {
    "name": "重型编程组",
    "policy": {"task_lock": True, "smart_tier": True, "hedged_racing": False},
    "members": [
        {"channel": "workbuddy-intl", "id": "glm-5.3",
         "tier": "strong", "priority": 1, "enabled": True},
    ],
}


class _UpstreamHandler(BaseHTTPRequestHandler):
    """最小上游：验证反代请求头，并返回稳定的 /api/models 数据。"""

    protocol_version = "HTTP/1.1"
    seen = []

    def _send(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).seen.append(dict(self.headers))
        if self.path.startswith("/api/models"):
            self._send(200, {"mock-channel": [{"id": "mock-model", "name": "Mock"}]})
        elif self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


class TestServerModelGroups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "model_groups.json"
        self._write_config(self._payload())

        self.old_path = server.MODEL_GROUPS_PATH
        self.old_upstream = server.UPSTREAM
        self.old_token = server.TOKEN
        self.old_port = server.PORT
        self.addCleanup(self._restore_server_globals)

        _UpstreamHandler.seen = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        threading.Thread(target=self.upstream.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self._stop_upstream)

        server.UPSTREAM = "http://127.0.0.1:%d" % self.upstream.server_address[1]
        server.TOKEN = ""
        server.MODEL_GROUPS_PATH = self.config_path
        server.reload_model_groups(force=True)

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        server.PORT = self.gateway.server_address[1]
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self._stop_gateway)

        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _stop_gateway(self):
        self.gateway.shutdown()
        self.gateway.server_close()

    def _stop_upstream(self):
        self.upstream.shutdown()
        self.upstream.server_close()

    def _restore_server_globals(self):
        server.MODEL_GROUPS_PATH = self.old_path
        server.UPSTREAM = self.old_upstream
        server.TOKEN = self.old_token
        server.PORT = self.old_port
        try:
            server.reload_model_groups(force=True)
        except ValueError:
            pass

    def _write_config(self, payload):
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _payload(version="test-1", groups=None):
        return {
            "version": version,
            "groups": groups or {"auto/code-heavy": copy.deepcopy(GROUP)},
        }

    def _request(self, method, path, headers=None, data=None):
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=5) as resp:
                return (resp.status,
                        {k.lower(): v for k, v in resp.headers.items()},
                        resp.read())
        except urllib.error.HTTPError as exc:
            return (exc.code,
                    {k.lower(): v for k, v in exc.headers.items()},
                    exc.read())

    def test_reverse_proxy_forwards_required_headers(self):
        status, headers, raw = self._request(
            "GET", "/api/models", headers={"X-Request-Id": "trace-abc"})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw),
                         {"mock-channel": [{"id": "mock-model", "name": "Mock"}]})
        self.assertEqual(headers.get("x-request-id"), "trace-abc")

        seen = {k.lower(): v for k, v in _UpstreamHandler.seen[-1].items()}
        self.assertTrue(seen["x-forwarded-for"].startswith("127."))
        self.assertTrue(seen["x-real-ip"].startswith("127."))
        self.assertEqual(seen["x-forwarded-proto"], "http")
        self.assertEqual(seen["x-request-id"], "trace-abc")
        # Host 必须指向上游，不能用网关自身 Host；否则云端虚拟主机/Cloudflare 会 403。
        self.assertEqual(
            seen["host"],
            "127.0.0.1:%d" % self.upstream.server_address[1],
        )
        # 原始客户端 Host 通过 X-Forwarded-Host 保留。
        self.assertEqual(
            seen["x-forwarded-host"],
            "127.0.0.1:%d" % self.gateway.server_address[1],
        )

    def test_groups_query_is_served_locally(self):
        status, _, raw = self._request("GET", "/api/models/groups")

        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["version"], "test-1")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["groups"][0]["group_id"], "auto/code-heavy")
        self.assertEqual(data["groups"][0]["members"][0]["name"],
                         "workbuddy-intl/glm-5.3")
        self.assertEqual(_UpstreamHandler.seen, [], "分组查询不得透传到上游")

    def test_dispatch_traces_empty_seeded_and_head(self):
        server.DISPATCH_LEDGER.clear()
        self.addCleanup(server.DISPATCH_LEDGER.clear)

        status, _, raw = self._request("GET", "/api/dispatch/traces")
        self.assertEqual(status, 200)
        empty = json.loads(raw)
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["traces"], [])

        trace = server.DISPATCH_LEDGER.start(
            rid="rid-trace-1", task_id="task-1", requested_model="auto/code-heavy",
            model="workbuddy-intl/glm-5.3", group_id="auto/code-heavy",
            locked=False, tier="strong", priority=1,
            policy={"task_lock": True}, lock_state="BOUND", lock_ttl=100,
            dispatch_ms=0.2)
        server.DISPATCH_LEDGER.finish(trace["trace_id"], status=400, ms=12,
                                      response_bytes=8, error="bad request")

        status, _, raw = self._request("GET", "/api/dispatch/traces?limit=10")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["traces"][0]["trace_id"], trace["trace_id"])
        self.assertEqual(data["traces"][0]["group_id"], "auto/code-heavy")
        self.assertEqual(data["traces"][0]["upstream_status"], 400)

        status, headers, raw = self._request("HEAD", "/api/dispatch/traces")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    def test_resolve_group_model_records_trace_and_headers(self):
        server.DISPATCH_LEDGER.clear()
        server.DISPATCHER.task_lock.clear()
        self.addCleanup(server.DISPATCH_LEDGER.clear)
        self.addCleanup(server.DISPATCHER.task_lock.clear)

        class _Handler:
            path = "/api/models/bench"
            headers = {"x-hub-task-id": "trace-task"}
            _dispatch_trace = None
            sent = []

            def send_header(self, key, value):
                self.sent.append((key, value))

            def _json(self, status, data, head_only=False):
                raise AssertionError("分组已定义，不应直接应答")

        handler = _Handler()
        body = json.dumps({"model": "auto/code-heavy", "messages": []}).encode("utf-8")
        out = server.HubNeoHandler._resolve_group_model(handler, body, "rid-trace-1")
        payload = json.loads(out)
        # 分组下发名优先 upstream_name（裸名上游），缺省回退 canonical name。
        self.assertEqual(payload["model"], "workbuddy-intl/glm-5.3")
        first = handler._dispatch_trace
        self.assertEqual(first["group_id"], "auto/code-heavy")
        self.assertEqual(first["lock_state"], "BOUND")
        self.assertEqual(first["path"], "/api/models/bench")
        self.assertEqual(first["tier"], "strong")
        self.assertEqual(first["priority"], 1)
        self.assertGreater(first["lock_ttl"], 0)

        handler.sent = []
        server.HubNeoHandler._emit_dispatch_headers(handler)
        sent = dict(handler.sent)
        self.assertEqual(sent["X-Hub-Group"], "auto/code-heavy")
        self.assertEqual(sent["X-Hub-Model"], "workbuddy-intl/glm-5.3")
        self.assertEqual(sent["X-Hub-Task-Locked"], "0")
        self.assertEqual(sent["X-Hub-Route-Tier"], "strong")
        self.assertEqual(sent["X-Hub-Trace-Id"], first["trace_id"])

        server.HubNeoHandler._resolve_group_model(handler, body, "rid-trace-2")
        second = handler._dispatch_trace
        self.assertTrue(second["locked"])
        self.assertEqual(second["lock_state"], "HIT")
        self.assertEqual(second["tier"], "strong")
        self.assertEqual(server.DISPATCH_LEDGER.count(), 2)

    def test_bind_upstream_rid_keeps_local_request_id(self):
        server.DISPATCH_LEDGER.clear()
        self.addCleanup(server.DISPATCH_LEDGER.clear)
        trace = server.DISPATCH_LEDGER.start(
            rid="local-req-1", task_id="task-1", requested_model="auto/code-heavy",
            model="workbuddy-intl/glm-5.3", group_id="auto/code-heavy",
            locked=False, tier="strong", policy={"task_lock": True},
            lock_state="BOUND")

        class _Handler:
            _dispatch_trace = trace
            sent = []

            def send_header(self, key, value):
                self.sent.append((key, value))

        handler = _Handler()
        server.HubNeoHandler._bind_dispatch_rid(handler, "upstream-rid-9")
        updated = handler._dispatch_trace
        self.assertEqual(updated["rid"], "upstream-rid-9")
        self.assertEqual(updated["request_id"], "local-req-1")
        self.assertEqual(updated["upstream_rid"], "upstream-rid-9")

        handler.sent = []
        server.HubNeoHandler._emit_dispatch_headers(handler)
        sent = dict(handler.sent)
        self.assertEqual(sent["X-Hub-Rid"], "upstream-rid-9")
        self.assertEqual(sent["X-Hub-Trace-Id"], trace["trace_id"])

    def test_attach_usage_traces_only_on_exact_rid(self):
        server.DISPATCH_LEDGER.clear()
        self.addCleanup(server.DISPATCH_LEDGER.clear)
        trace = server.DISPATCH_LEDGER.start(
            rid="rid-match", task_id="task-1", requested_model="auto/code-heavy",
            model="workbuddy-intl/glm-5.3", group_id="auto/code-heavy",
            locked=True, tier="strong", policy={"task_lock": True},
            lock_state="HIT", lock_ttl=100)
        payload = json.dumps({"rows": [
            {"rid": "rid-match", "model": "workbuddy-intl/glm-5.3"},
            {"rid": "rid-other", "model": "workbuddy-intl/glm-5.3"},
            {"uid": "rid-match", "model": "workbuddy-intl/glm-5.3"},
        ]}).encode("utf-8")

        out = server.HubNeoHandler._attach_usage_traces(payload)
        rows = json.loads(out)["rows"]
        self.assertEqual(rows[0]["hub_trace"]["trace_id"], trace["trace_id"])
        self.assertNotIn("hub_trace", rows[1], "不同 rid 不得挂轨迹")
        self.assertNotIn("hub_trace", rows[2], "uid 不是请求号，不能冒充 rid")

    def test_get_auto_reloads_changed_config(self):
        groups = {
            "auto/code-heavy": copy.deepcopy(GROUP),
            "auto/smart-fast": copy.deepcopy(GROUP),
        }
        groups["auto/smart-fast"]["name"] = "快响应组"
        self._write_config(self._payload(version="test-2", groups=groups))

        status, _, raw = self._request("GET", "/api/models/groups")

        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["version"], "test-2")
        self.assertEqual(data["count"], 2)

    def test_explicit_reload_endpoint(self):
        groups = {
            "auto/code-heavy": copy.deepcopy(GROUP),
            "auto/smart-fast": copy.deepcopy(GROUP),
        }
        self._write_config(self._payload(version="test-3", groups=groups))

        status, _, raw = self._request("POST", "/api/models/groups/reload", data=b"")

        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["reloaded"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["version"], "test-3")

    def test_reload_requires_admin_token(self):
        server.TOKEN = "test-token"

        status, _, _ = self._request("POST", "/api/models/groups/reload", data=b"")
        self.assertEqual(status, 403)

        status, _, _ = self._request(
            "POST", "/api/models/groups/reload", data=b"",
            headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 403)

        status, _, raw = self._request(
            "POST", "/api/models/groups/reload", data=b"",
            headers={"Authorization": "Bearer test-token"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["reloaded"])

    def test_invalid_config_returns_503_and_keeps_cache(self):
        self.config_path.write_text("{ broken json", encoding="utf-8")

        status, _, raw = self._request("GET", "/api/models/groups")
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.config_path), raw.decode("utf-8"))
        self.assertEqual(server.model_groups_snapshot()["count"], 1)

        groups = {
            "auto/code-heavy": copy.deepcopy(GROUP),
            "auto/smart-fast": copy.deepcopy(GROUP),
        }
        self._write_config(self._payload(version="test-4", groups=groups))
        status, _, raw = self._request("GET", "/api/models/groups")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["count"], 2)

    def test_health_reports_model_groups(self):
        status, _, raw = self._request("GET", "/health")

        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["model_groups"]["count"], 1)
        self.assertEqual(data["model_groups"]["version"], "test-1")
        self.assertIsNone(data["model_groups"]["error"])
        self.assertEqual(data["write_endpoints"], 15)

    def test_head_groups(self):
        status, headers, raw = self._request("HEAD", "/api/models/groups")

        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    def _post(self, path, payload, origin=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        return self._request("POST", path, headers=headers, data=body)

    def test_groups_save_updates_config_and_reloads(self):
        payload = {
            "group_id": "auto/code-heavy",
            "name": "重型编程组",
            "policy": {"task_lock": True, "smart_tier": False, "hedged_racing": False},
            "members": [
                {"channel": "workbuddy-intl", "id": "glm-5.3",
                 "tier": "strong", "priority": 1, "enabled": True},
                {"channel": "custom:novita", "id": "deepseek-v4.1-flash",
                 "tier": "weak", "priority": 2, "enabled": False},
            ],
        }
        status, _, raw = self._post("/api/groups/save", payload, origin=self.base)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["saved"], "auto/code-heavy")
        self.assertEqual(data["members"], 2)
        self.assertFalse(data["groups"][0]["policy"]["smart_tier"])

        disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        members = disk["groups"]["auto/code-heavy"]["members"]
        self.assertEqual([m["name"] for m in members],
                         ["workbuddy-intl/glm-5.3",
                          "custom:novita/deepseek-v4.1-flash"])
        self.assertFalse(members[1]["enabled"])
        self.assertEqual(server.model_groups_snapshot()["groups"][0]["group_id"],
                         "auto/code-heavy")

    def test_groups_save_rejects_foreign_origin(self):
        payload = {
            "group_id": "auto/code-heavy",
            "policy": {"task_lock": True},
            "members": [{"channel": "workbuddy-intl", "id": "glm-5.3"}],
        }
        status, _, _ = self._post("/api/groups/save", payload,
                                  origin="http://evil.example")
        self.assertEqual(status, 403)
        disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["groups"]["auto/code-heavy"]["members"][0]["id"],
                         "glm-5.3")

    def test_groups_save_rejects_invalid_member(self):
        payload = {
            "group_id": "auto/code-heavy",
            "policy": {},
            "members": [{"channel": "workbuddy-intl", "id": ""}],
        }
        status, _, raw = self._post("/api/groups/save", payload, origin=self.base)
        self.assertEqual(status, 400)
        self.assertIn("非法", json.loads(raw)["error"]["message"])

    def test_groups_save_rejects_duplicate_member(self):
        member = {"channel": "workbuddy-intl", "id": "glm-5.3"}
        payload = {
            "group_id": "auto/code-heavy",
            "policy": {},
            "members": [dict(member), dict(member)],
        }
        status, _, raw = self._post("/api/groups/save", payload, origin=self.base)
        self.assertEqual(status, 400)
        self.assertIn("重复", json.loads(raw)["error"]["message"])

    def test_task_locks_query_and_release(self):
        server.DISPATCHER.task_lock.clear()
        self.addCleanup(server.DISPATCHER.task_lock.clear)
        server.DISPATCHER.task_lock.bind(
            "task-1", "auto/code-heavy", "workbuddy-intl/glm-5.3", ttl=120)

        status, _, raw = self._request("GET", "/api/task_locks")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["count"], 1)
        lock = data["locks"][0]
        self.assertEqual(lock["task_id"], "task-1")
        self.assertEqual(lock["group_id"], "auto/code-heavy")
        self.assertEqual(lock["model"], "workbuddy-intl/glm-5.3")
        self.assertGreater(lock["ttl_remaining"], 0)

        status, _, raw = self._post(
            "/api/task_locks/release",
            {"task_id": "task-1", "group_id": "auto/code-heavy"},
            origin=self.base)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["released"])
        self.assertEqual(json.loads(raw)["remaining"], 0)

        status, _, raw = self._request("GET", "/api/task_locks")
        self.assertEqual(json.loads(raw)["count"], 0)

    def test_task_lock_release_rejects_missing_ids(self):
        status, _, raw = self._post(
            "/api/task_locks/release", {"task_id": "task-1"}, origin=self.base)
        self.assertEqual(status, 400)
        self.assertIn("task_id", json.loads(raw)["error"]["message"])

    def test_missing_config_reload_returns_500(self):
        server.MODEL_GROUPS_PATH = Path(self.tmp.name) / "missing.json"

        status, _, raw = self._request("POST", "/api/models/groups/reload", data=b"")

        self.assertEqual(status, 500)
        self.assertIn("无法读取模型分组配置", json.loads(raw)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
