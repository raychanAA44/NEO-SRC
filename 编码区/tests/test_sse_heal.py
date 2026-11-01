# -*- coding: utf-8 -*-
"""编码区/tests/test_sse_heal.py —— 流式 SSE 自愈 + 工具命令审计。

覆盖：
  · `tool_ast` 的语法级判定（rm -rf / 管道喂 shell / 敏感路径 / 内联脚本 fail-closed）；
  · `Healer` 的三态：shadow 字节零变化、enforce 正常流零新增帧、截断流补帧合法；
  · 单帧完整危险命令就地改写；跨帧分片只如实记 `tool_unrewritable`；
  · 非流式回执同一套审计（shadow 只计数、enforce 改写）；
  · HTTP 端到端：截断流经 8888 后客户端拿到可解析的 arguments；长 tool 历史被折叠；
    非流式危险工具调用被改写成 `[TOOL_BLOCKED]`。
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core import context_fold, sse_heal, tool_ast  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


def _frame(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


TRUNCATED_ARGS = '{"command": "ls'
COMPLETE_ARGS = '{"command": "ls"}'
DANGEROUS_ARGS = '{"command": "rm -rf /"}'


def _tool_frame(args: str, index: int = 0) -> bytes:
    return _frame({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": index, "id": "c1", "type": "function",
         "function": {"name": "run", "arguments": args}}]}}]})


class TestToolAst(unittest.TestCase):
    def test_rm_rf_blocked(self):
        res = tool_ast.audit_command("rm -rf /tmp/x")
        self.assertTrue(res["blocked"])
        self.assertIn("high_risk:rm", ",".join(res["rules"]))

    def test_whitelisted_command_passes(self):
        self.assertFalse(tool_ast.audit_command("pytest tests/ -q")["blocked"])
        self.assertFalse(tool_ast.audit_command("git status")["blocked"])
        self.assertFalse(tool_ast.audit_command("cat package.json")["blocked"])

    def test_pipe_to_shell_blocked(self):
        res = tool_ast.audit_command("echo cm0gLXJmIH4= | base64 -d | sh")
        self.assertTrue(res["blocked"])
        self.assertTrue(any("pipe_to_shell" in r for r in res["rules"]))

    def test_sensitive_path_blocked(self):
        res = tool_ast.audit_command("cat ~/.ssh/id_rsa")
        self.assertTrue(res["blocked"])
        self.assertTrue(any("sensitive_path" in r or "ssh" in r for r in res["rules"]))

    def test_inline_script_failclosed(self):
        res = tool_ast.audit_command('python -c "print(1)"')
        self.assertTrue(res["blocked"])
        self.assertTrue(res["failclosed"])

    def test_too_long_blocked(self):
        res = tool_ast.audit_command("echo " + "a" * 9000)
        self.assertTrue(res["blocked"])
        self.assertIn("too_long", res["rules"])


class TestObfuscationScan(unittest.TestCase):
    """GW-EVO-01 反混淆：scan_commands 对拆词/替换/sudo 前缀的识别。"""

    def setUp(self):
        sse_heal.reset_stats()
        tool_ast.reset_stats()

    def test_empty_quote_split_blocked(self):
        """引号拆词 `r''m -rf` 必须命中（规范化后正则可及）。"""
        self.assertIn("rm_rf", sse_heal.scan_commands("r''m -rf ./x"))

    def test_sudo_and_path_prefix_blocked(self):
        self.assertIn("rm_rf", sse_heal.scan_commands("sudo rm -rf /"))
        self.assertIn("rm_rf", sse_heal.scan_commands("/bin/rm -rf ./x"))

    def test_command_substitution_blocked(self):
        """`$(echo rm) -rf` / 反引号替换构造必须命中（扁平化检测）。"""
        self.assertTrue(sse_heal.scan_commands("$(echo rm) -rf /tmp/x"))
        self.assertTrue(sse_heal.scan_commands("`echo rm` -rf /tmp/x"))

    def test_benign_split_not_blocked(self):
        """无害引号拆词（`e''cho ok`）与正常命令不得误报。"""
        self.assertEqual(sse_heal.scan_commands("e''cho ok"), [])
        self.assertEqual(sse_heal.scan_commands("git status && ls -la"), [])
        self.assertEqual(sse_heal.scan_commands("pytest tests/ -q"), [])


class TestClosers(unittest.TestCase):
    def test_string_and_containers(self):
        self.assertEqual(sse_heal.closers('{"cmd": "ls -la'), '"}' )
        self.assertEqual(sse_heal.closers('{"a":[1,2'), "]}")
        self.assertEqual(sse_heal.closers('{"a": {"b": "c'), '"}}')
        self.assertEqual(sse_heal.closers('{"a": 1}'), "")
        self.assertEqual(sse_heal.closers('{"a": "x\\"'), '"}')


class TestHealer(unittest.TestCase):
    def setUp(self):
        sse_heal.reset_stats()
        tool_ast.reset_stats()

    def test_shadow_is_byte_identical(self):
        stream = (_tool_frame(COMPLETE_ARGS)
                  + _frame({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
                  + b"data: [DONE]\n\n")
        healer = sse_heal.Healer("shadow", "shadow")
        out = b"".join(healer.feed(stream[i:i + 7]) for i in range(0, len(stream), 7))
        out += healer.finish()
        self.assertEqual(out, stream)
        self.assertEqual(sse_heal.stats()["repair_applied"], 0)
        self.assertEqual(sse_heal.stats()["tool_rewritten"], 0)

    def test_enforce_complete_stream_untouched(self):
        stream = (_tool_frame(COMPLETE_ARGS)
                  + _frame({"choices": [{"index": 0, "delta": {},
                                         "finish_reason": "tool_calls"}]})
                  + b"data: [DONE]\n\n")
        healer = sse_heal.Healer("enforce", "shadow")
        out = b"".join(healer.feed(stream[i:i + 11]) for i in range(0, len(stream), 11))
        out += healer.finish()
        self.assertEqual(out, stream)
        self.assertEqual(sse_heal.stats()["repair_applied"], 0)

    def test_enforce_never_fabricates_truncated_arguments(self):
        """GW-EVO-01 铁律：`tool_calls.arguments` 半截 JSON **绝不脑补闭合**。

        补出来的"合法 JSON"是把半截命令补成可执行形态——故障升级成事故。
        enforce 档也只如实透传，由客户端 JSON.parse 失败走工具重试。
        """
        stream = _tool_frame(TRUNCATED_ARGS) + b"data: [DONE]\n\n"
        healer = sse_heal.Healer("enforce", "shadow")
        out = healer.feed(stream) + healer.finish()
        # 原帧逐字节保留，零新增帧
        self.assertIn(_tool_frame(TRUNCATED_ARGS), out)
        # 客户端拼出的 arguments 仍是半截（JSON.parse 必失败 → 重试路径）
        args = []
        for line in out.splitlines():
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            doc = json.loads(line[6:])
            for choice in doc.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    args.append((tc.get("function") or {}).get("arguments") or "")
        self.assertEqual("".join(args), TRUNCATED_ARGS)
        # 截断如实计数（shadow/enforce 都记）
        self.assertEqual(sse_heal.stats()["arguments_truncated"], 1)
        self.assertEqual(sse_heal.stats()["repair_applied"], 0)

    def test_markdown_style_partial_still_repaired(self):
        """展示类（非 dict 形态）半截 JSON 仍允许纯语法闭合。"""
        stream = _tool_frame('"just a string') + b"data: [DONE]\n\n"
        healer = sse_heal.Healer("enforce", "shadow")
        out = healer.feed(stream) + healer.finish()
        self.assertGreater(sse_heal.stats()["repair_applied"], 0)
        self.assertIn(b'just a string"', out)

    def test_repair_without_done_flushes_at_end(self):
        """没发 [DONE] 就断流：dict 形态半截 arguments 不补帧、只计数。"""
        healer = sse_heal.Healer("enforce", "shadow")
        self.assertEqual(healer.feed(_tool_frame(TRUNCATED_ARGS)), _tool_frame(TRUNCATED_ARGS))
        tail = healer.finish()
        self.assertNotIn(b"[DONE]", tail)
        self.assertEqual(sse_heal.stats()["arguments_truncated"], 1)
        self.assertEqual(sse_heal.stats()["repair_applied"], 0)

    def test_complete_single_frame_dangerous_rewritten(self):
        healer = sse_heal.Healer("shadow", "enforce")
        out = healer.feed(_tool_frame(DANGEROUS_ARGS))
        self.assertIn(b"TOOL_BLOCKED", out)
        self.assertNotIn(b"rm -rf /", out)
        self.assertEqual(sse_heal.stats()["tool_rewritten"], 1)

    def test_cross_frame_dangerous_not_rewritten(self):
        split = DANGEROUS_ARGS[:8], DANGEROUS_ARGS[8:]
        stream = _tool_frame(split[0]) + _tool_frame(split[1]) + b"data: [DONE]\n\n"
        healer = sse_heal.Healer("shadow", "enforce")
        out = healer.feed(stream) + healer.finish()
        self.assertEqual(out, stream)          # 分片撤不回，绝不伪造改写
        self.assertGreaterEqual(sse_heal.stats()["tool_unrewritable"], 1)

    def test_crlf_separator_preserved(self):
        stream = (b"data: " + json.dumps({"choices": []}).encode() + b"\r\n\r\n"
                  + b"data: [DONE]\r\n\r\n")
        healer = sse_heal.Healer("enforce", "shadow")
        self.assertEqual(healer.feed(stream) + healer.finish(), stream)

    def test_scrub_payload_shadow_then_enforce(self):
        doc = {"choices": [{"index": 0, "message": {
            "role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "run", "arguments": DANGEROUS_ARGS}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        raw = json.dumps(doc, ensure_ascii=False).encode()
        self.assertEqual(sse_heal.scrub_payload(raw, "shadow"), raw)
        out = sse_heal.scrub_payload(raw, "enforce")
        self.assertNotEqual(out, raw)
        parsed = json.loads(out)
        args = parsed["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("TOOL_BLOCKED", args)
        self.assertNotIn("rm -rf /", args)
        self.assertEqual(parsed["usage"]["prompt_tokens"], 1)


# ── HTTP 端到端 ────────────────────────────────────────────────────
class _MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            doc = {}
        type(self).calls.append(doc)
        if doc.get("stream"):
            payload = _tool_frame(TRUNCATED_ARGS) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if doc.get("model") == "danger":
            payload = json.dumps({
                "id": "mock", "model": "danger",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "run",
                                                 "arguments": DANGEROUS_ARGS}}]}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }).encode()
        else:
            payload = json.dumps({
                "id": "mock", "model": "glm-5.3",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _routes_payload(base_url):
    return {"schema": 1, "default_channel": "opencode",
            "channels": {"opencode": {
                "base_url": base_url, "token_env": "", "pricing_channel": "opencode",
                "models": ["glm-5.3", "danger"]}}}


class TestStreamHealHTTP(unittest.TestCase):
    def setUp(self):
        _MockUpstream.calls = []
        self.tmp = tempfile.mkdtemp(prefix="sse_http_")
        tmp = Path(self.tmp)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstream)
        threading.Thread(target=self.upstream.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        base = "http://127.0.0.1:%d" % self.upstream.server_address[1]

        routes = tmp / "model_upstreams.json"
        routes.write_text(json.dumps(_routes_payload(base + "/v1")), encoding="utf-8")

        self.old = {name: getattr(server, name) for name in (
            "MODEL_ROUTER", "LEDGER", "CONTROL", "MODEL_UPSTREAM", "MODEL_TOKEN",
            "TOKEN", "LEDGER_MODE", "SSE_HEAL_MODE", "TOOL_GUARD_MODE",
            "CONTEXT_FOLD_MODE")}
        self.old_prompt_mode = server.PROMPT_OPTIMIZER.mode

        def restore():
            for key, value in self.old.items():
                setattr(server, key, value)
            server.PROMPT_OPTIMIZER.set_mode(self.old_prompt_mode)
            server.reset_budget_cache()

        self.addCleanup(restore)
        self.addCleanup(self._reset_stats)
        server.MODEL_ROUTER = ModelUpstreamRouter(
            routes, default_upstream=base + "/v1", default_channel="opencode",
            default_token="", environ={})
        server.LEDGER = LocalLedger(tmp / "ledger.db", tmp / "pricing.json")
        server.CONTROL = LocalControl(tmp / "control.json", default_channel="opencode")
        server.MODEL_UPSTREAM = base + "/v1"
        server.MODEL_TOKEN = ""
        server.TOKEN = ""
        server.LEDGER_MODE = "local"
        server.SSE_HEAL_MODE = "enforce"
        server.TOOL_GUARD_MODE = "enforce"
        server.CONTEXT_FOLD_MODE = "enforce"
        server.PROMPT_OPTIMIZER.set_mode("off")
        server.reset_budget_cache()
        sse_heal.reset_stats()
        tool_ast.reset_stats()
        context_fold.reset_stats()

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.gateway.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.gateway.shutdown)
        self.base = "http://127.0.0.1:%d" % self.gateway.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _reset_stats():
        sse_heal.reset_stats()
        tool_ast.reset_stats()
        context_fold.reset_stats()

    def _post(self, doc, timeout=20):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(doc).encode("utf-8"), method="POST",
            headers={"content-type": "application/json"})
        return self.opener.open(req, timeout=timeout)

    def test_truncated_stream_never_fabricated_end_to_end(self):
        """GW-EVO-01 端到端：上游截断流经网关后，客户端拿到的 arguments 仍是原样
        半截（绝不脑补），JSON.parse 失败即走工具重试；截断计数 +1。"""
        with self._post({"model": "glm-5.3", "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}) as resp:
            raw = resp.read()
        args = []
        for line in raw.splitlines():
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            doc = json.loads(line[6:])
            for choice in doc.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    args.append((tc.get("function") or {}).get("arguments") or "")
        self.assertEqual("".join(args), TRUNCATED_ARGS)   # 原样透传，零脑补
        self.assertGreaterEqual(sse_heal.stats()["arguments_truncated"], 1)
        self.assertEqual(sse_heal.stats()["repair_applied"], 0)

    def test_long_tool_history_folded_end_to_end(self):
        noise = "\n".join("noise line %d: lorem ipsum dolor sit amet" % i for i in range(900))
        long_tool = noise[: len(noise) // 2] + "\nError: boom at line 300\n" + noise[len(noise) // 2:]
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "tool_call_id": "call_1", "content": "short"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "tool", "tool_call_id": "call_2", "content": long_tool},
            {"role": "user", "content": "go"},
        ]
        with self._post({"model": "glm-5.3", "messages": messages}) as resp:
            saved = resp.headers.get("X-Hub-Ctx-Saved")
            resp.read()
        self.assertTrue(saved and int(saved) > 0, "折叠节省头缺失: %s" % saved)
        seen = _MockUpstream.calls[-1]["messages"]
        self.assertEqual(seen[3]["content"], "short")          # 热区不碰
        self.assertIn("HUB NEO folded", seen[6]["content"])    # 中间已折
        self.assertIn("Error: boom", seen[6]["content"])       # 错误行保住
        self.assertEqual(seen[6]["tool_call_id"], "call_2")    # 结构不碰
        self.assertTrue(seen[6]["content"].startswith("noise line 0"))  # 首行保住
        self.assertIn("noise line 899", seen[6]["content"])            # 尾行保住

    def test_nonstream_dangerous_tool_call_rewritten(self):
        with self._post({"model": "danger",
                         "messages": [{"role": "user", "content": "hi"}]}) as resp:
            self.assertEqual(resp.headers.get("X-Hub-Tool-Guard"), "rewritten")
            doc = json.loads(resp.read())
        args = doc["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("TOOL_BLOCKED", args)
        self.assertNotIn("rm -rf /", args)
        self.assertEqual(doc["usage"]["total_tokens"], 5)

    def test_health_exposes_outbound_guardrails(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            doc = json.loads(resp.read())
        self.assertEqual(doc["stream_heal"]["sse"], "enforce")
        self.assertEqual(doc["stream_heal"]["tool_guard"], "enforce")
        self.assertIn("repair_applied", doc["stream_heal"]["stats"])
        self.assertEqual(doc["context_fold"]["mode"], "enforce")
        self.assertEqual(doc["context_fold"]["trigger_tokens"],
                         context_fold.TRIGGER_TOKENS)
        self.assertIn("saved_tokens", doc["context_fold"]["stats"])


if __name__ == "__main__":
    unittest.main()
