#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""model_upstreams.py —— 按模型名选择模型上游。

默认渠道沿用 ``HUB_MODEL_UPSTREAM``；额外渠道在
``运维区/configs/model_upstreams.json`` 里声明。只有显式列出的模型才会
改道，未列出的模型零篡改走默认渠道，避免同名单模型被误送。

决策模型（Ollama ``/v1/systemone``）的端点写死在渠道配置里；入口仍是
``/v1/chat/completions``，由网关把请求体 model 改写成规范 id 并改道。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path


def join_url(base_url: str, path: str) -> str:
    """拼 base_url 与入口路径；base 已带 /v1 时不再重复。"""
    base = str(base_url or "").rstrip("/")
    tail = "/" + str(path or "").lstrip("/")
    if base.endswith("/v1") and tail.startswith("/v1/"):
        return base + tail[3:]
    return base + tail


def mask_header_value(value: str) -> str:
    """自定义请求头的展示值：只出掩码，长度归一，不泄露原文。"""
    return "●●●●●●" if str(value or "") else ""


def mask_headers(headers: dict | None) -> dict:
    return {str(k): mask_header_value(v) for k, v in (headers or {}).items()}


# 这些头由网关自己管，配置里写了也不能覆盖：否则 host 会指错、长度会对不上。
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})


@dataclass(frozen=True)
class ModelRoute:
    channel: str
    pricing_channel: str
    base_url: str
    token: str
    path: str
    request_model: str
    decision: bool = False
    headers: dict = field(default_factory=dict)     # 出站自定义头（OpenCode 等专属鉴权）

    @property
    def url(self) -> str:
        return join_url(self.base_url, self.path)

    def safe_headers(self) -> dict:
        """可注入的出站头：跳过滤网保留头，键名归一成小写。"""
        return {str(k).strip(): str(v) for k, v in (self.headers or {}).items()
                if str(k).strip().lower() not in RESERVED_HEADERS}

    def public(self) -> dict:
        """脱敏快照：绝不带 token / 自定义头原文。"""
        return {
            "channel": self.channel,
            "pricing_channel": self.pricing_channel,
            "base_url": self.base_url,
            "path": self.path,
            "request_model": self.request_model,
            "decision": self.decision,
            "headers": mask_headers(self.headers),
        }


