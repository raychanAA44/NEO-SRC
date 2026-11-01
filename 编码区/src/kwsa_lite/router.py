# -*- coding: utf-8 -*-
"""KWSA lite 路由层：渠道别名、模型直通、auto/* 分组与任务粘性锁。

与 NEO 的裁决语义保持一致：
- 具体模型一律直通，网关不改写路由；只有 ``auto/`` 前缀触发分组调度。
- 分组按 priority 升序、smart_tier 优先 strong；task_lock 命中则复用同一模型。
- 任务锁只存内存，进程重启即失忆（NEO 同口径），不落盘、不跨实例。
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field

GROUP_PREFIX = "auto/"


def is_group_id(model: str) -> bool:
    return str(model or "").startswith(GROUP_PREFIX)


def join_url(base_url: str, path: str) -> str:
    """拼接 base_url 与入口路径；base 已带 /v1 时不再重复。"""
    base = str(base_url or "").rstrip("/")
    tail = "/" + str(path or "").lstrip("/")
    if base.endswith("/v1") and tail.startswith("/v1/"):
        return base + tail[3:]
    return base + tail


def mask_headers(headers: dict) -> dict:
    return {str(k): "●●●●●●" for k in (headers or {})}


@dataclass(frozen=True)
class Route:
    channel: str
    pricing_channel: str
    base_url: str
    token: str
    path: str
    request_model: str
    decision: bool = False
    headers: dict = field(default_factory=dict)
    group: str = ""
    locked: bool = False
    tier: str = ""
    priority: int = 0
    gate: str = ""
    egress: str = ""

    @property
    def url(self) -> str:
        return join_url(self.base_url, self.path)

    def public(self) -> dict:
        return {
            "channel": self.channel,
            "pricing_channel": self.pricing_channel,
            "base_url": self.base_url,
            "path": self.path,
            "request_model": self.request_model,
            "decision": self.decision,
            "headers": mask_headers(self.headers),
            "group": self.group,
            "locked": self.locked,
            "tier": self.tier,
            "priority": self.priority,
            "gate": self.gate,
            "egress": self.egress or "direct",
        }


class UnknownGroup(KeyError):
    """auto/* 分组未定义。"""


class NoActiveMembers(RuntimeError):
    """分组内无可用成员。"""


class TaskLockStore:
    """线程安全的 ``(task_id, group_id) -> model`` 内存粘性锁。"""

    def __init__(self, ttl_s: int = 43200, clock=time.time):
        self.ttl_s = max(1, int(ttl_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._binds: dict = {}

    def bind(self, task_id: str, group_id: str, model: str, ttl_s=None) -> None:
        if not task_id or not group_id or not model:
            raise ValueError("bind 需要 task_id / group_id / model 均非空")
        ttl = max(1, int(ttl_s or self.ttl_s))
        with self._lock:
            self._binds[(str(task_id), str(group_id))] = {
                "model": str(model),
                "expire_at": self._clock() + ttl,
            }

    def get(self, task_id: str, group_id: str):
        key = (str(task_id or ""), str(group_id or ""))
        with self._lock:
            rec = self._binds.get(key)
            if rec is None:
                return None
            if self._clock() >= rec["expire_at"]:
                self._binds.pop(key, None)
                return None
            return rec["model"]

    def release(self, task_id: str, group_id: str) -> bool:
        with self._lock:
            return self._binds.pop((str(task_id or ""), str(group_id or "")), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._binds.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._binds)

    def snapshot(self) -> list:
        now = self._clock()
        rows = []
        with self._lock:
            for (task_id, group_id), rec in list(self._binds.items()):
                if now >= rec["expire_at"]:
                    self._binds.pop((task_id, group_id), None)
                    continue
                rows.append({
                    "task_id": task_id,
                    "group_id": group_id,
                    "model": rec["model"],
                    "expire_at": rec["expire_at"],
                    "ttl_remaining": max(0, int(rec["expire_at"] - now + 0.5)),
                })
        rows.sort(key=lambda r: (r["expire_at"], r["task_id"], r["group_id"]))
        return rows


class Router:
    """一份配置快照的只读路由表 + 分组裁决器。"""

    def __init__(self, config):
        self._lock = threading.RLock()
        self.locks = TaskLockStore(config.routing.get("task_lock_ttl_s") or 43200)
        self.rebuild(config)

    def rebuild(self, config) -> None:
        aliases = {}
        owners = {}
        for name, entry in config.channels.items():
            for model in entry["models"]:
                for key in (model, "%s/%s" % (name, model), "custom/%s/%s" % (name, model)):
                    aliases[key] = (name, model)
                owners.setdefault(model, []).append((name, model))
            for alias, target in (entry.get("aliases") or {}).items():
                aliases[str(alias)] = (name, str(target))
        with self._lock:
            self.config = config
            self.aliases = aliases
            self.owners = owners
            self.groups = copy.deepcopy(config.groups)

    # ── 单模型 / 默认渠道 ──────────────────────────────────
    def _route(self, entry: dict, model: str, path: str, **meta) -> Route:
        return Route(
            channel=entry["name"],
            pricing_channel=entry["pricing_channel"],
            base_url=entry["base_url"],
            token=entry["token"],
            path=entry.get("endpoint") or path or "/v1/chat/completions",
            request_model=model,
            decision=bool(entry.get("decision")),
            headers=dict(entry.get("headers") or {}),
            gate=str(entry.get("gate") or ""),
            egress=str(entry.get("egress") or ""),
            **meta,
        )

    def _strip_channel_prefix(self, model: str) -> tuple:
        channels = sorted(self.config.channels, key=len, reverse=True)
        if model.startswith("custom/"):
            rest = model[len("custom/"):]
            for name in channels:
                if rest.startswith(name + "/"):
                    return name, rest[len(name) + 1:]
        for name in channels:
            if model.startswith(name + "/"):
                return name, model[len(name) + 1:]
        return "", model

    def resolve(self, model: str, path: str = "/v1/chat/completions") -> Route:
        raw = str(model or "").strip()
        if not raw:
            return self.default_route(path)
        _, bare = self._strip_channel_prefix(raw)
        hit = self.aliases.get(raw) or self.aliases.get(bare)
        if hit:
            channel, canonical = hit
            entry = self.config.channel(channel)
            if entry:
                # 裸模型名可能被多个渠道收录：命中的渠道若已停用，优先落到仍可用的同名单一渠道，
                # 避免「/v1/models 还列着、调用却 403」。带渠道前缀（channel/model）不参与回退，
                # 显式点名停用渠道就该拿到 403。
                if raw == bare and not self.config.model_enabled(channel, canonical):
                    for name, model in self.owners.get(bare, ()):
                        alt = self.config.channel(name)
                        if alt and name != channel and self.config.model_enabled(name, model):
                            return self._route(alt, model, path)
                return self._route(entry, canonical, path)
        return self.default_route(path, raw)

    def default_route(self, path: str = "/v1/chat/completions", model: str = "") -> Route:
        entry = self.config.channel(self.config.default_channel)
        return Route(
            channel=entry["name"],
            pricing_channel=entry["pricing_channel"],
            base_url=entry["base_url"],
            token=entry["token"],
            path=path or "/v1/chat/completions",
            request_model=model,
            decision=False,
            headers=dict(entry.get("headers") or {}),
            gate=str(entry.get("gate") or ""),
            egress=str(entry.get("egress") or ""),
        )

    # ── auto/* 分组 ────────────────────────────────────────
    def resolve_group(self, group_id: str, task_id: str = "",
                      path: str = "/v1/chat/completions") -> Route:
        with self._lock:
            group = self.groups.get(str(group_id or ""))
        if not group:
            raise UnknownGroup("未定义的分组: %s" % group_id)
        policy = group.get("policy") or {}
        lock_enabled = bool(policy.get("task_lock")) and bool(task_id)

        if lock_enabled:
            locked_model = self.locks.get(task_id, group_id)
            if locked_model:
                member = next(
                    (m for m in group["members"]
                     if m["id"] == locked_model or m["upstream_name"] == locked_model),
                    None,
                )
                entry = self.config.channel((member or {}).get("channel") or "")
                if member and entry:
                    return self._member_route(entry, member, path, group_id, locked=True)

        members = [
            m for m in group["members"]
            if m["enabled"] and self.config.channel(m["channel"] or self.config.default_channel)
        ]
        if not members:
            raise NoActiveMembers("分组 %s 内无可用活跃成员" % group_id)
        if policy.get("smart_tier"):
            strong = [m for m in members if m["tier"] == "strong"]
            if strong:
                members = strong
        chosen = members[0]
        route = self._member_route(
            self.config.channel(chosen["channel"] or self.config.default_channel),
            chosen, path, group_id, locked=False,
        )
        if lock_enabled:
            self.locks.bind(task_id, group_id, route.request_model)
        return route

    def _member_route(self, entry: dict, member: dict, path: str,
                      group_id: str, locked: bool) -> Route:
        model = member.get("upstream_name") or member.get("id") or ""
        return self._route(
            entry, model, path,
            group=group_id,
            locked=bool(locked),
            tier=str(member.get("tier") or ""),
            priority=int(member.get("priority") or 0),
        )

    # ── 观测面 ─────────────────────────────────────────────
    def catalog(self, only_enabled: bool = False) -> list:
        """模型清单。only_enabled=True 用于数据面：停用的渠道与模型不再对外宣告。"""
        rows = []
        for name, entry in self.config.channels.items():
            if only_enabled and not self.config.channel_enabled(name):
                continue
            for model in entry["models"]:
                if only_enabled and not self.config.model_enabled(name, model):
                    continue
                rows.append({
                    "id": model,
                    "object": "model",
                    "owned_by": name,
                    "channel": name,
                    "pricing_channel": entry["pricing_channel"],
                    "decision": bool(entry.get("decision")),
                    "endpoint": entry.get("endpoint") or "/v1/chat/completions",
                })
        for gid, group in self.config.groups.items():
            rows.append({
                "id": gid,
                "object": "model",
                "owned_by": "kwsa-lite",
                "group": True,
                "name": group.get("name") or gid,
                "members": len(group.get("members") or []),
            })
        return rows

    def summary(self) -> dict:
        return {
            "default_channel": self.config.default_channel,
            "aliases": {
                key: {"channel": hit[0], "model": hit[1]}
                for key, hit in self.aliases.items()
            },
            "channels": [
                {
                    "channel": name,
                    "base_url": entry["base_url"],
                    "models": list(entry["models"]),
                    "endpoint": entry.get("endpoint") or "/v1/chat/completions",
                    "decision": bool(entry.get("decision")),
                    "token_present": bool(entry["token"]),
                }
                for name, entry in self.config.channels.items()
            ],
            "groups": [
                {"id": gid, "name": g["name"], "members": len(g["members"]),
                 "policy": dict(g.get("policy") or {})}
                for gid, g in self.config.groups.items()
            ],
            "locks": self.locks.size(),
        }

    def health_check(self, pricebook=None) -> dict:
        """路由自检：只报配置能证实的问题，不在请求时静默兜底。"""
        issues = []

        def add(level, code, detail, **extra):
            row = {"level": level, "code": code, "detail": detail}
            row.update({k: v for k, v in extra.items() if v})
            issues.append(row)

        default = self.config.channel(self.config.default_channel)
        if not default:
            add("error", "default_channel_missing", "default_channel 指向未定义渠道")
        elif not self.config.channel_enabled(self.config.default_channel):
            add("warning", "default_channel_disabled", "默认渠道当前被停用",
                channel=self.config.default_channel)
        elif not default.get("token"):
            add("warning", "default_channel_no_token", "默认渠道没有可用密钥",
                channel=self.config.default_channel)

        for alias, (channel, target) in self.aliases.items():
            entry = self.config.channel(channel)
            if not entry:
                add("error", "alias_channel_missing", "别名指向未定义渠道",
                    channel=channel, model=alias)
            elif target not in entry.get("models", []):
                add("error", "alias_target_missing", "别名目标不在渠道模型清单",
                    channel=channel, model=alias)

        for gid, group in self.groups.items():
            members = group.get("members") or []
            if not members:
                add("error", "group_empty", "分组没有成员", group=gid)
                continue
            active = 0
            for member in members:
                channel = member.get("channel") or self.config.default_channel
                model = member.get("upstream_name") or member.get("id") or ""
                entry = self.config.channel(channel)
                if not entry:
                    add("error", "member_channel_missing", "分组成员指向未定义渠道",
                        group=gid, channel=channel, model=model)
                    continue
                if not model:
                    add("error", "member_model_missing", "分组成员缺少模型名",
                        group=gid, channel=channel)
                    continue
                if model not in entry.get("models", []):
                    add("error", "member_model_missing", "分组成员模型不在渠道清单",
                        group=gid, channel=channel, model=model)
                enabled = bool(member.get("enabled")) and self.config.channel_enabled(channel)
                if enabled:
                    active += 1
                else:
                    add("warning", "member_disabled", "分组成员当前不可用",
                        group=gid, channel=channel, model=model)
                if pricebook is not None:
                    target = entry.get("pricing_channel") or channel
                    if not pricebook.rate(model, target):
                        add("warning", "pricing_missing", "分组成员未配置价目",
                            group=gid, channel=channel, model=model)
            if not active:
                add("error", "group_no_active_members", "分组内没有可用成员", group=gid)

        errors = sum(1 for i in issues if i["level"] == "error")
        warnings = sum(1 for i in issues if i["level"] == "warning")
        return {"ok": errors == 0, "error_count": errors, "warning_count": warnings,
                "checked_at": int(time.time()), "issues": issues}

    def release_lock(self, task_id: str, group_id: str) -> bool:
        return self.locks.release(task_id, group_id)
