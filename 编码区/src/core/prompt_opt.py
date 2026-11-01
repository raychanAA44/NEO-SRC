# -*- coding: utf-8 -*-
"""core/prompt_opt.py —— 网关前置提示词自愈中转器(NEO-GW-04 / M3 §三)。

## 职责

将客户端**首轮**模糊输入重构为高质量工程指令(意图显式化、KV 锚定、结构化输出、
抗膨胀、纯指令),后续轮次靠 Prefix Cache 吃优化红利。核心纪律:

1. **Fail-Open 绝对放行**:任何超时(1500ms 硬闸)、网络故障、异常,100% 返回原文,
   主链路绝不挂起、绝不崩溃;
2. **一次性保证**:task_id 优化过即标 `opt_done`,同任务后续轮次绝不二次优化;
3. **红线7**:显式单模型请求不携带 `x-hub-prompt-opt: 1` 一律跳过(不选则不生效);
4. **安全四道防线**(M3 §三.5):出网前最小化裁剪 / 输入隔离包裹 / 输出校验
   fail-closed-to-raw / 语言一致性;
5. **三态灰度**:off 旁路 / shadow 真调不下发 / enforce 下发改写。

纯标准库;调用为同步实现(网关 `_proxy` 为同步 handler,规格中的 async 形态
在此语境下无收益,见 ponytail 七级梯子)。
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request

__all__ = [
    "PromptOptimizer", "is_first_turn", "REFINER_SYSTEM_PROMPT",
    "PROMPT_OPT_OFF", "PROMPT_OPT_SHADOW", "PROMPT_OPT_ENFORCE",
]

PROMPT_OPT_OFF = "off"
PROMPT_OPT_SHADOW = "shadow"
PROMPT_OPT_ENFORCE = "enforce"
_MODES = (PROMPT_OPT_OFF, PROMPT_OPT_SHADOW, PROMPT_OPT_ENFORCE)

REFINER_TIMEOUT = 3.5          # 硬超时:放宽至 3.5s(实测广域网云端轻模型 P95 3.3s + 200ms 冗余,NEO-GW-06)
RAW_MIN_LEN = 12               # 末条 user 文本下限,低于此视为"无需自愈"
RAW_MAX_LEN = 4000             # 出网前截断上限(M3 §三.5 PII 最小化)
OUT_MIN_RATIO = 0.3            # 输出长度下限:0.3 × 原文
OUT_MAX_RATIO = 4.0            # 输出长度上限:4.0 × 原文
OPT_DONE_TTL = 24 * 3600       # opt_done 标记 TTL(M3 §三.7 缓存同口径)

# M3 §三.3 网关技术闭环优化指令规范(原文照录,不增删)
REFINER_SYSTEM_PROMPT = (
    "你是一个高性能 AI 工程网关的前置提示词重构专家。请将用户输入的原始模糊提示词"
    "优化为高质量工程指令,严格遵循以下网关技术约束:\n"
    "1. 意图显式化:提炼出具体目标、工程上下文与技术边界,消除模糊代词;\n"
    "2. KV Cache 保护:将不变的基础角色约束与系统规范置于首部,便于网关实现 "
    "Prefix Cache 滑动锚定;\n"
    "3. 流式协议兼容:要求目标模型优先采用结构化格式输出,工具调用严格按标准 JSON 闭合;\n"
    "4. 剪枝抗膨胀:明确要求目标模型\"仅指出关键改动行与必要代码,严禁全量打印无关文件"
    "或整段报错栈\";\n"
    "5. 输出格式:直接输出改写后的最终指令内容,严禁附加任何开场白、客套话或多余解释。"
)

_ISOLATE_OPEN = "<<<USER_RAW"
_ISOLATE_CLOSE = ">>>"


def is_first_turn(messages: list) -> bool:
    """首轮判定(M3 模式 B):len(messages) <= 2 且尾部消息为 role=user。"""
    if not isinstance(messages, list) or not messages or len(messages) > 2:
        return False
    tail = messages[-1]
    return isinstance(tail, dict) and tail.get("role") == "user"


def _user_text(message: dict) -> str:
    """兼容 content 为 str 或 [{type:text,...}] 两种主流形态。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(x for x in parts if x)
    return ""


def _last_user_text(messages: list) -> str:
    if not isinstance(messages, list) or not messages:
        return ""
    tail = messages[-1]
    if not isinstance(tail, dict) or tail.get("role") != "user":
        return ""
    return _user_text(tail)


