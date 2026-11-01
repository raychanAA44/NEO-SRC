# -*- coding: utf-8 -*-
"""core/context_fold.py —— 上下文 L1 堆栈折叠（移植自 hub-ng GW-2 的 L1 部分）。

## 为什么要在网关层折

长任务会话的膨胀大头不是 system prompt，而是**客户端每轮把上一次的工具结果全量重发**：
同一会话几百条 `role=tool` 回显，真正有用的只有几行（报错行、栈顶、当前文件路径），
其余是几千行 `node_modules` / 日志噪声。结构性折叠是纯赚：省 token、省注意力、不改语义。

## 三条绝对不变量（本模块的"不许碰"清单）

1. **只动 `index >= 3` 且不在前 3 条非 system 消息里的条目** —— 前缀 cache 靠逐字命中，
   改热区会让整段重算；
2. **只动 `role=tool` 的 `content`** —— 不删条目、不改 role、不动 `tool_call_id`；
   删 tool 消息上游会直接 400 `Invalid tool_call_id`；
3. **不重排、不合并消息** —— 折叠只发生在单条消息内部，保持原始顺序。

## 折叠算法（结构化堆栈折叠）

对单条 `role=tool` 的文本：

    [首 keep_head 行] + 中间"**保护行**" + [已折叠标记] + [尾 keep_tail 行]

保护行 = 命中 `_PROTECT_NEEDLES` 的行（报错/栈帧/代码/路径）。中间的**连续未保护行**
合并成一个标记（保序，不重排）。

### ⚠️ 保护扫描是窗口化的（性能与"零丢失"的取舍）

hub-ng 实测：893KB / 6000 行全量扫描地板价 1.6ms，而预算 <1ms。故只扫每条消息中间区
的**首尾各 `guard_window_chars`（默认 32KB）**。这不影响实际语义：报错行在尾部、
代码片段在首部，两头窗口 + 首尾各 25/20 行已覆盖真实场景；超出窗口的漏保护量会如实
计入 `unprotected` 供影子统计复核。

## 三态

    off     完全不介入
    shadow  只算"本可省多少 token"，请求体一个字节都不改
    enforce 就地替换 content（默认）

为什么默认 enforce（与 hub-ng 的 shadow 默认不同）：这是用户明确要的"立省 30%~50%"，
且不触碰协议与消息结构；`HUB_CONTEXT_FOLD=shadow|off` 可随时回退观察。

## 参数（环境变量）

    HUB_CONTEXT_FOLD            off|shadow|enforce      默认 enforce
    HUB_CONTEXT_FOLD_TRIGGER    触发体量（估算 token）  默认 8000
    HUB_CONTEXT_FOLD_MIN_CHARS  单条 tool 小于此不折    默认 1200
    HUB_CONTEXT_FOLD_KEEP_HEAD  保留首 N 行             默认 25
    HUB_CONTEXT_FOLD_KEEP_TAIL  保留尾 N 行             默认 20
"""
from __future__ import annotations

import os
import threading

CHAR_PER_TOKEN = 3.5          # 与 hub-ng 同口径的粗略估算；不引 tiktoken
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "enforce"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


MODE = (os.environ.get("HUB_CONTEXT_FOLD") or DEFAULT_MODE).strip().lower()
if MODE not in MODES:
    MODE = DEFAULT_MODE
TRIGGER_TOKENS = max(0, _env_int("HUB_CONTEXT_FOLD_TRIGGER", 8000))
MIN_FOLD_CHARS = max(0, _env_int("HUB_CONTEXT_FOLD_MIN_CHARS", 1200))
KEEP_HEAD = max(1, _env_int("HUB_CONTEXT_FOLD_KEEP_HEAD", 25))
KEEP_TAIL = max(1, _env_int("HUB_CONTEXT_FOLD_KEEP_TAIL", 20))
GUARD_WINDOW = max(0, _env_int("HUB_CONTEXT_FOLD_WINDOW", 32768))

