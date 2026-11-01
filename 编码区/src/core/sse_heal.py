# -*- coding: utf-8 -*-
"""core/sse_heal.py —— 流式 SSE 自愈 + 工具出站守卫（移植自 hub-ng GW-4 / GW-11）。

## 为什么做在字节流包装层

HUB NEO 的 `/v1/*` 是同步反代（`http.server`），真流式由 `server._stream_sse()` 逐块
读写。要判断「这一帧是不是一个未闭合的 `tool_calls.arguments` 增量」，必须解析 JSON
并**跨帧累积状态**；包在 `_stream_sse` 的读循环上即可，天然顺序、零并发。

## 三条硬铁律（测试逐条断言，破了就是事故）

1. **只修 `tool_calls.arguments` 末尾缺失的闭合符**，且**以追加一帧的形式**输出
   （旧帧一字不改）。不修 `content`、不改 `finish_reason`、不动 `[DONE]` 与心跳注释帧 ——
   截断就如实保留 `finish_reason=length`，绝不粉饰。
2. **正常完整流零新增帧**（逐帧 byte-identical）。这是"自愈"，不是"重写"。
3. **工具出站黑名单默认 shadow**：只记录事件与计数，**绝不阻断生成、绝不篡改参数**；
   `HUB_TOOL_GUARD=enforce` 才改写危险命令。

## 三态语义

    off     完全不介入（连累积都不做，零开销直通）
    shadow  逐帧透传 + **只统计**"本该修几条流""命中几条危险命令"，不改一个字节
    enforce 追加修复帧；工具守卫在**单帧 arguments 完整**时就地改写

⇒ 默认 `HUB_SSE_HEAL=shadow`、`HUB_TOOL_GUARD=shadow` ⇒ **线上行为零变化**，
先看影子统计再谈 enforce。与 hub-ng 线上口径一致。

## 为什么 `arguments` 会半截

上游在 `finish_reason=length` 或链路截断时，`arguments` 停在半个 JSON 里，客户端
`JSON.parse` 当场抛 → agent 循环崩。补法是**纯语法**的：扫一遍累积串，算出缺哪些
闭括号，不猜语义。

## 性能

逐帧只做 1 次 `bytes.__contains__` 字面量检查（C 速度），**只有命中 `"tool_calls"`
的帧才 `json.loads`** —— 内容增量帧（占绝大多数）完全不解析、只透传。

## 与 hub-ng 的差异

- 去掉 `core.agw` 依赖，模式由环境变量 + `server.py` 的模块级开关控制；
- 入口从异步生成器 `wrap()` 改为同步 `Healer.feed()/finish()`（`http.server` 是同步栈）；
- 新增 `scrub_payload()`：非流式回执里的完整 `tool_calls.arguments` 也纳入同一审计，
  避免"只有流式才有防线、普通 JSON 回执裸奔"。
"""
from __future__ import annotations

import json
import re
import threading

from core import tool_ast

