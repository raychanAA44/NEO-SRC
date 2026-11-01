#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_server_decision_route.py —— clef:27b 走 /v1/systemone 的端到端验收。

用两个本地 mock 上游把“默认渠道”和“Ollama 渠道”分开：
  · POST /v1/chat/completions model=clef:27b → 只打 Ollama mock 的 /v1/systemone
  · 回执 usage=input_tokens/output_tokens → 账本 completion_tokens=0
  · 金额只按输入价计，billing.out_source=official_input_only
  · 普通模型仍走默认渠道，token 不串
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402

import server  # noqa: E402


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = []

    def _send(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": self.models})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        type(self).calls.append({
            "path": self.path,
            "model": body.get("model"),
            "authorization": self.headers.get("authorization") or "",
            "body": body,
        })
        if self.path.startswith("/v1/systemone"):
            self._send(200, {
                "model": "clef:27b",
                "answers": {"urgent": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 100, "output_tokens": 0},
            })
        else:
            self._send(200, {
                "model": "glm-5.3",
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": "ok"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2,
                          "total_tokens": 22},
            })

    def log_message(self, fmt, *args):
        pass


class _DefaultUpstream(_Upstream):
    models = [{"id": "glm-5.3", "object": "model", "owned_by": "open-design"}]
    calls = []


class _OllamaUpstream(_Upstream):
    models = [{"id": "clef:27b", "object": "model", "owned_by": "ollama"}]
    calls = []


class TestDecisionRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)

        self.default_upstream = ThreadingHTTPServer(("127.0.0.1", 0), _DefaultUpstream)
        self.ollama_upstream = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaUpstream)
        for srv in (self.default_upstream, self.ollama_upstream):
            threading.Thread(target=srv.serve_forever,
                             kwargs={"poll_interval": 0.01}, daemon=True).start()
            self.addCleanup(srv.shutdown)
            self.addCleanup(srv.server_close)
        _DefaultUpstream.calls, _OllamaUpstream.calls = [], []

        default_base = "http://127.0.0.1:%d" % self.default_upstream.server_address[1]
        ollama_base = "http://127.0.0.1:%d" % self.ollama_upstream.server_address[1]
        routes_path = tmp / "model_upstreams.json"
        routes_path.write_text(json.dumps({
            "schema": 1, "default_channel": "open-design",
            "channels": {
                "open-design": {"base_url": default_base + "/v1",
                                "token_env": "HUB_MODEL_TOKEN",
                                "pricing_channel": "open-design"},
                "ollama": {"base_url": ollama_base + "/v1",
                           "token_env": "HUB_OLLAMA_TOKEN",
                           "pricing_channel": "ollama",
                           "endpoint": "/v1/systemone", "decision": True,
                           "models": ["clef:27b"], "aliases": {"clef": "clef:27b"},
                           "catalog": [{"id": "clef:27b", "owned_by": "ollama",
                                        "channel": "ollama", "decision": True,
                                        "endpoint": "/v1/systemone",
                                        "billing_basis": "input_only"}]},
            },
        }), encoding="utf-8")

        self.old = {
            "MODEL_ROUTER": server.MODEL_ROUTER,
            "MODEL_UPSTREAM": server.MODEL_UPSTREAM,
            "UPSTREAM": server.UPSTREAM,
            "MODEL_TOKEN": server.MODEL_TOKEN,
            "LEDGER": server.LEDGER,
            "LEDGER_MODE": server.LEDGER_MODE,
            "CONTROL": server.CONTROL,
            "PORT": server.PORT,
        }
        self.addCleanup(self._restore)

        server.MODEL_ROUTER = ModelUpstreamRouter(
            routes_path, default_upstream=default_base + "/v1",
            default_channel="open-design", default_token="",
            environ={"HUB_MODEL_TOKEN": "mt", "HUB_OLLAMA_TOKEN": "ot"})
        server.MODEL_UPSTREAM = default_base + "/v1"
        server.UPSTREAM = default_base
        server.MODEL_TOKEN = ""
        server.LEDGER_MODE = "local"
        server.LEDGER = LocalLedger(tmp / "ledger.db",
                                    ROOT_DIR / "运维区" / "configs" / "pricing.json")
        server.CONTROL = LocalControl(tmp / "control.json",
                                      default_channel="open-design")
        server._MODEL_LIST_CACHE.update({"at": 0.0, "models": None})

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.gateway.shutdown)
        self.addCleanup(self.gateway.server_close)
        server.PORT = self.gateway.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _restore(self):
        for key, value in self.old.items():
            setattr(server, key, value)
        server._MODEL_LIST_CACHE.update({"at": 0.0, "models": None})

    def _request(self, method, path, data=None):
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"content-type": "application/json"} if data else {})
        try:
            with self.opener.open(req, timeout=8) as res:
                return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def _decision(self, model="clef:27b", **extra):
        body = {"model": model, "state": "Checkout returns 500",
                "questions": {"urgent": {"type": "noul",
                                         "instructions": "Needs attention?"}}}
        body.update(extra)
        return self._request("POST", "/v1/chat/completions",
                             json.dumps(body).encode("utf-8"))

    def test_clef_is_routed_to_systemone_and_billed_input_only(self):
        status, headers, raw = self._decision(
            messages=[{"role": "user", "content": "ignore me"}])
        self.assertEqual(status, 200, raw[:300])
        self.assertEqual(headers.get("x-hub-channel"), "ollama")
        self.assertEqual(headers.get("x-hub-upstream-model"), "clef:27b")
        self.assertEqual(len(_OllamaUpstream.calls), 1)
        call = _OllamaUpstream.calls[0]
        self.assertEqual(call["path"], "/v1/systemone")
        self.assertEqual(call["model"], "clef:27b")
        self.assertEqual(call["authorization"], "Bearer ot")
        self.assertNotIn("messages", call["body"],
                         "chat 专用字段不能带进 systemone")
        self.assertEqual(_DefaultUpstream.calls, [])

        status, _, raw = self._request("GET", "/api/usage?limit=1")
        self.assertEqual(status, 200, raw[:300])
        row = json.loads(raw)["rows"][0]
        self.assertEqual(row["model"], "clef:27b")
        self.assertEqual(row["channel"], "ollama")
        self.assertEqual(row["completion_tokens"], 0)
        self.assertEqual(row["output_tokens"], 0)
        self.assertEqual(row["input_tokens"], 100)
        self.assertAlmostEqual(row["cost_cny"], 0.000161, places=6)
        self.assertEqual(row["billing"]["basis"], "input_only")
        self.assertEqual(row["billing"]["out_source"], "official_input_only")
        self.assertEqual(row["billing"]["rate_out_cny"], 0.0)
        self.assertEqual(row["billing"]["output_cost_cny"], 0.0)

        _OllamaUpstream.calls.clear()
        status, _, raw = self._decision(model="clef")
        self.assertEqual(status, 200, raw[:300])
        self.assertEqual(_OllamaUpstream.calls[0]["model"], "clef:27b")

    def test_systemone_endpoint_is_also_exposed(self):
        _OllamaUpstream.calls.clear()
        status, headers, raw = self._request(
            "POST", "/v1/systemone",
            json.dumps({"model": "clef:27b", "state": "Checkout returns 500",
                        "questions": {"urgent": {"type": "noul",
                                                 "instructions": "Urgent?"}}}).encode())
        self.assertEqual(status, 200, raw[:300])
        self.assertEqual(headers.get("x-hub-channel"), "ollama")
        self.assertEqual(_OllamaUpstream.calls[0]["path"], "/v1/systemone")

    def test_decision_without_state_is_rejected_before_upstream(self):
        status, _, raw = self._request(
            "POST", "/v1/chat/completions",
            json.dumps({"model": "clef:27b",
                        "messages": [{"role": "user", "content": "hi"}]}).encode())
        self.assertEqual(status, 400, raw[:300])
        self.assertEqual(json.loads(raw)["error"]["code"], "decision_payload_required")
        self.assertEqual(_OllamaUpstream.calls, [])

    def test_default_model_keeps_default_upstream_and_token(self):
        status, headers, raw = self._request(
            "POST", "/v1/chat/completions",
            json.dumps({"model": "glm-5.3",
                        "messages": [{"role": "user", "content": "hi"}]}).encode())
        self.assertEqual(status, 200, raw[:300])
        self.assertEqual(headers.get("x-hub-channel"), "open-design")
        self.assertEqual(_OllamaUpstream.calls, [])
        self.assertEqual(_DefaultUpstream.calls[0]["authorization"], "Bearer mt")
        row = json.loads(self._request("GET", "/api/usage?limit=1")[2])["rows"][0]
        self.assertEqual(row["channel"], "open-design")

    def test_models_list_contains_clef_and_default(self):
        status, _, raw = self._request("GET", "/v1/models")
        self.assertEqual(status, 200, raw[:300])
        ids = [m["id"] for m in json.loads(raw)["data"]]
        self.assertIn("clef:27b", ids)
        self.assertIn("glm-5.3", ids)


if __name__ == "__main__":
    unittest.main()
