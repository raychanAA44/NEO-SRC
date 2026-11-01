# -*- coding: utf-8 -*-
"""core/dispatcher.py —— 请求分流裁决器:分组调度 + Task Model Lock(NEO-GW-02) + 领域分流瀑布(NEO-GW-03)。

## 裁决语义(红线 7:选则生效,不选则原样不生效)

- 客户端显式指定具体单模型(如 workbuddy-intl/glm-5.3)→ 网关绝对不干预,原样直通,零篡改。
- 仅当客户端选择分组 ID(以 auto/ 开头)才激活智能路由:
  - 粘性锁命中: 复用锁内模型(保住 Prefix Cache);
  - 硬约束过滤: 请求含图像硬信号时, 仅保留含 vision_media 成员; 无视觉成员抛 ValueError 明确拒发;
  - 领域交集过滤: 当 policy.domain_route 为真时, 优先匹配请求领域; 空交集则 Fail-Open 降级回全量活跃成员(记 domain_miss=True);
  - 档位策略过滤: 当 policy.smart_tier 为真时, 优先选择 strong 成员;
  - priority 升序挑选当选成员;
  - 若启用 task_lock 则固化绑定粘性锁。

## 与 catalog 的鸭子类型契约

dispatch(model, body, task_id, catalog) 只依赖:
1. catalog.get_group(gid) -> dict | None(未命中抛 KeyError 或返回 None,均归一为 KeyError);
2. catalog.get_active_members(gid) -> 按 priority 升序的成员列表(过滤 enabled=False)。

纯标准库实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model_attributes import (
    DOMAIN_VISION_MEDIA,
    auto_match_attributes,
    detect_task_domain,
)
from .task_lock import TaskLockStore

__all__ = [
    "ResolvedTarget",
    "RequestDispatcher",
    "GROUP_PREFIX",
    "is_group_id",
    "detect_task_domain",
]

GROUP_PREFIX = "auto/"


def is_group_id(model_name: str) -> bool:
    """判断是否分组 ID:以 auto/ 开头即分组,其余一律视为具体单模型。"""
    return str(model_name or "").startswith(GROUP_PREFIX)


@dataclass
class ResolvedTarget:
    """分流裁决结果契约:四基础字段 + NEO-GW-03 扩展字段。"""
    model: str
    is_group: bool
    locked: bool
    group_id: str = ""
    tier: str = ""
    priority: object = None
    policy: dict = field(default_factory=dict)
    domain: str = ""
    hard_vision: bool = False
    domain_miss: bool = False


class RequestDispatcher:
    """分流裁决器:直通 / 分组锁命中 / 分组首轮选模(瀑布式过滤)。"""

    def __init__(self, task_lock: TaskLockStore | None = None) -> None:
        self.task_lock = task_lock or TaskLockStore()

    @staticmethod
    def _member_domains(member: dict) -> list[str]:
        """获取成员领域列表，若未声明则按 bare_id 自动推导。"""
        domains = member.get("domains")
        if domains is not None:
            return list(domains)
        name = str(member.get("name") or "")
        bare = name.split("/", 1)[-1] if "/" in name else name
        return list(auto_match_attributes("", bare).get("domains") or [])

    def dispatch(
        self,
        model: str,
        body: dict | None,
        task_id: str,
        catalog: Any,
    ) -> ResolvedTarget:
        """按红线 7 裁决最终下发模型。"""
        model = str(model or "")
        # 1 直通分支(非分组单模型):不选则不生效,网关零干预
        if not is_group_id(model):
            return ResolvedTarget(model=model, is_group=False, locked=False)

        # 2 分组激活分支:分组必须已定义
        try:
            group = catalog.get_group(model)
        except KeyError:
            group = None
        if group is None:
            raise KeyError("未定义的分组: %s" % model)

        policy = group.get("policy") or {}
        lock_enabled = bool(policy.get("task_lock")) and bool(task_id)

        # 2a 已有粘性锁 → 直接复用(保住 Prefix Cache)
        if lock_enabled:
            locked_model = self.task_lock.get(task_id, model)
            if locked_model:
                row = None
                for m in (group.get("members") or []):
                    # 锁里存的是**下发名**（优先 upstream_name，裸名上游时
                    # 与 canonical name 不同），两列都要认，否则 HIT 行丢档位。
                    if (m.get("name") == locked_model
                            or m.get("upstream_name") == locked_model):
                        row = m
                        break
                return ResolvedTarget(
                    model=locked_model,
                    is_group=True,
                    locked=True,
                    group_id=model,
                    tier=str((row or {}).get("tier") or ""),
                    priority=(row or {}).get("priority"),
                    policy=dict(policy),
                    domain="",
                    hard_vision=False,
                    domain_miss=False,
                )

        # 2b 选模瀑布: 活跃候选池获取
        pool = catalog.get_active_members(model)
        if not pool:
            raise RuntimeError("分组 %s 内无可用活跃成员" % model)

        # 3 硬约束检查与过滤 (视觉信号)
        task_domain, scores, hard_vision = detect_task_domain(body or {})
        if hard_vision:
            vision_pool = [
                m for m in pool if DOMAIN_VISION_MEDIA in self._member_domains(m)
            ]
            if not vision_pool:
                raise ValueError("分组 %s 内无视觉能力成员，拒绝下发图像请求" % model)
            pool = vision_pool

        # 4 领域交集过滤 (policy.domain_route 为真时生效)
        domain_miss = False
        if policy.get("domain_route"):
            domain_matched = [
                m for m in pool if task_domain in self._member_domains(m)
            ]
            if domain_matched:
                pool = domain_matched
            else:
                # 领域交集为空: Fail-Open 降级回当前候选集，记录 domain_miss
                domain_miss = True

        # 5 智能档位过滤 (policy.smart_tier 为真时优先选 strong)
        if policy.get("smart_tier"):
            strong = [m for m in pool if str(m.get("tier") or "") == "strong"]
            if strong:
                pool = strong

        if not pool:
            raise RuntimeError("分组 %s 内无可用活跃成员" % model)

        chosen_member = pool[0]  # active members 已按 priority 升序排序
        # 下发名优先 upstream_name(适配裸名上游),缺省回退 canonical name
        chosen = chosen_member.get("upstream_name") or chosen_member.get("name", "")
        if not chosen:
            raise RuntimeError("分组 %s 内无可用活跃成员" % model)

        if lock_enabled:
            self.task_lock.bind(task_id, model, chosen)

        return ResolvedTarget(
            model=chosen,
            is_group=True,
            locked=False,
            group_id=model,
            tier=str(chosen_member.get("tier") or ""),
            priority=chosen_member.get("priority"),
            policy=dict(policy),
            domain=task_domain,
            hard_vision=hard_vision,
            domain_miss=domain_miss,
        )
