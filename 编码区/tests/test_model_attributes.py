# -*- coding: utf-8 -*-
"""编码区/tests/test_model_attributes.py —— 模型属性画像与领域分流单测 (NEO-GW-03)。

DoD 门禁要求:
1. 黄金样本集 (Golden Set) 24/24 命中率 100%;
2. 未知模型兜底 Fail-Safe (needs_review=True, tier=weak);
3. 视觉硬信号拦截与拒发;
4. 领域空交集 Fail-Open 降级 (domain_miss=True);
5. ModelAttributeStore 增删查改与去重真理源持久化;
6. detect_task_domain 软硬信号加权与平局裁决。
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core.dispatcher import RequestDispatcher, ResolvedTarget  # noqa: E402
from core.model_attributes import (  # noqa: E402
    ALL_DOMAINS,
    DOMAIN_BATCH_TASKS,
    DOMAIN_DESIGN_PLANNING,
    DOMAIN_DOC_PROCESSING,
    DOMAIN_FAST_CHAT,
    DOMAIN_HEAVY_CODING,
    DOMAIN_MARKET_RESEARCH,
    DOMAIN_MULTI_STEP_AGENT,
    DOMAIN_VISION_MEDIA,
    ModelAttributeStore,
    auto_match_attributes,
    detect_task_domain,
)
from core.task_lock import TaskLockStore  # noqa: E402


class TestGoldenSet(unittest.TestCase):
    """黄金样本集 24/24 硬闸门测试 (M3 §二.3)。"""

    GOLDEN_SET = [
        # 重度开发 heavy_coding
        ("trae/claude-3-7-sonnet", DOMAIN_HEAVY_CODING),
        ("anthropic/claude-3-5-sonnet-20241022", DOMAIN_HEAVY_CODING),
        ("deepseek/deepseek-coder-v2", DOMAIN_HEAVY_CODING),
        ("aliyun/qwen2.5-coder-32b", DOMAIN_HEAVY_CODING),
        ("openai/codex-davinci", DOMAIN_HEAVY_CODING),
        ("bigcode/starcoder-15b", DOMAIN_HEAVY_CODING),
        # 设计规划 design_planning
        ("deepseek/deepseek-r1", DOMAIN_DESIGN_PLANNING),
        ("openai/o1-preview", DOMAIN_DESIGN_PLANNING),
        ("openai/o1-mini", DOMAIN_DESIGN_PLANNING),
        ("openai/o3-mini", DOMAIN_DESIGN_PLANNING),
        ("deepseek/deepseek-reasoner", DOMAIN_DESIGN_PLANNING),
        ("aliyun/qwen-thinking-2.5", DOMAIN_DESIGN_PLANNING),
        # 影像制作 vision_media
        ("openai/gpt-4o", DOMAIN_VISION_MEDIA),
        ("openai/gpt-4o-mini", DOMAIN_VISION_MEDIA),
        ("anthropic/claude-3-7-sonnet-vision", DOMAIN_VISION_MEDIA),
        ("aliyun/qwen-vl-max", DOMAIN_VISION_MEDIA),
        # 文档处理 doc_processing
        ("zhipu/glm-4-long", DOMAIN_DOC_PROCESSING),
        ("moonshot/kimi-1m", DOMAIN_DOC_PROCESSING),
        ("anthropic/claude-2.1-200k", DOMAIN_DOC_PROCESSING),
        # 批量任务 batch_tasks
        ("google/gemini-2.5-flash", DOMAIN_BATCH_TASKS),
        ("openai/gpt-3.5-turbo", DOMAIN_BATCH_TASKS),
        ("anthropic/claude-3-haiku", DOMAIN_BATCH_TASKS),
        # 日常快答 fast_chat
        ("workbuddy-intl/glm-5.3", DOMAIN_FAST_CHAT),
        ("aliyun/qwen-2.5-7b", DOMAIN_FAST_CHAT),
    ]

    def test_golden_set_24_models_hit_rate_100_percent(self):
        self.assertEqual(len(self.GOLDEN_SET), 24, "黄金样本集必须包含且仅包含 24 款经典模型")
        for model_id, expected_primary in self.GOLDEN_SET:
            with self.subTest(model=model_id, expected=expected_primary):
                res = auto_match_attributes(channel="", model_id=model_id)
                self.assertEqual(
                    res["primary_domain"],
                    expected_primary,
                    "模型 %s 期望 primary_domain 为 %s, 实际为 %s"
                    % (model_id, expected_primary, res["primary_domain"]),
                )
                self.assertIn(
                    expected_primary,
                    res["domains"],
                    "primary_domain 必须存在于 domains 列表中",
                )
                self.assertFalse(res["needs_review"], "黄金集模型不得标记 needs_review")


class TestFailSafeUnknownModel(unittest.TestCase):
    """未知模型兜底断言 (Fail-Safe)。"""

    def test_unknown_model_defaults_to_weak_and_needs_review(self):
        res = auto_match_attributes("custom", "mystery-blackbox-model-v9")
        self.assertEqual(res["primary_domain"], DOMAIN_FAST_CHAT)
        self.assertEqual(res["domains"], [DOMAIN_FAST_CHAT, DOMAIN_BATCH_TASKS])
        self.assertEqual(res["tier"], "weak")
        self.assertTrue(res["needs_review"])

    def test_context_len_spec_inference(self):
        res = auto_match_attributes("custom", "mystery-model", context_len=256_000)
        self.assertIn(DOMAIN_DOC_PROCESSING, res["domains"])
        self.assertEqual(res["tier"], "mid")


class TestAttributeStore(unittest.TestCase):
    """ModelAttributeStore 持久化与去重真理源。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.store = ModelAttributeStore(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_store_crud_and_deduplication(self):
        # 1. 自动推导并缓存
        attrs1 = self.store.resolve("trae/claude-3-7-sonnet")
        self.assertEqual(attrs1["primary_domain"], DOMAIN_HEAVY_CODING)

        # 2. 跨渠道去重继承: workbuddy-intl/claude-3-7-sonnet 应命中同一真理源
        attrs2 = self.store.resolve("workbuddy-intl/claude-3-7-sonnet")
        self.assertEqual(attrs2["primary_domain"], DOMAIN_HEAVY_CODING)
        self.assertEqual(attrs1["model_id"], attrs2["model_id"])

        # 3. 人工覆盖 (Level 5 Override)
        custom_attrs = dict(attrs1, tier="strong", primary_domain=DOMAIN_DESIGN_PLANNING)
        self.store.set("claude-3-7-sonnet", custom_attrs, updated_by="manual")

        # 4. 重新加载并验证持久化
        new_store = ModelAttributeStore(self.tmp.name)
        reloaded = new_store.get("workbuddy-intl/claude-3-7-sonnet")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["primary_domain"], DOMAIN_DESIGN_PLANNING)
        self.assertEqual(reloaded["updated_by"], "manual")


