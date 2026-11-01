#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catalog.py —— HUB NEO 模型命名规范、分组校验与策略加载器 (NEO-GW-01)。

三层命名：
- ``bare_id``      模型短名，如 ``glm-5.3``；
- ``canonical_id`` 渠道全称 ``channel/bare_id``，如 ``workbuddy-intl/glm-5.3``；
- ``group_id``     分组名，必须以 ``auto/`` 开头，如 ``auto/code-heavy``。

分组成员一律使用 canonical 全称；加载时对未登记模型做 Fail-Closed 防呆。
纯标准库实现，不引入任何第三方依赖。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .model_attributes import auto_match_attributes

GROUP_PREFIX = "auto/"

__all__ = [
    "GROUP_PREFIX",
    "normalize_canonical",
    "parse_canonical",
    "validate_group_definition",
    "ModelGroupCatalog",
]


def normalize_canonical(channel: str, model_id: str) -> str:
    """拼装渠道全称 ``channel/model_id``（两侧去空白）。"""
    if not isinstance(channel, str) or not isinstance(model_id, str):
        raise ValueError("channel 与 model_id 必须为字符串")
    channel = channel.strip()
    model_id = model_id.strip()
    if not channel or not model_id:
        raise ValueError("channel 与 model_id 不可为空")
    if "/" in channel:
        raise ValueError("channel 不可包含斜杠: %s" % channel)
    return "%s/%s" % (channel, model_id)


def parse_canonical(canonical_id: str) -> tuple[str, str]:
    """解析 ``channel/model_id``；缺少渠道、模型为空或非字符串一律抛 ValueError。"""
    if not isinstance(canonical_id, str):
        raise ValueError("canonical_id 必须为字符串")
    value = canonical_id.strip()
    if "/" not in value:
        raise ValueError("非法规范全称（缺少渠道前缀）: %s" % canonical_id)
    channel, model_id = value.split("/", 1)
    channel, model_id = channel.strip(), model_id.strip()
    if not channel or not model_id:
        raise ValueError("非法规范全称（渠道或模型为空）: %s" % canonical_id)
    return channel, model_id


def _normalize_member(group_id: str, index: int, member: object,
                      registered: set[str] | None,
                      attr_store: object | None = None) -> dict:
    """校验并归一化单个成员；返回带派生 canonical ``name`` 与属性画像的副本。"""
    if not isinstance(member, dict):
        raise ValueError("分组 %s 第 %d 个成员必须为对象" % (group_id, index + 1))
    channel = member.get("channel")
    model_id = member.get("id")
    try:
        canonical = normalize_canonical(channel, model_id)
    except ValueError as exc:
        raise ValueError("分组 %s 第 %d 个成员非法: %s"
                         % (group_id, index + 1, exc)) from exc

    if registered is not None and canonical not in registered:
        if not member.get("orphan", False):
            raise ValueError("未登记的非法模型ID: %s" % canonical)

    out = copy.deepcopy(member)
    out["channel"], out["id"] = channel.strip(), model_id.strip()
    # 供分流器直接下发确实存在的具体模型；配置无需重复填写。
    out["upstream_name"] = str(member.get("upstream_name") or canonical).strip()
    out["name"] = canonical

    # 属性画像继承与去重绑定 (NEO-GW-03 / M3 §二.2)
    attrs = None
    if attr_store is not None and hasattr(attr_store, "resolve"):
        attrs = attr_store.resolve(canonical, channel=channel)
    else:
        attrs = auto_match_attributes(channel=channel, model_id=model_id)

    if attrs:
        if "domains" not in out or not out["domains"]:
            out["domains"] = list(attrs.get("domains") or [])
        if "primary_domain" not in out or not out["primary_domain"]:
            out["primary_domain"] = str(attrs.get("primary_domain") or "")
        if "tier" not in out or not out["tier"]:
            out["tier"] = str(attrs.get("tier") or "")

    return out