class ModelUpstreamRouter:
    """一份 model_upstreams.json 的只读路由表。"""

    def __init__(self, config_path, *, default_upstream="", default_channel="",
                 default_token="", environ=None):
        self.config_path = Path(config_path)
        self.default_upstream = str(default_upstream or "").rstrip("/")
        self.default_channel = str(default_channel or "default").strip() or "default"
        self.default_token = str(default_token or "")
        self._environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()
        self._channels = {}
        self._aliases = {}
        self._catalog = []
        self._default_route = None
        self.reload()

    # ── 读取 ────────────────────────────────────────────────
    def _read(self) -> dict:
        if not self.config_path.is_file():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _str_list(value) -> list:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def reload(self) -> dict:
        doc = self._read()
        default_channel = str(doc.get("default_channel") or self.default_channel).strip()
        default_channel = default_channel or "default"
        raw_channels = doc.get("channels")
        raw_channels = raw_channels if isinstance(raw_channels, dict) else {}

        channels, aliases, catalog = {}, {}, []
        for name, entry in raw_channels.items():
            channel = str(name or "").strip()
            if not channel or not isinstance(entry, dict):
                continue
            base = str(entry.get("base_url") or "").strip().rstrip("/")
            if not base and channel == default_channel:
                base = self.default_upstream
            if not base:
                continue
            token_env = str(entry.get("token_env") or "").strip()
            token = str(self._environ.get(token_env) or "").strip() if token_env else ""
            if not token and channel == default_channel:
                token = self.default_token
            models = self._str_list(entry.get("models"))
            raw_headers = entry.get("headers")
            headers, header_env = {}, {}
            if isinstance(raw_headers, dict):
                for hk, hv in raw_headers.items():
                    hkey = str(hk).strip()
                    if not hkey:
                        continue
                    hval = str(hv)
                    # `env:VAR` 让密钥留在环境变量/仓外 secrets，不落 model_upstreams.json；
                    # 变量缺失时不注入空头（空鉴权头比不鉴权更容易被上游判 400）。
                    if hval.startswith("env:"):
                        env_name = hval[len("env:"):].strip()
                        header_env[hkey] = env_name
                        hval = str(self._environ.get(env_name) or "").strip()
                        if not hval:
                            continue
                    headers[hkey] = hval
            endpoint = str(entry.get("endpoint") or "").strip()
            decision = bool(entry.get("decision")) or endpoint.endswith("/systemone")
            pricing = str(entry.get("pricing_channel") or channel).strip() or channel
            channels[channel] = {
                "base_url": base,
                "token": token,
                "pricing_channel": pricing,
                "endpoint": endpoint,
                "decision": decision,
                "models": models,
                "headers": headers,
                "header_env": header_env,
            }
            for model in models:
                aliases[model] = (channel, model, endpoint, decision)
                aliases["%s/%s" % (channel, model)] = (channel, model, endpoint, decision)
                aliases["custom/%s/%s" % (channel, model)] = (channel, model, endpoint, decision)
            raw_aliases = entry.get("aliases")
            if isinstance(raw_aliases, dict):
                for alias, target in raw_aliases.items():
                    alias, target = str(alias or "").strip(), str(target or "").strip()
                    if alias and target:
                        aliases[alias] = (channel, target, endpoint, decision)
            raw_catalog = entry.get("catalog")
            if isinstance(raw_catalog, list):
                for item in raw_catalog:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    row = copy.deepcopy(item)
                    row.setdefault("channel", channel)
                    row.setdefault("owned_by", channel)
                    row.setdefault("decision", decision)
                    row.setdefault("endpoint", endpoint)
                    catalog.append(row)
            else:
                for model in models:
                    catalog.append({"id": model, "name": model, "owned_by": channel,
                                    "channel": channel, "decision": decision,
                                    "endpoint": endpoint})

        if default_channel not in channels:
            base = self.default_upstream
            if base:
                channels[default_channel] = {
                    "base_url": base, "token": self.default_token,
                    "pricing_channel": default_channel, "endpoint": "",
                    "decision": False, "models": [], "headers": {},
                    "header_env": {},
                }
        default_entry = channels.get(default_channel)
        if default_entry:
            default_route = ModelRoute(
                channel=default_channel,
                pricing_channel=default_entry["pricing_channel"],
                base_url=default_entry["base_url"],
                token=default_entry["token"],
                path="/v1/chat/completions",
                request_model="",
                decision=False,
                headers=dict(default_entry.get("headers") or {}),
            )
        else:
            default_route = None

        with self._lock:
            self.default_channel = default_channel
            self._channels = channels
            self._aliases = aliases
            self._catalog = catalog
            self._default_route = default_route
        return self.summary()

    # ── 查询 ────────────────────────────────────────────────
    def _strip_channel_prefix(self, model: str) -> tuple:
        """把 custom/<渠道>/<模型> 或 <渠道>/<模型> 还原成 (渠道, 模型)。"""
        if model.startswith("custom/"):
            rest = model[len("custom/"):]
            for name in self._channels:
                if rest.startswith(name + "/"):
                    return name, rest[len(name) + 1:]
        for name in self._channels:
            if model.startswith(name + "/"):
                return name, model[len(name) + 1:]
        return "", model

    def resolve(self, model: str, path: str = "/v1/chat/completions", *,
                default_upstream: str | None = None,
                default_token: str | None = None) -> ModelRoute:
        raw = str(model or "").strip()
        if not raw:
            return self.default_route(path, upstream=default_upstream,
                                      token=default_token)
        _, bare = self._strip_channel_prefix(raw)
        hit = self._aliases.get(raw) or self._aliases.get(bare)
        if hit:
            channel, canonical, endpoint, decision = hit
            entry = self._channels[channel]
            return ModelRoute(
                channel=channel,
                pricing_channel=entry["pricing_channel"],
                base_url=entry["base_url"],
                token=entry["token"],
                path=endpoint or path,
                request_model=canonical,
                decision=decision,
                headers=dict(entry.get("headers") or {}),
            )
        return self.default_route(path, model=raw, upstream=default_upstream,
                                  token=default_token)

    def default_route(self, path: str = "/v1/chat/completions",
                      model: str = "", upstream: str | None = None,
                      token: str | None = None) -> ModelRoute:
        with self._lock:
            route = self._default_route
        base = str(upstream if upstream not in (None, "") else
                   (route.base_url if route else self.default_upstream)).rstrip("/")
        secret = str(token if token not in (None, "") else
                     (route.token if route else self.default_token))
        extra = dict(route.headers or {}) if route else {}
        if not route:
            return ModelRoute(
                channel=self.default_channel, pricing_channel=self.default_channel,
                base_url=base, token=secret,
                path=path, request_model=model, decision=False, headers=extra,
            )
        return ModelRoute(
            channel=route.channel, pricing_channel=route.pricing_channel,
            base_url=base, token=secret,
            path=path, request_model=model, decision=False, headers=extra,
        )

    def channel(self, name: str) -> dict:
        with self._lock:
            entry = self._channels.get(str(name or "").strip())
            return copy.deepcopy(entry) if entry else {}

    def token_for(self, channel: str) -> str:
        return str(self.channel(channel).get("token") or "")

    def catalog_models(self) -> list:
        with self._lock:
            return copy.deepcopy(self._catalog)

    def has_channel(self, name: str) -> bool:
        with self._lock:
            return str(name or "").strip() in self._channels

    def summary(self) -> dict:
        with self._lock:
            channels = []
            for name, entry in self._channels.items():
                headers = dict(entry.get("headers") or {})
                channels.append({
                    "channel": name,
                    "base_url": entry["base_url"],
                    "pricing_channel": entry["pricing_channel"],
                    "endpoint": entry["endpoint"] or "/v1/chat/completions",
                    "decision": entry["decision"],
                    "models": list(entry["models"]),
                    "token_present": bool(entry["token"]),
                    "headers": mask_headers(headers),
                    "header_names": sorted(headers),
                    "header_env": dict(entry.get("header_env") or {}),
                })
            return {
                "default_channel": self.default_channel,
                "channels": channels,
                "catalog_models": len(self._catalog),
                "config": str(self.config_path).replace("\\", "/"),
            }
