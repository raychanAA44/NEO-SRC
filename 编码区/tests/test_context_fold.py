# -*- coding: utf-8 -*-
"""编码区/tests/test_context_fold.py —— 上下文 L1 堆栈折叠。

覆盖：
  · 只折旧 `role=tool` 回显：前 3 条非 system 消息、user/assistant 一律不动；
  · 首尾行、错误行、Windows 路径、代码行保留；中间连续噪声折叠成标记；
  · 不删消息、不改 role、不动 tool_call_id；
  · 三态：off 不动 / shadow 只报省额不动字节 / enforce 真改；
  · 触发阈值与单条最小长度；多段 content 只折 text 段；
  · 票面性能 < 1ms（6000 行 / 335KB，宿主可测时才断言）。
"""
import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from _timing import REFERENCE_LIMIT_MS, best_wall_ms, reference_wall_ms  # noqa: E402
from core import context_fold  # noqa: E402


def _noise(n=600, prefix="noise"):
    return "\n".join("%s line %d: lorem ipsum dolor sit amet" % (prefix, i) for i in range(n))


def _body(tool_text, *, head_guard=True):
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "tool_call_id": "call_1", "content": "short"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "tool", "tool_call_id": "call_2", "content": tool_text},
        {"role": "user", "content": "go"},
    ]
    if not head_guard:            # 把长 tool 移到热区，验证绝不碰
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "tool", "tool_call_id": "call_1", "content": tool_text},
            {"role": "user", "content": "u2"},
        ]
    return {"model": "glm-5.3", "messages": messages}


class TestFoldText(unittest.TestCase):
    def test_keeps_head_tail_error_and_code(self):
        text = ("head-0\nhead-1\n"
                + "noise a\n"
                + "Traceback (most recent call last):\n"
                + '  File "app.py", line 3, in <module>\n'
                + "    import os\n"
                + "Error: boom\n"
                + "C:\\work\\app.py:12\n"
                + _noise(400, "middle")
                + "\ntail-0\ntail-1")
        out, meta = context_fold.fold_text(text)
        self.assertGreater(meta["saved_chars"], 0)
        self.assertIn("HUB NEO folded", out)
        self.assertIn("head-0", out)
        self.assertIn("tail-1", out)
        self.assertIn("Traceback", out)
        self.assertIn("Error: boom", out)
        self.assertIn('File "app.py"', out)
        self.assertIn("import os", out)
        self.assertIn("C:\\work\\app.py", out)
        self.assertLess(len(out), len(text))

    def test_short_text_untouched(self):
        text = "short\n" * 10
        out, meta = context_fold.fold_text(text)
        self.assertEqual(out, text)
        self.assertEqual(meta["saved_chars"], 0)

    def test_ratio_above_30_percent_on_noise(self):
        text = _noise(800)
        out, meta = context_fold.fold_text(text)
        self.assertGreater(meta["saved_chars"] / len(text), 0.3)


class TestApply(unittest.TestCase):
    def setUp(self):
        context_fold.reset_stats()

    def test_off_is_noop(self):
        body = _body(_noise(800))
        before = copy.deepcopy(body)
        info = context_fold.apply(body, "off")
        self.assertEqual(body, before)
        self.assertEqual(info["folded"], 0)
        self.assertEqual(context_fold.stats()["requests"], 0)

    def test_shadow_reports_without_mutation(self):
        body = _body(_noise(800))
        before = copy.deepcopy(body)
        info = context_fold.apply(body, "shadow")
        self.assertEqual(body, before)
        self.assertGreater(info["saved_tokens"], 0)
        self.assertEqual(info["mode"], "shadow")
        self.assertEqual(context_fold.stats()["shadow_only"], 1)

    def test_enforce_folds_only_old_tool_messages(self):
        body = _body(_noise(800))
        info = context_fold.apply(body, "enforce")
        self.assertGreater(info["folded"], 0)
        msgs = body["messages"]
        self.assertEqual(msgs[3]["content"], "short")                 # 热区
        self.assertEqual(msgs[1]["content"], "u1")                    # user
        self.assertEqual(msgs[2]["content"], "a1")                    # assistant
        self.assertIn("HUB NEO folded", msgs[6]["content"])           # 旧 tool
        self.assertEqual(msgs[6]["tool_call_id"], "call_2")           # 结构
        self.assertEqual(msgs[7]["content"], "go")                    # 末条 user
        self.assertEqual(len(msgs), 8)                                # 不增删

    def test_early_tool_message_never_touched(self):
        body = _body(_noise(800), head_guard=False)
        before = copy.deepcopy(body)
        info = context_fold.apply(body, "enforce")
        self.assertEqual(body, before)
        self.assertEqual(info["folded"], 0)

    def test_below_trigger_untouched(self):
        body = _body(_noise(60))
        before = copy.deepcopy(body)
        info = context_fold.apply(body, "enforce")
        self.assertEqual(body, before)
        self.assertEqual(info["folded"], 0)

    def test_min_chars_skips_small_tool(self):
        body = _body("tiny\n" * 100)
        info = context_fold.apply(body, "enforce")
        self.assertEqual(info["folded"], 0)

    def test_list_content_only_text_parts(self):
        text = _noise(800)
        body = _body([{"type": "text", "text": text},
                      {"type": "image_url", "image_url": {"url": "http://x/y.png"}}])
        info = context_fold.apply(body, "enforce")
        self.assertGreater(info["folded"], 0)
        parts = body["messages"][6]["content"]
        self.assertIn("HUB NEO folded", parts[0]["text"])
        self.assertEqual(parts[1], {"type": "image_url", "image_url": {"url": "http://x/y.png"}})

    def test_perf_under_1ms(self):
        big = _noise(6000)
        ref = reference_wall_ms()
        if ref > REFERENCE_LIMIT_MS:
            self.skipTest("宿主此刻不可测亚毫秒（基准 %.2fms > %.1fms）"
                          % (ref, REFERENCE_LIMIT_MS))
        ms = best_wall_ms(lambda: context_fold.fold_text(big), rounds=5)
        self.assertLess(ms, 1.0,
                        "6000 行折叠 %.3fms 应 < 1ms（基准 %.2fms）" % (ms, ref))


if __name__ == "__main__":
    unittest.main()