def validate_group_definition(group_id: str, group_data: dict,
                              registered_models: list[str] | None = None,
                              attr_store: object | None = None) -> dict:
    """校验单个分组定义并返回归一化副本。

    - ``group_id`` 必须以 ``auto/`` 开头且后缀非空；
    - ``members`` 必须为列表，每项含非空 ``channel`` 与 ``id``；
    - 传入 ``registered_models`` 时，未登记成员必须显式标记 ``orphan: true``。
    """
    if not isinstance(group_id, str):
        raise ValueError("group_id 必须为字符串")
    if group_id != group_id.strip():
        raise ValueError("分组ID 不可包含首尾空白: %r" % group_id)
    if not group_id.startswith(GROUP_PREFIX):
        raise ValueError("分组ID 必须以 %s 开头: %s" % (GROUP_PREFIX, group_id))
    if not group_id[len(GROUP_PREFIX):].strip():
        raise ValueError("分组ID 在 %s 后不可为空: %s" % (GROUP_PREFIX, group_id))
    if not isinstance(group_data, dict):
        raise ValueError("分组 %s 的定义必须为对象" % group_id)

    members = group_data.get("members")
    if not isinstance(members, list):
        raise ValueError("分组 %s 的 members 必须为列表" % group_id)

    registered = None
    if registered_models is not None:
        if not isinstance(registered_models, (list, tuple, set, frozenset)):
            raise ValueError("registered_models 必须为模型全称列表")
        registered = set(registered_models)

    normalized = [_normalize_member(group_id, i, m, registered, attr_store)
                  for i, m in enumerate(members)]
    result = copy.deepcopy(group_data)
    result["members"] = normalized
    return result


def _priority_key(member: dict) -> float:
    """priority 升序；缺失或非数值按 0 处理，保证排序稳定不抛错。"""
    value = member.get("priority", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


class ModelGroupCatalog:
    """内存模型分组目录：一次读盘校验，之后全部走缓存。"""

    def __init__(self, attribute_store: object | None = None) -> None:
        self._groups: dict[str, dict] = {}
        self._version = ""
        self._attribute_store = attribute_store

    @property
    def attribute_store(self) -> object | None:
        return self._attribute_store

    def load_from_file(self, filepath: str,
                       registered_models: list[str] | None = None) -> "ModelGroupCatalog":
        """读盘、全量校验后原子替换缓存；任一分组非法则保留旧缓存并抛 ValueError。"""
        path = Path(filepath)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("无法读取模型分组配置: %s" % filepath) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("模型分组配置不是合法 JSON: %s: %s" % (filepath, exc)) from exc
        if not isinstance(payload, dict):
            raise ValueError("模型分组配置顶层必须为对象: %s" % filepath)
        groups = payload.get("groups")
        if not isinstance(groups, dict):
            raise ValueError("模型分组配置缺少 groups 对象: %s" % filepath)

        validated = {gid: validate_group_definition(gid, data, registered_models, self._attribute_store)
                     for gid, data in groups.items()}
        self._groups = validated
        self._version = str(payload.get("version") or "")
        return self

    @property
    def version(self) -> str:
        """配置版本号（读取自顶层 version，缺省为空串）。"""
        return self._version

    def get_group(self, group_id: str) -> dict | None:
        """返回分组深拷贝；不存在返回 None。"""
        if not isinstance(group_id, str):
            return None
        group = self._groups.get(group_id)
        return copy.deepcopy(group) if group is not None else None

    def list_groups(self) -> list[dict]:
        """列出全部合法分组（附 group_id）。"""
        return [dict(copy.deepcopy(data), group_id=gid)
                for gid, data in self._groups.items()]

    def get_active_members(self, group_id: str, tier: str | None = None) -> list[dict]:
        """按 priority 升序返回活跃成员；tier 非空时只保留同档成员。"""
        group = self._groups.get(group_id)
        if group is None:
            return []
        active = [
            copy.deepcopy(member)
            for member in group.get("members") or []
            if member.get("enabled", True) is not False
            and (tier is None or member.get("tier") == tier)
        ]
        active.sort(key=_priority_key)
        return active