class TestTaskDomainDetection(unittest.TestCase):
    """请求侧领域识别引擎测试 (detect_task_domain)。"""

    def test_hard_vision_image_url(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请分析这张图"},
                        {"type": "image_url", "image_url": {"url": "http://example.com/a.png"}},
                    ],
                }
            ]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_VISION_MEDIA)
        self.assertTrue(hard_vision)

    def test_soft_heavy_coding_error_stack(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "Traceback (most recent call last):\n  File 'a.py', line 10\nZeroDivisionError: division by zero\n请帮我修复这个 bug",
                }
            ]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_HEAVY_CODING)
        self.assertFalse(hard_vision)
        self.assertGreater(scores[DOMAIN_HEAVY_CODING], 0)

    def test_soft_design_planning(self):
        body = {
            "messages": [
                {"role": "user", "content": "请制定一份关于微服务治理与事件驱动架构的 RFC 方案蓝图，推演权衡"}
            ]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_DESIGN_PLANNING)

    def test_soft_doc_processing(self):
        body = {
            "messages": [
                {"role": "user", "content": "请对该采购合同文档进行条款摘要与合规归档审查"}
            ]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_DOC_PROCESSING)

    def test_soft_batch_tasks(self):
        body = {
            "messages": [
                {"role": "user", "content": "请批量逐条将下列 100 条数据进行格式转换与批处理归类"}
            ]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_BATCH_TASKS)

    def test_soft_multi_step_agent(self):
        body = {
            "messages": [{"role": "user", "content": "执行部署任务"}],
            "tools": [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}],
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_MULTI_STEP_AGENT)

    def test_soft_fast_chat(self):
        body = {
            "messages": [{"role": "user", "content": "这是什么？"}]
        }
        domain, scores, hard_vision = detect_task_domain(body)
        self.assertEqual(domain, DOMAIN_FAST_CHAT)

    def test_tie_breaker_priority(self):
        # 构造同分情况: 比如空内容 -> 回落 DOMAIN_HEAVY_CODING
        domain, scores, hard_vision = detect_task_domain({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(domain, DOMAIN_HEAVY_CODING)


class _FakeCatalog:
    """分流测试专用伪目录。"""

    def __init__(self, group_dict):
        self.group_dict = group_dict

    def get_group(self, gid):
        if gid not in self.group_dict:
            raise KeyError(gid)
        return self.group_dict[gid]

    def get_active_members(self, gid):
        g = self.group_dict.get(gid) or {}
        members = [m for m in g.get("members", []) if m.get("enabled", True) is not False]
        return sorted(members, key=lambda m: float(m.get("priority", 0)))


class TestDispatcherDomainRouting(unittest.TestCase):
    """分流器领域路由与瀑布过滤断言。"""

    def setUp(self):
        self.dispatcher = RequestDispatcher(task_lock=TaskLockStore())
        self.catalog = _FakeCatalog({
            "auto/text-only": {
                "policy": {"domain_route": True, "task_lock": True},
                "members": [
                    {
                        "name": "trae/claude-3-7-sonnet",
                        "priority": 1,
                        "enabled": True,
                        "domains": [DOMAIN_HEAVY_CODING],
                    },
                    {
                        "name": "workbuddy-intl/glm-5.3",
                        "priority": 2,
                        "enabled": True,
                        "domains": [DOMAIN_FAST_CHAT],
                    },
                ],
            },
            "auto/vision-group": {
                "policy": {"domain_route": True, "task_lock": True},
                "members": [
                    {
                        "name": "openai/gpt-4o",
                        "priority": 1,
                        "enabled": True,
                        "domains": [DOMAIN_VISION_MEDIA],
                    },
                    {
                        "name": "workbuddy-intl/glm-5.3",
                        "priority": 2,
                        "enabled": True,
                        "domains": [DOMAIN_FAST_CHAT],
                    },
                ],
            },
        })

    def test_vision_hard_signal_blocked_when_no_vision_member(self):
        """图像请求落入纯文本组，抛出明确 ValueError 拒绝下发。"""
        img_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "http://ex.com/1.png"}}],
                }
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            self.dispatcher.dispatch("auto/text-only", img_body, "t1", self.catalog)
        self.assertIn("无视觉能力成员", str(ctx.exception))

    def test_vision_hard_signal_selected_when_vision_member_present(self):
        """含视觉成员的组，精准选择 vision_media 成员。"""
        img_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "http://ex.com/1.png"}}],
                }
            ]
        }
        res = self.dispatcher.dispatch("auto/vision-group", img_body, "t1", self.catalog)
        self.assertEqual(res.model, "openai/gpt-4o")
        self.assertTrue(res.hard_vision)

    def test_domain_intersection_filtering(self):
        """当命中对应领域时，过滤出最匹配的成员。"""
        code_body = {
            "messages": [
                {"role": "user", "content": "请重构此函数，修复 bug"}
            ]
        }
        res = self.dispatcher.dispatch("auto/text-only", code_body, "t1", self.catalog)
        self.assertEqual(res.model, "trae/claude-3-7-sonnet")
        self.assertEqual(res.domain, DOMAIN_HEAVY_CODING)
        self.assertFalse(res.domain_miss)

    def test_domain_empty_intersection_fail_open(self):
        """当领域无匹配成员时，Fail-Open 降级至全量活跃成员挑模 (domain_miss=True)。"""
        # 请求调研检索，但在 text-only 组只有 heavy_coding 和 fast_chat
        research_body = {
            "messages": [
                {"role": "user", "content": "请进行竞品调研检索，撰写趋势报告综述"}
            ]
        }
        res = self.dispatcher.dispatch("auto/text-only", research_body, "t1", self.catalog)
        # 降级回全量，priority 最小者 (trae/claude-3-7-sonnet, priority=1) 当选
        self.assertEqual(res.model, "trae/claude-3-7-sonnet")
        self.assertTrue(res.domain_miss)


if __name__ == "__main__":
    unittest.main()