# 工具出站黑名单（默认 shadow）。正则刻意保守：宁可漏报也不误伤正常命令 ——
# 误伤会让 agent 的正常任务被挡，而漏报在 shadow 期只是少一条统计。
DENY = (
    ("rm_rf", re.compile(r"rm\s+-(?:rf|fr|r\s*-f)\b")),
    ("ssh_key", re.compile(r"~/\.ssh|\.ssh/id_[A-Za-z0-9]+")),
    ("mkfs", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    ("dd_if", re.compile(r"\bdd\s+if=")),
    ("b64_pipe_sh", re.compile(r"base64\s+(?:-d|--decode)\s*\|\s*(?:ba|z|k)?sh\b")),
    ("curl_internal", re.compile(
        r"curl[^\n]{0,120}(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})")),
)

_TC = b'"tool_calls"'
_DONE = b"[DONE]"      # ⚠️ 是方括号不是斜杠：写成 "/DONE" 会永不命中（hub-ng 判据 B4 抓到）
_DATA_PREFIX = b"data:"

MODES = ("off", "shadow", "enforce")
DEFAULT_HEAL_MODE = "shadow"
DEFAULT_TOOL_MODE = "shadow"

STATS = {
    "streams": 0,
    "frames": 0,
    "repair_would": 0,       # 展示类（Markdown 围栏）本该补几条流（shadow 也记）
    "repair_applied": 0,     # 展示类真追加了几帧
    "arguments_truncated": 0,  # tool_calls.arguments 截断条数（GW-EVO-01：绝不脑补闭合，
                               # shadow/enforce 都如实计数；客户端 JSON.parse 失败即触发工具重试）
    "tool_blocked": 0,       # 命中危险命令的条数（shadow 是"本该拦"）
    "tool_rewritten": 0,     # 单帧完整、已就地改写的条数
    "tool_unrewritable": 0,  # 跨帧分片、结构上改不了（如实记，不伪造成功）
}
_STATS_LOCK = threading.Lock()


def _bump(name: str, n: int = 1) -> None:
    with _STATS_LOCK:
        STATS[name] = int(STATS.get(name) or 0) + int(n or 0)


def stats() -> dict:
    with _STATS_LOCK:
        return dict(STATS)


def reset_stats() -> None:
    with _STATS_LOCK:
        for key in STATS:
            STATS[key] = 0


def normalize_mode(value, default: str = DEFAULT_HEAL_MODE) -> str:
    text = str(value or "").strip().lower()
    return text if text in MODES else default


def env_mode(name: str, default: str) -> str:
    import os
    return normalize_mode(os.environ.get(name), normalize_mode(default))


def _cmd_text(args_json: str) -> str:
    """从（可能是**半截**的）`arguments` JSON 文本里抠出命令字符串。

    为什么必须抠：`scan_commands` 拿到的是流式累积的**分片 JSON**（如
    `{"command": "dd if=/dev/zero`），直接丢给语法审计会被判"引号不平衡/不可解析"，
    于是每条正常命令都被误报成混淆。

    宁可少抠也不能误伤：优先级 `command`/`cmd` 字段 → 全串兜底。
    """
    if not args_json:
        return ""
    try:
        v = json.loads(args_json)
        if isinstance(v, dict):
            for k in ("command", "cmd", "script", "code"):
                if isinstance(v.get(k), str):
                    return v[k]
            # 没有已知键：把字符串值拼起来（工具参数里能当命令的只可能是它们）
            return " ".join(str(x) for x in v.values() if isinstance(x, str))
        if isinstance(v, str):
            return v
    except Exception:                                        # noqa: BLE001
        pass
    # 半截 JSON：正则抠已知键的字符串值（容忍未闭合）
    m = re.search(r'"(?:command|cmd|script|code)"\s*:\s*"((?:[^"\\]|\\.)*)', args_json)
    if m:
        try:
            return m.group(1).encode().decode("unicode_escape")
        except Exception:                                    # noqa: BLE001
            return m.group(1)
    return args_json if len(args_json) < 4096 else ""


def scan_commands(text: str) -> list:
    """扫一段文本（通常即累积的 `tool_calls.arguments`）→ 命中的规则名列表。

    先走 `core/tool_ast.py`（`shlex` 分词 + 管道段/子命令替换判定 + 敏感路径 +
    喂 shell 识别），再并上本节保留的 6 条**字面**正则（对日志检索友好，且能兜住
    AST 切不动的场景）。

    ⚠️ 顺序讲究：正则扫**原始文本**（半截 JSON 里也常有 `rm -rf` 字样，能抓到），
    AST 只扫**抠出来的命令**（避免把 JSON 语法当成 shell 混淆）。

    GW-EVO-01 反混淆预处理（Deobfuscator）：对**原始文本与其规范化形态**双重扫描——
    · 剥空引号对（`r''m` → `rm`）：引号拆词是绕过正则的经典手法，剥掉后字面
      规则就能命中；双引号同理（`r""m`）。
    · `sudo` / 绝对路径前缀归一（`sudo rm -rf`、`/bin/rm -rf` → `rm -rf`）：
      让正则与 AST 都按真实可执行名判定。
    规范化**只用于检测**，透传字节流保持原样（shadow 铁律不受影响）。
    """
    if not text:
        return []
    variants = [str(text)]
    stripped = text.replace("''", "").replace('""', "")
    if stripped != text:
        variants.append(stripped)
    # 命令替换外壳剥离：`$(x)` / `` `x` `` 的**内层命令**可能本身无害（echo），
    # 但其执行结果被拼接成外层命令的一部分（`$(echo rm) -rf /`）。
    # 检测口径：剥掉外壳后把内外层拼成一条"扁平命令"再扫——只用于检测，
    # 不改透传字节。
    if "$(" in text or "`" in text:
        flat = re.sub(r"\$\(([^()]*)\)|`([^`]*)`", r"\1\2", text)
        if flat != text:
            variants.append(flat)
    rules = []
    for v in variants:
        rules.extend(name for name, pat in DENY if pat.search(v))
    # 检测用规范化：剥 sudo / 可执行路径前缀，让 AST/正则看真实命令
    for v in list(variants):
        m = re.match(r"^\s*(?:sudo\s+|-?\s*/(?:usr/)?(?:local/)?(?:s?bin/))?"
                     r"((?:[\w.-]+/)*)([\w-]+)(\s+.*)?$", v)
        if m and m.group(2):
            normalized = m.group(2) + (m.group(3) or "")
            if normalized != v:
                rules.extend(name for name, pat in DENY if pat.search(normalized))
                v = normalized
        try:
            r = tool_ast.audit_command(_cmd_text(v))
        except Exception:                                    # noqa: BLE001
            continue
        if r.get("blocked"):
            # 去重口径：AST 的 `high_risk:rm(-rf)` 与正则的 `rm_rf` 是**同一条危险**，
            # 合并成一条（否则 `tool_blocked` 会把同一命令数成两遍）。
            _covered = {"rm_rf": ("rm",), "ssh_key": (".ssh", "id_rsa"),
                        "mkfs": ("mkfs",), "dd_if": ("dd",),
                        "b64_pipe_sh": ("base64",), "curl_internal": ("curl",)}
            _have = set()
            for nm in rules:
                for tok in _covered.get(nm, ()):
                    _have.add(tok)
            for x in (r.get("rules") or []):
                base = x.split(":", 1)[0]
                if base == "high_risk":
                    cmd = x.split(":", 1)[1].split("(")[0]
                    if cmd in _have:
                        continue
                if base == "sensitive_path" and ("ssh" in x or "id_rsa" in x) and "ssh_key" in rules:
                    continue
                rules.append(x)
    # 去重保序
    seen, out = set(), []
    for x in rules:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def closers(partial: str) -> str:
    """算出把半截 JSON 补完整所需的字符（纯语法，不做语义猜测）。

    状态机：字符串内/外、转义、括号栈。返回需追加的字符串（必要时含 `"`）。
    例：`{"cmd": "ls -la` → `"}`；`{"a":[1,2` → `]}`
    """
    stack, in_str, esc = [], False, False
    for chn in partial:
        if esc:
            esc = False
            continue
        if in_str:
            if chn == "\\":
                esc = True
            elif chn == '"':
                in_str = False
            continue
        if chn == '"':
            in_str = True
        elif chn in "{[":
            stack.append("}" if chn == "{" else "]")
        elif chn in "}]":
            if stack:
                stack.pop()
    out = '"' if in_str else ""
    return out + "".join(reversed(stack))


def repair_frame(tc_index: int, need: str) -> bytes:
    """构造补全帧（**不带**结尾空行；由调用方按原流分隔符拼接）。"""
    obj = {"choices": [{"index": 0, "finish_reason": None,
                        "delta": {"tool_calls": [{"index": tc_index,
                                                  "function": {"arguments": need}}]}}]}
    return _DATA_PREFIX + b" " + json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _payload_text(event: bytes) -> str:
    """取 SSE 事件里 `data:` 行的内容；没有则空串。"""
    for line in event.splitlines():
        if line.startswith(_DATA_PREFIX):
            return line[len(_DATA_PREFIX):].strip().decode("utf-8", "ignore")
    return ""


def _split_first(buf: bytes):
    """找第一个 SSE 事件边界 → `(下标, 分隔符)`；没有返回 `(-1, b"")`。

    同时容忍 `\\n\\n` 与 `\\r\\n\\r\\n`，并**保留原始分隔符**：
    shadow 模式下输出必须逐字节等于输入。
    """
    i_n = buf.find(b"\n\n")
    i_r = buf.find(b"\r\n\r\n")
    if i_n < 0 and i_r < 0:
        return -1, b""
    if i_n < 0:
        return i_r, b"\r\n\r\n"
    if i_r < 0:
        return i_n, b"\n\n"
    return (i_r, b"\r\n\r\n") if i_r < i_n else (i_n, b"\n\n")


class Healer:
    """单条流的状态机。`feed()` 吃上游块，返回可写客户端的字节；`finish()` 收尾。"""

    def __init__(self, mode: str | None = None, tool_mode: str | None = None):
        self.mode = normalize_mode(mode, env_mode("HUB_SSE_HEAL", DEFAULT_HEAL_MODE))
        self.tool_mode = normalize_mode(
            tool_mode, env_mode("HUB_TOOL_GUARD", DEFAULT_TOOL_MODE))
        self._buf = b""
        self._args = ""
        self._tc_index = 0
        self._after_done = False
        self._rewritten = False
        self._would = 0
        self._applied = 0
        self._rules = []
        self._finished = False
        if self.mode != "off" or self.tool_mode != "off":
            _bump("streams")

    # ── 帧内解析 ────────────────────────────────────────────
    def _absorb(self, event: bytes) -> None:
        """吸收 `tool_calls.arguments` 增量（只在命中 `tool_calls` 的帧上调用）。"""
        try:
            body = _payload_text(event)
            if not body or body == "[DONE]":
                return
            obj = json.loads(body)
            for ch_ in (obj.get("choices") or []):
                if not isinstance(ch_, dict):
                    continue
                for tc in ((ch_.get("delta") or {}).get("tool_calls") or []):
                    if not isinstance(tc, dict):
                        continue
                    try:
                        self._tc_index = int(tc.get("index") or 0)
                    except (TypeError, ValueError):
                        pass
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str) and args:
                        self._args += args
        except Exception:                                    # noqa: BLE001
            pass

    def _need_repair(self) -> tuple:
        """→ `(kind, 闭合符)`。kind: `""` 无需处理 / `markdown` 展示类可补 / `truncated` 命令截断。

        GW-EVO-01 口径收紧：
        · `tool_calls.arguments` 半截 JSON → **绝不脑补闭合**。补出来的"完整 JSON"看似
          合法，实则是把半截命令（如 `{"command": "rm -rf /tm`）补成可执行形态下发——
          那是把故障升级成事故。只标记 `truncated`，让客户端 `json.loads` 失败后走
          工具重试链路。
        · 展示类半截（Markdown 代码围栏等纯文本）→ 纯语法闭合是安全的（最坏也只是
          显示一个空代码块），保持原补帧能力。
        """
        if not self._args:
            return "", ""
        # arguments 形态判定：整体必须是完整 JSON；补不回来或补出来的是 dict
        # （工具参数形态）都算 truncated —— 只要有 dict 语义嫌疑就按命令截断处理。
        need = closers(self._args)
        try:
            json.loads(self._args)
            return "", ""                        # 本就完整，无需修复
        except Exception:                        # noqa: BLE001
            pass
        try:
            closed = json.loads(self._args + need)
        except Exception:                        # noqa: BLE001
            return "truncated", ""               # 补不回来 → 如实截断
        if isinstance(closed, dict):
            return "truncated", ""               # 补回来是 dict ⇒ 工具参数半截，不脑补
        return "markdown", need                  # 非对象形态（字符串/数组）按展示类补

    def _rewrite_event(self, event: bytes):
        """本帧内 arguments 已完整且判违规 → 返回**改写后的整帧**；否则 None。

        只在"单帧自足"时改写：SSE 的 arguments 是增量拼接语义，分片到达时撤不回已发
        片段，追加改写帧只会让客户端拼出非法 JSON（那是把安全加固变成新的故障源）。
        """
        text = event.decode("utf-8", "ignore")
        if not text.startswith("data:"):
            return None
        body = text[len("data:"):].strip()
        if not body or body == "[DONE]":
            return None
        try:
            obj = json.loads(body)
        except Exception:                                    # noqa: BLE001
            return None
        changed = False
        for ch_ in (obj.get("choices") or []):
            if not isinstance(ch_, dict):
                continue
            for tc in ((ch_.get("delta") or {}).get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str) or not args:
                    continue
                try:
                    parsed = json.loads(args)               # 必须是完整 JSON 才改写
                except Exception:                            # noqa: BLE001
                    continue
                # 只改「完整对象」：流式分片里 `"rm -rf /"` 这种合法 JSON 字符串只是片段，
                # 改写它会把后续分片拼坏。对象才是工具参数的正常完整形态。
                if not isinstance(parsed, dict):
                    continue
                rules = scan_commands(args)
                if not rules:
                    continue
                fn["arguments"] = tool_ast.rewrite_arguments(args)
                self._rules.extend(rules)
                _bump("tool_blocked", len(rules))
                _bump("tool_rewritten")
                changed = True
        if not changed:
            return None
        return _DATA_PREFIX + b" " + json.dumps(
            obj, ensure_ascii=False).encode("utf-8") + b"\n\n"

    # ── 对外入口 ────────────────────────────────────────────
    def feed(self, chunk: bytes) -> bytes:
        """吃一块上游字节，返回这一块里所有**完整事件**的转发字节。"""
        if self.mode == "off" and self.tool_mode == "off":
            return chunk
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            chunk = str(chunk).encode("utf-8")
        self._buf += bytes(chunk)
        out = []
        while True:
            idx, sep = _split_first(self._buf)
            if idx < 0:
                break
            event = self._buf[:idx]
            self._buf = self._buf[idx + len(sep):]
            out.append(self._process(event, sep))
        return b"".join(out)

    def _process(self, event: bytes, sep: bytes) -> bytes:
        _bump("frames")
        try:
            if self._after_done:
                return event + sep
            if _DONE in event and _payload_text(event) == "[DONE]":
                # `[DONE]` 之前是最后一个"客户端还会读"的位置 → 修复帧插在这里
                kind, need = self._need_repair()
                self._after_done = True
                if kind == "truncated":
                    # GW-EVO-01：命令截断绝不脑补闭合，shadow/enforce 都只如实计数，
                    # 客户端 JSON.parse 失败 → 触发工具重试（这是设计好的失败路径）。
                    self._would += 1
                    _bump("arguments_truncated")
                elif kind == "markdown":
                    self._would += 1
                    _bump("repair_would")
                    if self.mode == "enforce":
                        self._applied += 1
                        _bump("repair_applied")
                        return repair_frame(self._tc_index, need) + sep + event + sep
                return event + sep
            if _TC in event:
                self._absorb(event)
                # 单帧内已完整的 arguments → 可就地改写（协议不变）
                if self.tool_mode == "enforce" and not self._rewritten:
                    nb = self._rewrite_event(event)
                    if nb is not None:
                        self._rewritten = True
                        return nb + sep
        except Exception:                                    # noqa: BLE001
            pass                                              # 观测绝不干扰透传
        return event + sep

    def finish(self) -> bytes:
        """流结束：刷出未成帧的尾巴；没发 `[DONE]` 就结束时补修复帧。"""
        if self._finished:
            return b""
        self._finished = True
        if self.mode == "off" and self.tool_mode == "off":
            return b""
        out = self._buf
        self._buf = b""
        try:
            if not self._after_done:
                kind, need = self._need_repair()
                if kind == "truncated":
                    self._would += 1
                    _bump("arguments_truncated")
                elif kind == "markdown":
                    self._would += 1
                    _bump("repair_would")
                    if self.mode == "enforce":
                        self._applied += 1
                        _bump("repair_applied")
                        if out and not out.endswith((b"\n\n", b"\r\n\r\n")):
                            out += b"\n\n"
                        out += repair_frame(self._tc_index, need) + b"\n\n"
            # 跨帧分片的载荷**改不了**：OpenAI SSE 的 `tool_calls.arguments` 是增量拼接
            # 语义，已发出的帧撤不回。这里只如实标注"本可拦但改不了"，不伪造改写成功。
            if self.tool_mode != "off" and self._args and not self._rewritten:
                blocked = scan_commands(self._args)
                if blocked:
                    _bump("tool_blocked", len(blocked))
                    _bump("tool_unrewritable")
                    self._rules.extend(blocked)
        except Exception:                                    # noqa: BLE001
            pass
        return out


