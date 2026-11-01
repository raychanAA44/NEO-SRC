# -*- coding: utf-8 -*-
"""core/model_attributes.py —— 模型多维属性画像、自动打标与领域识别引擎 (NEO-GW-03)。

遵循 RFC-20261024-ROUTING (M3 §二):
1. 工作倾向性八大领域字典 (Domain Taxonomy);
2. 五级瀑布自动打标引擎 (auto_match_attributes);
3. 属性持久化与去重真理源 (ModelAttributeStore);
4. 请求侧领域识别引擎 (detect_task_domain: 纯规则、零模型调用、P95 < 0.5ms)。
纯 Python 标准库实现，严禁引入任何第三方依赖 (Ponytail 原则)。
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from core import paths

# ── 1. 八大工作倾向性领域字典 ──────────────────────────────────
DOMAIN_HEAVY_CODING = "heavy_coding"
DOMAIN_DESIGN_PLANNING = "design_planning"
DOMAIN_DOC_PROCESSING = "doc_processing"
DOMAIN_BATCH_TASKS = "batch_tasks"
DOMAIN_MULTI_STEP_AGENT = "multi_step_agent"
DOMAIN_VISION_MEDIA = "vision_media"
DOMAIN_MARKET_RESEARCH = "market_research"
DOMAIN_FAST_CHAT = "fast_chat"

ALL_DOMAINS: list[str] = [
    DOMAIN_HEAVY_CODING,
    DOMAIN_DESIGN_PLANNING,
    DOMAIN_DOC_PROCESSING,
    DOMAIN_BATCH_TASKS,
    DOMAIN_MULTI_STEP_AGENT,
    DOMAIN_VISION_MEDIA,
    DOMAIN_MARKET_RESEARCH,
    DOMAIN_FAST_CHAT,
]

# 平局裁决优先级: 便宜领域永远输平局 (M3 §二.5)
DOMAIN_PRIORITY: list[str] = [
    DOMAIN_VISION_MEDIA,
    DOMAIN_HEAVY_CODING,
    DOMAIN_MULTI_STEP_AGENT,
    DOMAIN_DESIGN_PLANNING,
    DOMAIN_DOC_PROCESSING,
    DOMAIN_BATCH_TASKS,
    DOMAIN_MARKET_RESEARCH,
    DOMAIN_FAST_CHAT,
]

# ── 2. 正则与规则匹配定义 ──────────────────────────────────────
# 优先级从高到低 (Level 1 规则树)
_RULE_PATTERNS = [
    # 1. 影像制作 (vision_media -> multi_step_agent, tier=strong)
    (
        1,
        re.compile(r"(?:^|[-_./])(vl|vision|4o|4v|image)(?:[-_./]|$)|qwen-vl|glm-4v", re.IGNORECASE),
        DOMAIN_VISION_MEDIA,
        [DOMAIN_MULTI_STEP_AGENT],
        "strong",
    ),
    # 2. 设计规划 (design_planning -> market_research, tier=strong)
    (
        2,
        re.compile(r"(?:^|[-_./])(r1|o1|o3|reasoner|thinking)(?:[-_./\d]|$)", re.IGNORECASE),
        DOMAIN_DESIGN_PLANNING,
        [DOMAIN_MARKET_RESEARCH],
        "strong",
    ),
    # 3. 重度开发 (heavy_coding -> multi_step_agent, tier=strong)
    (
        3,
        re.compile(r"(?:^|[-_./])(coder|sonnet|codex|code|starcoder)(?:[-_./\d]|$)|deepseek-coder", re.IGNORECASE),
        DOMAIN_HEAVY_CODING,
        [DOMAIN_MULTI_STEP_AGENT],
        "strong",
    ),
    # 4. 文档处理 (doc_processing -> market_research, tier=mid)
    (
        4,
        re.compile(r"(?:^|[-_./])(long|1m|200k)(?:[-_./]|$)", re.IGNORECASE),
        DOMAIN_DOC_PROCESSING,
        [DOMAIN_MARKET_RESEARCH],
        "mid",
    ),
    # 5. 批量任务 (batch_tasks -> fast_chat, tier=weak)
    (
        5,
        re.compile(r"(?:^|[-_./])(flash|mini|turbo|lite|haiku)(?:[-_./]|$)", re.IGNORECASE),
        DOMAIN_BATCH_TASKS,
        [DOMAIN_FAST_CHAT],
        "weak",
    ),
    # 6. 对话默认 (fast_chat -> batch_tasks, tier=weak)
    (
        6,
        re.compile(r"(?:^|[-_./])(glm|qwen|chat|instruct)(?:[-_./\d]|$)|^glm-|^qwen-", re.IGNORECASE),
        DOMAIN_FAST_CHAT,
        [DOMAIN_BATCH_TASKS],
        "weak",
    ),
]


def _extract_bare_id(canonical_or_bare: str) -> str:
    """提取裸模型名 (bare_id)。"""
    raw = str(canonical_or_bare or "").strip()
    if "/" in raw:
        return raw.split("/", 1)[1].strip()
    return raw


# ── 3. 五级瀑布自动匹配引擎 ────────────────────────────────────
def auto_match_attributes(
    channel: str,
    model_id: str,
    context_len: int = 0,
) -> dict[str, Any]:
    """五级瀑布自动推导模型属性字典 (M3 §二.3)。

    Level 1: 显式规则树正则匹配 (置信度 high)
    Level 2: 能力参数推导 (context_len >= 200K 追加 doc_processing)
    Level 4: 未知模型兜底 Fail-Safe (置信度 low, tier=weak, needs_review=True)
    """
    bare_id = _extract_bare_id(model_id)
    name = bare_id.lower()

    matched_domains: list[str] = []
    primary_domain: str | None = None
    assigned_tier: str = "weak"
    has_pattern_hit = False

    # Level 1: 显式规则树
    for _, pattern, primary, extras, tier in _RULE_PATTERNS:
        if pattern.search(name):
            has_pattern_hit = True
            if primary_domain is None:
                primary_domain = primary
                assigned_tier = tier
            if primary not in matched_domains:
                matched_domains.append(primary)
            for ex in extras:
                if ex not in matched_domains:
                    matched_domains.append(ex)

    # Level 2: 能力参数推导
    if context_len >= 200_000:
        if DOMAIN_DOC_PROCESSING not in matched_domains:
            matched_domains.append(DOMAIN_DOC_PROCESSING)
        if primary_domain is None:
            primary_domain = DOMAIN_DOC_PROCESSING
            assigned_tier = "mid"
        elif assigned_tier == "weak":
            assigned_tier = "mid"

    # Level 4: 未知模型兜底 (Fail-Safe)
    if not has_pattern_hit and primary_domain is None:
        primary_domain = DOMAIN_FAST_CHAT
        matched_domains = [DOMAIN_FAST_CHAT, DOMAIN_BATCH_TASKS]
        assigned_tier = "weak"
        needs_review = True
    else:
        needs_review = False

    # 判断是否适合作 Refiner
    # 适选条件: weak 档或 fast_chat / batch_tasks, 排除 vision
    suitable_for_refiner = (
        (assigned_tier == "weak" or primary_domain in (DOMAIN_FAST_CHAT, DOMAIN_BATCH_TASKS))
        and primary_domain != DOMAIN_VISION_MEDIA
    )

    return {
        "model_id": bare_id,
        "primary_domain": primary_domain,
        "domains": matched_domains,
        "tier": assigned_tier,
        "needs_review": needs_review,
        "suitable_for_refiner": suitable_for_refiner,
        "updated_at": int(time.time()),
        "updated_by": "auto",
    }


# ── 4. 请求侧领域识别引擎 ────────────────────────────────────
# 关键词集合定义 (零模型、纳秒级命中)
_KW_ERROR = ("traceback", "exception", "error:", "failed", "报错", "堆栈", "崩溃")
_KW_CODE = ("重构", "修复", "bug", "函数", "编译", "单测", "refactor", "ast", "diff")
_KW_PLANNING = ("架构", "设计", "方案", "rfc", "蓝图", "推演", "权衡", "trade-off")
_KW_DOC = ("文档", "合同", "摘要", "提取", "归档", "markdown", "pdf")
_KW_BATCH = ("批量", "逐条", "batch", "格式转换", "归类", "批处理")
_KW_RESEARCH = ("调研", "竞品", "趋势", "报告", "检索", "综述")
_KW_QUESTION = ("?", "？", "什么", "怎么", "如何", "why", "how", "what", "哪", "几")


def detect_task_domain(body: dict[str, Any]) -> tuple[str, dict[str, int], bool]:
    """请求侧领域识别引擎 (M3 §二.5)。

    返回值: (primary_domain, scores_dict, hard_vision_bool)
    - P95 < 0.5ms，纯规则、同步无模型调用；
    - 硬信号: 包含 image_url / type: image 时 hard_vision=True, 直接判定 vision_media;
    - 软信号: 加权打分, 平局依 DOMAIN_PRIORITY 裁决;
    - 零分兜底: DOMAIN_HEAVY_CODING。
    """
    if not isinstance(body, dict):
        return DOMAIN_HEAVY_CODING, {d: 0 for d in ALL_DOMAINS}, False

    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    # ① 硬信号检查 (硬图像拦截)
    # 检查 body 顶层与 messages 中是否存在图像
    if body.get("image") or body.get("images") or body.get("input_image"):
        return DOMAIN_VISION_MEDIA, {d: 0 for d in ALL_DOMAINS}, True

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    ptype = str(part.get("type", "")).lower()
                    if ptype in ("image", "image_url", "input_image") or "image_url" in part:
                        return DOMAIN_VISION_MEDIA, {d: 0 for d in ALL_DOMAINS}, True
        elif isinstance(content, dict):
            ptype = str(content.get("type", "")).lower()
            if ptype in ("image", "image_url") or "image_url" in content:
                return DOMAIN_VISION_MEDIA, {d: 0 for d in ALL_DOMAINS}, True

    # ② 软信号加权打分
    scores: dict[str, int] = {d: 0 for d in ALL_DOMAINS}

    # 提取系统首部 2K 字符 + 末条 user 文本
    sys_head = ""
    user_tail = ""
    total_chars = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        cnt = msg.get("content")
        c_str = ""
        if isinstance(cnt, str):
            c_str = cnt
        elif isinstance(cnt, list):
            c_str = " ".join(
                str(p.get("text", "")) for p in cnt if isinstance(p, dict)
            )
        total_chars += len(c_str)

        if role == "system" and not sys_head:
            sys_head = c_str[:2000]
        elif role == "user":
            user_tail = c_str

    target_text = (sys_head + " " + user_tail).lower()

    # 报错栈 -> heavy_coding +3
    if any(kw in target_text for kw in _KW_ERROR):
        scores[DOMAIN_HEAVY_CODING] += 3

    # 代码意图 -> heavy_coding +2 (封顶 +6)
    code_intent_hits = sum(1 for kw in _KW_CODE if kw in target_text)
    if code_intent_hits > 0:
        scores[DOMAIN_HEAVY_CODING] += min(6, code_intent_hits * 2)

    # 代码围栏 -> heavy_coding +2
    if "```" in target_text:
        scores[DOMAIN_HEAVY_CODING] += 2

    # 架构推演 -> design_planning +2
    if any(kw in target_text for kw in _KW_PLANNING):
        scores[DOMAIN_DESIGN_PLANNING] += 2

    # 长文处理 -> doc_processing +2
    if any(kw in target_text for kw in _KW_DOC):
        scores[DOMAIN_DOC_PROCESSING] += 2

    # 超长上下文 (估算 > 60K tokens, 约 120K 字符) -> doc_processing +3
    if total_chars > 120_000:
        scores[DOMAIN_DOC_PROCESSING] += 3

    # 批量语义 -> batch_tasks +3
    if any(kw in target_text for kw in _KW_BATCH):
        scores[DOMAIN_BATCH_TASKS] += 3

    # 工具密集 (tools >= 3) -> multi_step_agent +2
    tools = body.get("tools")
    if isinstance(tools, list) and len(tools) >= 3:
        scores[DOMAIN_MULTI_STEP_AGENT] += 2

    # 多轮深链 (len(messages) > 6) -> multi_step_agent +1
    if len(messages) > 6:
        scores[DOMAIN_MULTI_STEP_AGENT] += 1

    # 调研检索 -> market_research +2
    if any(kw in target_text for kw in _KW_RESEARCH):
        scores[DOMAIN_MARKET_RESEARCH] += 2

    # 短促问答 (< 30 字符且含疑问词) -> fast_chat +1
    user_stripped = user_tail.strip()
    if 0 < len(user_stripped) < 30 and any(qw in user_stripped.lower() for qw in _KW_QUESTION):
        scores[DOMAIN_FAST_CHAT] += 1

    # ③ 平局裁决与零分兜底
    max_score = max(scores.values())
    if max_score == 0:
        return DOMAIN_HEAVY_CODING, scores, False

    for domain in DOMAIN_PRIORITY:
        if scores[domain] == max_score:
            return domain, scores, False

    return DOMAIN_HEAVY_CODING, scores, False


# ── 5. 属性持久化类 ModelAttributeStore ────────────────────────
class ModelAttributeStore:
    """模型属性持久化存储与单一真理源 (M3 §二.2)。

    以裸模型名 bare_id 为主键去重，多渠道自动继承。
    """

    def __init__(self, filepath: str | Path | None = None) -> None:
        if filepath is None:
            # 兼容运维区或 catalog 相对路径（根目录口径见 core/paths.py）
            candidate1 = paths.CONFIG_DIR / "catalog" / "model_attributes.json"
            candidate2 = paths.APP_ROOT / "catalog" / "model_attributes.json"
            if candidate1.parent.exists():
                self.filepath = candidate1
            else:
                self.filepath = candidate2
        else:
            self.filepath = Path(filepath)

        self._attributes: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """从磁盘加载属性字典，若不存在则跳过。"""
        if not self.filepath.exists():
            return
        try:
            raw = self.filepath.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                attrs = data.get("attributes")
                if isinstance(attrs, dict):
                    self._attributes = copy.deepcopy(attrs)
        except Exception:
            # 读取失败保留当前状态
            pass

    def save(self) -> None:
        """持久化保存至磁盘。"""
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "version": "1.0.0",
                "attributes": self._attributes,
            }
            tmp = self.filepath.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if os.name == "nt" and self.filepath.exists():
                self.filepath.unlink()
            tmp.replace(self.filepath)
        except Exception:
            pass

    def get(self, canonical_or_bare: str) -> dict[str, Any] | None:
        """按 bare_id 查询属性字典。"""
        bare_id = _extract_bare_id(canonical_or_bare)
        entry = self._attributes.get(bare_id)
        return copy.deepcopy(entry) if entry else None

    def set(
        self,
        canonical_or_bare: str,
        attributes: dict[str, Any],
        updated_by: str = "manual",
    ) -> dict[str, Any]:
        """设置或更新模型属性，并标记修改来源。"""
        bare_id = _extract_bare_id(canonical_or_bare)
        entry = copy.deepcopy(attributes)
        entry["model_id"] = bare_id
        entry["updated_by"] = updated_by
        entry["updated_at"] = int(time.time())
        self._attributes[bare_id] = entry
        self.save()
        return copy.deepcopy(entry)

    def resolve(
        self,
        canonical_or_bare: str,
        channel: str = "",
        context_len: int = 0,
    ) -> dict[str, Any]:
        """解析属性：已持久化则直接复用；未持久化则走自动推导并缓存。"""
        bare_id = _extract_bare_id(canonical_or_bare)
        if bare_id in self._attributes:
            return copy.deepcopy(self._attributes[bare_id])

        matched = auto_match_attributes(channel=channel, model_id=bare_id, context_len=context_len)
        self._attributes[bare_id] = matched
        self.save()
        return copy.deepcopy(matched)