_PROTECT_NEEDLES = (
    "Error",        # Error: / AssertionError / TypeError …（含栈底报错行）
    "Traceback",    # Python 栈头（即使后面没有 Error 也要留）
    "Exception",
    "FAILED",
    "error:",
    "ERROR",
    'File "',       # Python traceback 帧
    ":\\",          # Windows 盘符路径
    "\n    at ",    # Node/JS 栈帧（带换行：避免命中正文里的英文 " at "）
    # ⚠️ 代码前缀**不能带前导换行**：真实回显里是缩进的（`    def foo():`），
    # 带 `\n` 的针永远匹配不到。代价是正文里出现 "import "/"def " 也会被保护 ——
    # 安全优先，宁可少折一行，不可丢关键行。
    "import ",
    "def ",
    "class ",
    "return ",
)

STATS = {"requests": 0, "folded_messages": 0, "saved_chars": 0,
         "saved_tokens": 0, "shadow_only": 0, "unprotected": 0}
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


def estimate_chars(msgs) -> int:
    """输入字符数（含 tool_calls 的 arguments —— 那也是实打实的 token）。"""
    n = 0
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        cc = m.get("content")
        if isinstance(cc, str):
            n += len(cc)
        elif isinstance(cc, list):
            for pp in cc:
                if isinstance(pp, dict):
                    n += len(str(pp.get("text") or ""))
        for tc in (m.get("tool_calls") or []):
            try:
                n += len(str(((tc or {}).get("function") or {}).get("arguments") or ""))
            except Exception:                                # noqa: BLE001
                pass
    return n


def estimate_tokens(msgs) -> int:
    return int(estimate_chars(msgs) / CHAR_PER_TOKEN)


def _early_indices(msgs) -> set:
    """前缀 cache 热区（前 3 条**非 system** 消息）在 messages 里的下标。"""
    out, k = set(), 0
    for i, m in enumerate(msgs or []):
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        out.add(i)
        k += 1
        if k >= 3:
            break
    return out


# ── 纯文本折叠 ──────────────────────────────────────────────────
def _head_end(text: str, n: int) -> int:
    """前 n 行之后的行首偏移。"""
    pos = 0
    for _ in range(n):
        nl = text.find("\n", pos)
        if nl < 0:
            return len(text)
        pos = nl + 1
    return pos


def _tail_start(text: str, n: int) -> int:
    """最后 n 行的行首偏移。"""
    pos = len(text)
    for _ in range(n):
        nl = text.rfind("\n", 0, pos)
        if nl < 0:
            return 0
        pos = nl
    return pos + 1 if pos else 0


def _protected(text: str, lo: int, hi: int, window: int) -> tuple:
    """中间区 [lo,hi) 内**保护行**的行首偏移集合 + 是否只扫了窗口。"""
    starts = set()
    if hi <= lo:
        return starts, False
    if window and window > 0:
        spans = ((lo, min(hi, lo + window)), (max(lo, hi - window), hi))
        windowed = (hi - lo) > 2 * window
    else:
        spans, windowed = ((lo, hi),), False
    for kw in _PROTECT_NEEDLES:
        for a, b in spans:
            p = text.find(kw, a, b)
            while p != -1:
                ls = text.rfind("\n", 0, p) + 1
                if lo <= ls < hi:
                    starts.add(ls)
                p = text.find(kw, p + 1, b)
    return starts, windowed


def _tidy(text: str) -> str:
    """空白与重复行压缩：>1 连续空行 → 1；相邻完全相同的非空行只留一条。"""
    out, blank, prev = [], 0, None
    for ln in text.split("\n"):
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        if ln == prev and ln.strip():
            continue
        out.append(ln)
        prev = ln
    return "\n".join(out)