# ── 安全防线 1:出网前最小化裁剪 ─────────────────────────────
_TOOLS_KEY_RE = re.compile(r'"tools"\s*:\s*\[', re.I)
_ENV_PATH_RE = re.compile(r"[A-Za-z]:\\[\w\\\-. ]{3,120}|/[\w\-./]{3,120}")
_FENCE_RE = re.compile(r"```[\w]*\n.*?\n```", re.S)


def sanitize_for_refiner(text: str) -> str:
    """剔除 tools 定义、代码大文件块与环境路径,截断到 RAW_MAX_LEN。"""
    out = _FENCE_RE.sub("```\n[code block omitted]\n```", str(text or ""))
    out = _ENV_PATH_RE.sub("[path]", out)
    return out[:RAW_MAX_LEN]


class PromptOptimizer:
    """提示词自愈中转器:skip 判定 → 隔离调用 → 四防线校验 → 三态裁决。

    用法::

        opt = PromptOptimizer(mode="enforce", refiner_endpoint="http://…/v1/chat/completions")
        new_text, applied, reason = opt.maybe_optimize(messages, headers, is_group, task_id)
        if applied:
            messages[-1]["content"] = new_text
    """

    def __init__(self, mode: str = PROMPT_OPT_OFF, refiner_endpoint: str = "",
                 timeout: float = None, clock=None, refiner_model: str = None,
                 refiner_token: str = ""):
        self.mode = mode if mode in _MODES else PROMPT_OPT_OFF
        self.refiner_endpoint = str(refiner_endpoint or "").strip()
        # R-2:模型名配置化;None 时读环境(缺省 auto/fast),坚决不留硬编码
        self.refiner_model = (str(refiner_model) if refiner_model is not None
                              else os.environ.get("HUB_REFINER_MODEL", "auto/fast")).strip()
        self.refiner_token = str(refiner_token or "").strip()
        self.timeout = max(0.1, float(timeout if timeout is not None
                                      else os.environ.get("HUB_REFINER_TIMEOUT", REFINER_TIMEOUT)))
        self._clock = clock or __import__("time").time
        self._opt_done: dict = {}                 # task_id -> done_at(一次性保证)
        self._lock = threading.Lock()
        self.stats = {"skip": 0, "applied": 0, "shadow": 0, "fail_open": 0}

    # ── 三态热载 ─────────────────────────────────────────────
    def set_mode(self, mode: str) -> str:
        if mode not in _MODES:
            raise ValueError("prompt_opt 模式必须是 off|shadow|enforce")
        self.mode = mode
        return self.mode

    # ── 一次性保证 ───────────────────────────────────────────
    def _is_opt_done(self, task_id: str) -> bool:
        with self._lock:
            done_at = self._opt_done.get(task_id)
        if done_at is None:
            return False
        if self._clock() - done_at > OPT_DONE_TTL:
            with self._lock:
                self._opt_done.pop(task_id, None)
            return False
        return True

    def _mark_opt_done(self, task_id: str) -> None:
        with self._lock:
            self._opt_done[task_id] = self._clock()

    # ── Skip List(M3 §三.8 全集)─────────────────────────────
    def should_skip(self, messages: list, headers: dict,
                    is_group: bool, task_id: str) -> tuple:
        """→ (skip, 原因)。满足任一条件即直通原文。"""
        if self.mode == PROMPT_OPT_OFF:
            return True, "mode_off"
        h = headers or {}
        opt_header = str(h.get("x-hub-prompt-opt") or "").strip()
        if opt_header == "0":
            return True, "header_disabled"
        if str(h.get("internal_refiner") or "").strip() == "1":
            return True, "internal_refiner"       # 防死循环
        if not is_first_turn(messages):
            return True, "not_first_turn"
        text = _last_user_text(messages)
        if len(text.strip()) < RAW_MIN_LEN:
            return True, "too_short"
        if not is_group and opt_header != "1":
            return True, "explicit_model_no_opt"  # 红线7:不选则不生效
        if task_id and self._is_opt_done(task_id):
            return True, "opt_done"               # 一次性保证
        return False, ""

    # ── 专职 Refiner 调用(1.5s 硬熔断)───────────────────────
    def optimize_prompt(self, raw_prompt: str, refiner_endpoint: str = "",
                        timeout: float = None, mode: str = None,
                        refiner_model: str = None) -> tuple:
        """→ (最终文本, 是否生效, 原因)。

        任何超时/网络故障/异常 100% Fail-Open 返回 (raw_prompt, False, "fail_open")。
        """
        raw_prompt = str(raw_prompt or "")
        endpoint = refiner_endpoint or self.refiner_endpoint
        mode = mode or self.mode
        model = (refiner_model if refiner_model is not None else self.refiner_model) or "auto/fast"
        if not endpoint:
            return raw_prompt, False, "fail_open"
        user_payload = sanitize_for_refiner(raw_prompt)
        body = json.dumps({
            "model": model,                       # 专职池模型(R-2 配置化,不再硬编码)
            "messages": [
                {"role": "system", "content": REFINER_SYSTEM_PROMPT},
                {"role": "user", "content":
                    "%s\n%s\n%s" % (_ISOLATE_OPEN, user_payload, _ISOLATE_CLOSE)},
            ],
            "temperature": 0,                     # M3 §三.6 参数纪律
            "stream": False,
            "max_tokens": 1024,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"content-type": "application/json",
                     "internal_refiner": "1"})    # 防循环依赖内部标记
        if self.refiner_token:                    # 真实上游专职池需要 Bearer(R-2 实测)
            req.add_header("authorization", "Bearer " + self.refiner_token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                doc = json.loads(res.read().decode("utf-8", "replace"))
            refined = self._extract_text(doc)
        except Exception:                         # noqa: BLE001 超时/5xx/解析一律 fail-open
            return self._finalize(raw_prompt, raw_prompt, False, "fail_open", mode)
        # 安全防线 2/3/4:输出校验 fail-closed-to-raw
        refined = self._strip_wrappers(refined)
        if not self._output_ok(refined, raw_prompt):
            return self._finalize(raw_prompt, raw_prompt, False, "fail_open", mode)
        return self._finalize(raw_prompt, refined, True, "ok", mode)

    def _finalize(self, raw, refined, applied, reason, mode):
        """三态裁决与影子标记:shadow 记录但不下发,off/enforce 按语义。"""
        if applied and mode == PROMPT_OPT_SHADOW:
            self.stats["shadow"] += 1
            return raw, False, "shadow_recorded"   # 记录 diff 不下发,对外仍原文
        if applied and mode == PROMPT_OPT_ENFORCE:
            self.stats["applied"] += 1
            return refined, True, reason
        self.stats["fail_open" if not applied else "applied"] += 1
        return raw, applied, reason

    @staticmethod
    def _extract_text(doc: dict) -> str:
        """兼容 OpenAI 形态 choices[0].message.content 与纯文本回显。"""
        if not isinstance(doc, dict):
            return ""
        try:
            return str(doc["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _strip_wrappers(text: str) -> str:
        """剥离 Refiner 可能回显的隔离符包裹。"""
        text = str(text or "").strip()
        if text.startswith(_ISOLATE_OPEN):
            text = text[len(_ISOLATE_OPEN):].lstrip(" \n")
        if text.endswith(_ISOLATE_CLOSE):
            text = text[:-len(_ISOLATE_CLOSE)].rstrip(" \n")
        return text.strip()

    @staticmethod
    def _output_ok(refined: str, raw: str) -> bool:
        """防线2 长度区间 / 防线3 无注入痕迹 / 防线4 语言一致性(主体同文种)。"""
        if not refined:
            return False
        n_raw, n_out = len(raw), len(refined)
        if not (OUT_MIN_RATIO * n_raw <= n_out <= OUT_MAX_RATIO * n_raw):
            return False
        if "tool_calls" in refined or _ISOLATE_OPEN in refined:
            return False
        if _ascii_ratio(raw) > 0.9 and _ascii_ratio(refined) <= 0.5:
            return False                           # 原文近纯英/码,结果近纯中文 → 漂移
        if _ascii_ratio(raw) <= 0.5 and _ascii_ratio(refined) > 0.9:
            return False                           # 原文近纯中文,结果近纯英 → 漂移
        return True

    # ── 主入口 ───────────────────────────────────────────────
    def maybe_optimize(self, messages: list, headers: dict,
                       is_group: bool, task_id: str) -> tuple:
        """skip 判定 + 优化 + 登记 opt_done → (最终文本, 是否生效, 原因)。

        仅当 enforce 真下发时才标记 opt_done(shadow 不消耗一次性额度,
        否则灰度期观察完就没得 enforce 了)。
        """
        skip, why = self.should_skip(messages, headers, is_group, task_id)
        if skip:
            self.stats["skip"] += 1
            text = _last_user_text(messages)
            return text, False, why
        raw = _last_user_text(messages)
        refined, applied, reason = self.optimize_prompt(raw)
        if applied and task_id:
            self._mark_opt_done(task_id)
        return refined, applied, reason


def _ascii_ratio(text: str) -> float:
    if not text:
        return 1.0
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    return sum(1 for c in chars if ord(c) < 128) / len(chars)