def scrub_payload(payload: bytes, mode: str | None = None) -> bytes:
    """非流式回执的工具审计：完整 `tool_calls.arguments` 命中危险命令时就地改写。

    shadow 只计数、原文返回；enforce 才重新序列化（调用方必须用返回值重算
    content-length）。解析失败一律原文返回，绝不因为审计把正常回执弄坏。
    """
    mode = normalize_mode(mode, env_mode("HUB_TOOL_GUARD", DEFAULT_TOOL_MODE))
    if mode == "off" or not payload:
        return payload
    try:
        text = payload.decode("utf-8")
        doc = json.loads(text)
    except Exception:                                        # noqa: BLE001
        return payload
    if not isinstance(doc, dict):
        return payload
    changed = False
    for ch_ in (doc.get("choices") or []):
        if not isinstance(ch_, dict):
            continue
        msg = ch_.get("message")
        if not isinstance(msg, dict):
            msg = ch_.get("delta")
        if not isinstance(msg, dict):
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if not isinstance(args, str) or not args:
                continue
            try:
                parsed = json.loads(args)                    # 非流式也要求完整 JSON 对象
            except Exception:                                # noqa: BLE001
                continue
            if not isinstance(parsed, dict):
                continue
            rules = scan_commands(args)
            if not rules:
                continue
            _bump("tool_blocked", len(rules))
            if mode == "enforce":
                fn["arguments"] = tool_ast.rewrite_arguments(args)
                _bump("tool_rewritten")
                changed = True
    if not changed:
        return payload
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")