def fold_text(text: str, keep_head: int = KEEP_HEAD, keep_tail: int = KEEP_TAIL,
              window: int = GUARD_WINDOW) -> tuple:
    """折叠单条文本 → (新文本, meta)。**纯函数，不改入参**。

    标记里写**字符数 + 约行数**：精确行数要对整条中间区做一次 `str.count("\\n")`，
    大回显上单这一步就吃满 1ms 预算；字符数是 `len()` 算术、零成本。
    """
    meta = {"saved_chars": 0, "unprotected": 0, "windowed": False, "lines_dropped": 0}
    if not isinstance(text, str) or len(text) < MIN_FOLD_CHARS:
        return text, meta
    he = _head_end(text, max(1, int(keep_head)))
    ts = _tail_start(text, max(1, int(keep_tail)))
    if ts <= he:                                    # 太短：首尾已覆盖全文
        return text, meta
    total = len(text)
    starts, windowed = _protected(text, he, ts, int(window))
    meta["windowed"] = windowed
    if windowed:
        meta["unprotected"] = max(0, (ts - he) - 2 * int(window))
    avg = (he / float(keep_head)) if keep_head else 80.0
    if avg <= 1:
        avg = 80.0
    parts = [text[:he]]
    cur, dropped = he, 0
    for off in sorted(starts):
        if off > cur:
            ch = off - cur
            dropped += int(ch / avg)
            parts.append("[... HUB NEO folded %d chars (~%d lines) ...]" % (ch, int(ch / avg)))
        nl = text.find("\n", off)
        end = (nl + 1) if nl > 0 else total
        parts.append(text[off:end])
        cur = end
    if cur < ts:
        ch = ts - cur
        dropped += int(ch / avg)
        parts.append("[... HUB NEO folded %d chars (~%d lines) ...]" % (ch, int(ch / avg)))
    parts.append(text[ts:])
    out = _tidy("".join(parts))
    meta["lines_dropped"] = dropped
    meta["saved_chars"] = max(0, total - len(out))
    return out, meta


def apply(body: dict, mode: str | None = None) -> dict:
    """按档位处理 `body["messages"]`（**唯一入口**）。

    返回 `{mode, folded, saved_chars, saved_tokens, est_before, est_after, windowed}`。
    `mode != "enforce"` 时 `body` 保持**字节级不变**。
    """
    mode = (str(mode).strip().lower() if mode is not None else MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE
    info = {"mode": mode, "folded": 0, "saved_chars": 0, "saved_tokens": 0,
            "est_before": 0, "est_after": 0, "windowed": False, "folded_chars": 0}
    if mode == "off" or not isinstance(body, dict):
        return info
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return info
    est_before = estimate_tokens(msgs)
    info["est_before"] = est_before
    if est_before < TRIGGER_TOKENS:
        return info
    _bump("requests")
    early = _early_indices(msgs)
    new_vals = {}
    saved_chars = 0
    folded = 0
    tool_chars = 0
    windowed = False
    unprotected = 0
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        cc = m.get("content")
        if isinstance(cc, str):
            tool_chars += len(cc)
        if i < 3 or i in early:                      # 不变量：热区绝不碰
            continue
        if isinstance(cc, str):
            if len(cc) < MIN_FOLD_CHARS:
                continue
            nt, meta = fold_text(cc)
            if meta["saved_chars"] > 0:
                new_vals[i] = nt
                saved_chars += meta["saved_chars"]
                unprotected += meta["unprotected"]
                windowed = windowed or meta["windowed"]
                folded += 1
        elif isinstance(cc, list):
            # 多段 content（图文/多文本块）：只动 text 段，其它段原样
            new_parts, saved_here = [], 0
            for pp in cc:
                if isinstance(pp, dict) and isinstance(pp.get("text"), str) \
                        and len(pp["text"]) >= MIN_FOLD_CHARS:
                    nt, meta = fold_text(pp["text"])
                    if meta["saved_chars"] > 0:
                        pp = dict(pp, text=nt)
                        saved_here += meta["saved_chars"]
                        unprotected += meta["unprotected"]
                        windowed = windowed or meta["windowed"]
                new_parts.append(pp)
            if saved_here > 0:
                new_vals[i] = new_parts
                saved_chars += saved_here
                folded += 1
    if not folded:
        return info
    saved_tokens = int(saved_chars / CHAR_PER_TOKEN)
    info.update(folded=folded, saved_chars=saved_chars, saved_tokens=saved_tokens,
                est_after=max(0, est_before - saved_tokens), windowed=windowed,
                folded_chars=saved_chars)
    _bump("folded_messages", folded)
    _bump("saved_chars", saved_chars)
    _bump("saved_tokens", saved_tokens)
    _bump("unprotected", unprotected)
    if mode == "shadow":
        _bump("shadow_only")
        return info
    # enforce：只替换 content，不增删消息、不动 role / tool_call_id
    for i, value in new_vals.items():
        try:
            msgs[i]["content"] = value
        except Exception:                                # noqa: BLE001
            pass
    return info
