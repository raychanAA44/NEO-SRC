#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_server_usage_join.py - 02 流水行与派发轨迹的一对一 rid 关联。

用本地 mock 模型上游跑完整 8888 链路：
  POST /v1/chat/completions（auto/ 分组）
    → 分流裁决登记 DispatchLedger（rid = X-Request-Id）
    → LocalLedger 记一行 usage（rid 相同）
    → GET /api/usage 读侧挂 hub_trace
断言 GROUP / LOCKED / TIER 角标所需字段真实存在，且本地模式不会被
上游 x-hub-rid 改写（本地账本的 join 键必须保持网关自己的请求号）。
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
from core.local_ledger import LocalLedger
from core.model_upstreams import ModelUpstreamRouter

GROUP = {
    "name": "重型编程组",
    "policy": {"task_lock": True, "smart_tier": True, "hedged_racing": False},
    "members": [
        {"channel": "workbuddy-intl", "id": "glm-5.3",
         "tier": "strong", "priority": 1, "enabled": True},
    ],
}


class _ModelUpstream(BaseHTTPRequestHandler):
    """最小模型上游：回 usage 与一个 x-hub-rid，验证本地模式不采纳后者。"""

    protocol_version = "HTTP/1.1"
    calls = []

    def _send(self, status, data, extra=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, {"data": [{"id": "glm-5.3"}]})
        elif self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            type(self).calls.append(json.loads(body.decode("utf-8")))
        except Exception:
            type(self).calls.append({})
        self._send(200, {
            "id": "mock-completion",
            "model": "glm-5.3",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }, {"x-hub-rid": "upstreamrid123456"})

    def log_message(self, fmt, *args):
        pass


class TestUsageDispatchJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.config_path = tmp / "model_groups.json"
        self.config_path.write_text(json.dumps({
            "version": "usage-join-1",
            "groups": {"auto/code-heavy": copy.deepcopy(GROUP)},
        }, ensure_ascii=False), encoding="utf-8")

        self.old = {
            "MODEL_GROUPS_PATH": server.MODEL_GROUPS_PATH,
            "UPSTREAM": server.UPSTREAM,
            "MODEL_ROUTER": server.MODEL_ROUTER,
            "MODEL_UPSTREAM": server.MODEL_UPSTREAM,
            "MODEL_TOKEN": server.MODEL_TOKEN,
            "LEDGER": server.LEDGER,
            "LEDGER_MODE": server.LEDGER_MODE,
            "PORT": server.PORT,
            "TOKEN": server.TOKEN,
        }
        self.addCleanup(self._restore)

        _ModelUpstream.calls = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _ModelUpstream)
        threading.Thread(target=self.upstream.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self._stop_upstream)

        upstream = "http://127.0.0.1:%d" % self.upstream.server_address[1]
        server.MODEL_GROUPS_PATH = self.config_path
        server.reload_model_groups(force=True)
        server.UPSTREAM = upstream
        server.MODEL_UPSTREAM = upstream
        # 生产路由表会把未列出的模型送往真实渠道（model_upstreams.json 的默认渠道），
        # 只换 MODEL_UPSTREAM 会被绕过 → 真打外网。用例必须换成本地空表。
        server.MODEL_ROUTER = ModelUpstreamRouter(
            tmp / "model_upstreams.json", default_upstream=upstream,
            default_channel="open-design", default_token="", environ={})
        server.MODEL_TOKEN = ""
        server.TOKEN = ""
        server.LEDGER_MODE = "local"
        server.LEDGER = LocalLedger(tmp / "ledger.db", tmp / "pricing.json")
        server.DISPATCH_LEDGER.clear()
        server.DISPATCHER.task_lock.clear()
        server._MODEL_LIST_CACHE.update({"at": 0.0, "models": None})
        self.addCleanup(server.DISPATCH_LEDGER.clear)
        self.addCleanup(server.DISPATCHER.task_lock.clear)

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        server.PORT = self.gateway.server_address[1]
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self._stop_gateway)
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _stop_upstream(self):
        self.upstream.shutdown()
        self.upstream.server_close()

    def _stop_gateway(self):
        self.gateway.shutdown()
        self.gateway.server_close()

    def _restore(self):
        for key, value in self.old.items():
            setattr(server, key, value)
        try:
            server.reload_model_groups(force=True)
        except ValueError:
            pass
        server._MODEL_LIST_CACHE.update({"at": 0.0, "models": None})

    def _request(self, method, path, headers=None, data=None):
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=5) as res:
                return (res.status,
                        {k.lower(): v for k, v in res.headers.items()},
                        res.read())
        except urllib.error.HTTPError as exc:
            return (exc.code,
                    {k.lower(): v for k, v in exc.headers.items()},
                    exc.read())

    def _chat(self, task_id):
        return self._request(
            "POST", "/v1/chat/completions",
            headers={"content-type": "application/json",
                     "x-hub-task-id": task_id},
            data=json.dumps({
                "model": "auto/code-heavy",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode("utf-8"))

    def test_usage_rows_join_dispatch_trace_by_rid(self):
        status, headers, raw = self._chat("usage-join-task")
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(headers.get("x-hub-group"), "auto/code-heavy")
        self.assertEqual(headers.get("x-hub-route-tier"), "strong")
        self.assertEqual(headers.get("x-hub-task-locked"), "0")
        self.assertNotIn("x-hub-rid", headers,
                         "本地账本模式不得被上游 x-hub-rid 改写 join 键")
        first_rid = headers["x-request-id"]
        first_trace = headers["x-hub-trace-id"]

        status, headers, raw = self._chat("usage-join-task")
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(headers.get("x-hub-task-locked"), "1")
        second_rid = headers["x-request-id"]
        second_trace = headers["x-hub-trace-id"]
        self.assertNotEqual(first_rid, second_rid)

        status, _, raw = self._request("GET", "/api/usage?limit=10")
        self.assertEqual(status, 200)
        rows = json.loads(raw)["rows"]
        by_rid = {r["rid"]: r for r in rows}
        self.assertIn(first_rid, by_rid)
        self.assertIn(second_rid, by_rid)

        first = by_rid[first_rid]
        self.assertEqual(first["hub_trace"]["trace_id"], first_trace)
        self.assertEqual(first["hub_trace"]["group_id"], "auto/code-heavy")
        self.assertEqual(first["hub_trace"]["tier"], "strong")
        self.assertFalse(first["hub_trace"]["locked"])

        second = by_rid[second_rid]
        self.assertEqual(second["hub_trace"]["trace_id"], second_trace)
        self.assertEqual(second["hub_trace"]["group_id"], "auto/code-heavy")
        self.assertEqual(second["hub_trace"]["tier"], "strong")
        self.assertTrue(second["hub_trace"]["locked"])
        self.assertEqual(second["hub_trace"]["lock_state"], "HIT")

    def test_head_usage_is_local_and_empty_body(self):
        self._chat("usage-head-task")
        status, headers, raw = self._request("HEAD", "/api/usage?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(headers["content-length"]), 0)


if __name__ == "__main__":
    unittest.main()
