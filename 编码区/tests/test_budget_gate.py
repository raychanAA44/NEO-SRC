#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_budget_gate.py - 预算硬闸门端到端回归（真实 8888 handler + 本地 mock 上游）。

覆盖：
  · 未设上限：/v1 正常放行，上游被打到
  · 今日支出 ≥ 上限：402 budget_exceeded，且上游一次都没被打（闸门在反代之前）
  · 清空上限：立即恢复放行
  · 只拦推理 POST：GET /v1/models 不受预算影响
  · /health 暴露 control.budget 现状（spent / blocked / caps）
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

import server  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


class _MockUpstream(BaseHTTPRequestHandler):
    calls = []

    def _send(self, status, doc=None):
        payload = json.dumps(doc or {"ok": True}).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models"):
            self._send(200, {"data": [{"id": "glm-5.3"}]})
        else:
            self._send(200, {"status": "ok"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            type(self).calls.append(json.loads(body.decode("utf-8")))
        except ValueError:
            type(self).calls.append({})
        self._send(200, {
            "id": "mock-completion",
            "model": "glm-5.3",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    def log_message(self, *args):
        pass


class TestBudgetGateHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        pricing = tmp / "pricing.json"
        pricing.write_text(json.dumps({"models": {
            "glm-5.3": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")

        self.ledger = LocalLedger(tmp / "ledger.db", pricing)
        self.ctl = LocalControl(tmp / "control.json", default_channel="open-design")

        self.old = {
            "LEDGER": server.LEDGER,
            "CONTROL": server.CONTROL,
            "MODEL_ROUTER": server.MODEL_ROUTER,
            "MODEL_UPSTREAM": server.MODEL_UPSTREAM,
            "UPSTREAM": server.UPSTREAM,
            "MODEL_TOKEN": server.MODEL_TOKEN,
            "TOKEN": server.TOKEN,
            "LEDGER_MODE": server.LEDGER_MODE,
        }
        self.addCleanup(self._restore)

        _MockUpstream.calls = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
        threading.Thread(target=self.upstream.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self._stop_upstream)

        upstream = "http://127.0.0.1:%d" % self.upstream.server_address[1]
        server.LEDGER = self.ledger
        server.CONTROL = self.ctl
        server.UPSTREAM = upstream
        server.MODEL_UPSTREAM = upstream
        # 生产路由表会把未列出的模型送往真实渠道（model_upstreams.json 的默认渠道），
        # 只换 MODEL_UPSTREAM 会被绕过 → 真打外网并回 401。用例必须换成本地空表。
        server.MODEL_ROUTER = ModelUpstreamRouter(
            tmp / "model_upstreams.json", default_upstream=upstream,
            default_channel="open-design", default_token="", environ={})
        server.MODEL_TOKEN = ""
        server.TOKEN = ""
        server.LEDGER_MODE = "local"
        server.reset_budget_cache()

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
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
        server.reset_budget_cache()

    @staticmethod
    def _parse(raw):
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {"_raw": raw[:200].decode("utf-8", "replace")}

    def _request(self, method, path, headers=None, data=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=5) as res:
                return res.status, self._parse(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._parse(exc.read())

    def _chat(self):
        return self._request(
            "POST", "/v1/chat/completions",
            headers={"content-type": "application/json"},
            data=json.dumps({"model": "glm-5.3",
                             "messages": [{"role": "user", "content": "hi"}]}
                            ).encode("utf-8"))

    def _overspend(self):
        """记一笔超过上限的账：10M 输入 token ≈ ¥7，再设 ¥5 上限。"""
        self.ledger.record(model="glm-5.3", prompt=10_000_000, completion=0,
                           at=int(time.time()) - 5, ok=True, status=200,
                           channel="open-design", rid="overspend-rid")
        self.ctl.set_budget("global", 5)
        server.reset_budget_cache()

    def test_under_cap_passes_through(self):
        self.ctl.set_budget("global", 5)
        server.reset_budget_cache()
        status, doc = self._chat()
        self.assertEqual(status, 200, doc)
        self.assertEqual(len(_MockUpstream.calls), 1, "有额度就必须真的打到上游")

    def test_over_cap_blocks_before_upstream(self):
        self._overspend()
        status, doc = self._chat()
        self.assertEqual(status, 402, doc)
        err = doc["error"]
        self.assertEqual(err["code"], "budget_exceeded")
        self.assertEqual(err["key"], "global")
        self.assertEqual(err["cap_cny"], 5.0)
        self.assertGreaterEqual(err["spent_cny"], 5.0)
        self.assertEqual(err["period"], "day")
        self.assertEqual(_MockUpstream.calls, [],
                         "超限请求不得打上游（包括提示词自愈）")

    def test_clearing_cap_restores_traffic(self):
        self._overspend()
        self.assertEqual(self._chat()[0], 402)
        self.ctl.set_budget("global", None)
        server.reset_budget_cache()
        status, doc = self._chat()
        self.assertEqual(status, 200, doc)
        self.assertEqual(len(_MockUpstream.calls), 1)

    def test_zero_cap_blocks_everything(self):
        self.ctl.set_budget("global", 0)
        server.reset_budget_cache()
        self.assertEqual(self._chat()[0], 402)

    def test_model_listing_not_gated(self):
        """预算只拦推理 POST；读取类请求不该被当成花钱动作拒掉。"""
        self._overspend()
        status, doc = self._request("GET", "/v1/models")
        self.assertNotEqual(status, 402, doc)
        self.assertNotEqual((doc.get("error") or {}).get("code"), "budget_exceeded")

    def test_health_reports_warn_line(self):
        """80% 预警要在 /health 里看得到，不用等撞上限。"""
        self.ledger.record(model="glm-5.3", prompt=1_000_000, completion=0,
                           at=int(time.time()) - 5, ok=True, status=200,
                           channel="open-design", rid="warn-rid")
        total, _ = self.ledger.spend_since(0)
        self.ctl.set_budget("global", round(total / 0.9, 4))
        server.reset_budget_cache()
        budget = self._request("GET", "/health")[1]["control"]["budget"]
        self.assertFalse(budget["blocked"])
        self.assertTrue(budget["warn"], "到 80% 就要在 /health 标出来")
        self.assertEqual(budget["warn_pct"], 80)
        self.assertGreaterEqual(budget["used_pct"], 80)
        # 预警不等于拦截：请求照样放行
        self.assertEqual(self._chat()[0], 200)

    def test_health_reports_gate(self):
        self.ctl.set_budget("global", 5)
        server.reset_budget_cache()
        status, doc = self._request("GET", "/health")
        self.assertEqual(status, 200)
        budget = doc["control"]["budget"]
        self.assertTrue(budget["enforced"])
        self.assertEqual(budget["cap_count"], 1)
        self.assertEqual(budget["caps"][0]["key"], "global")
        self.assertFalse(budget["blocked"])
        self._overspend()
        self.assertTrue(self._request("GET", "/health")[1]["control"]["budget"]["blocked"])


if __name__ == "__main__":
    unittest.main()
