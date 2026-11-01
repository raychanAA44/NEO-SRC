#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KWSA lite —— 单进程轻量网关（默认 8798）。

保留 NEO 的数据面契约与路由架构：
- 多渠道路由、模型别名、``auto/*`` 分组、任务粘性锁、决策端点改写；
- 4 个转发头、W3C trace 透传、``X-Request-Id`` 回填、结构化 JSON Access Log；
- 令牌桶限流（``/health`` / ``/healthz`` / ``/metrics`` 豁免）、单实例保护；
- SQLite 用量账本、峰谷价目、输入/缓存/输出三段令牌；无预算闸门（不按金额拦截请求）；
- 面板登录（会话 + CSRF + 登录熔断）与渠道收录抽屉、控制面（渠道收录/探测/删除、模型开关、任务锁释放）；
- 官方账单接口预留：保存登录地址、账单路径、账号密码，但不发起取数请求。

不包含（明确边界）：账号池、官方账单取数、KEY 保险库、提示词/SSE/工具/上下文护栏。
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core import clock
from core.hardening import inject_security_headers
from core.rate_limiter import RateLimiter
from core.server_runtime import (
    GatewayAlreadyRunningError,
    SingleInstanceHTTPServer,
)

from . import monitor as hopmonitor
from . import ocbridge
from .audit import AUDIT_EVENTS, AuditError, AuditLog
from .auth import AuthError, COOKIE_NAME, PANEL_MIN_PASSWORD, PanelAuth, parse_cookies
from .clientkeys import ClientKeyError, ClientKeyStore
from .config import ROOT, Config, ConfigError, is_loopback_host, unavailable_label
from .egress import Egress, EgressError
from .ledger import Ledger, PriceBook, day_start
from .router import NoActiveMembers, Router, UnknownGroup, is_group_id, join_url
from .vault import SecretVault, VaultError, mask_token

VERSION = "0.6.3"
SERVICE = "kwsa-lite"
WEB_DIR = HERE / "web"
DEFAULT_CONFIG = ROOT / "运维区" / "configs" / "kwsa_lite.json"
DEFAULT_VAULT = ROOT / "运维区" / "data" / "kwsa_lite_creds.json"
DEFAULT_AUTH = ROOT / "运维区" / "data" / "kwsa_lite_auth.json"
PID_FILE = ROOT / "运维区" / "data" / "kwsa_lite.pid"
SENSITIVE_ROOTS = ("/api/admin", "/api/actuator", "/api/vault", "/admin", "/actuator")
SENSITIVE_RE = re.compile(r"^/api/keys/[^/]+/reveal$")
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})
EXEMPT_PATHS = ("/health", "/healthz", "/metrics")


class Gateway:
    """进程级状态：配置热加载、路由表、账本、限流与探针。"""

    def __init__(self, config: Config, auth: PanelAuth | None = None):
        self.config = config
        self.auth = auth
        self.audit = AuditLog(config.audit["path"], config.audit["max_bytes"])
        try:
            self.client_keys = ClientKeyStore(
                config.data_plane["keys_path"],
                default_rps=config.data_plane["default_rps"],
                default_burst=config.data_plane["default_burst"],
            )
        except ClientKeyError as exc:
            raise ConfigError("数据面 Key 仓不可用: %s" % exc) from exc
        self.ledger = Ledger(config.ledger["path"], config.pricing,
                             keep_days=config.ledger["keep_days"])
        self.router = Router(config)
        self.egress = Egress(config.egress["legs"], config.egress["default"])
        self._hops = deque(maxlen=200)
        self.rate = RateLimiter(
            rate=config.rate_limit["rps"],
            burst=config.rate_limit["burst"],
            enabled=config.rate_limit["enabled"],
        )
        self.started = time.time()
        self._reload_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self.channel_health = {}
        self._stats_lock = threading.Lock()
        self.stats = {"requests_total": 0, "upstream_errors": 0, "inflight": 0}
        self._stop = threading.Event()
        self._probe_thread = None

    # ── 配置 / 路由热加载 ───────────────────────────────────
    def refresh(self) -> bool:
        if not self.config.changed_on_disk():
            return False
        with self._reload_lock:
            if not self.config.changed_on_disk():
                return False
            self.config.reload()
            self.router.rebuild(self.config)
            self.ledger.pricebook = PriceBook(self.config.pricing)
            self.rate.set_rate(self.config.rate_limit["rps"], self.config.rate_limit["burst"])
            self.rate.enabled = self.config.rate_limit["enabled"]
            self.egress.reload(self.config.egress["legs"], self.config.egress["default"])
            keys_path = Path(self.config.data_plane["keys_path"])
            if keys_path != self.client_keys.path:
                self.client_keys = ClientKeyStore(
                    keys_path,
                    default_rps=self.config.data_plane["default_rps"],
                    default_burst=self.config.data_plane["default_burst"],
                )
            else:
                self.client_keys.default_rps = self.config.data_plane["default_rps"]
                self.client_keys.default_burst = self.config.data_plane["default_burst"]
            audit_path = Path(self.config.audit["path"])
            if audit_path != self.audit.path:
                self.audit = AuditLog(audit_path, self.config.audit["max_bytes"])
            return True

    def after_control(self) -> None:
        with self._reload_lock:
            self.router.rebuild(self.config)
            self.ledger.pricebook = PriceBook(self.config.pricing)
            self.egress.reload(self.config.egress["legs"], self.config.egress["default"])

    # ── 中间层样本 ──────────────────────────────────────────
    def record_hop(self, *, channel="", model="", ok=True, status=0,
                   route_ms=None, upstream_ms=None, ttft_ms=None, total_ms=None):
        with self._stats_lock:
            self._hops.append({
                "ts": int(time.time()),
                "channel": str(channel or ""),
                "model": str(model or ""),
                "ok": bool(ok),
                "status": int(status or 0),
                "route_ms": None if route_ms is None else round(float(route_ms), 1),
                "upstream_ms": None if upstream_ms is None else round(float(upstream_ms), 1),
                "ttft_ms": None if ttft_ms is None else round(float(ttft_ms), 1),
                "total_ms": None if total_ms is None else round(float(total_ms), 1),
            })

    def monitor_payload(self) -> dict:
        """中间层监视：自己记的样本 + 渠道探针 + 出口腿 + 呼吸灯读数。"""
        with self._stats_lock:
            samples = list(self._hops)
            stats = dict(self.stats)
        rows = []
        for row in self.channels_payload():
            requests = int(row.get("requests_today") or 0)
            errors = int(row.get("errors_today") or 0)
            rows.append({
                "channel": row["channel"],
                "status": row["status"],
                "enabled": row["enabled"],
                "latency_ms": row.get("latency_ms"),
                "checked_at": row.get("checked_at"),
                "error": row.get("error") or "",
                "requests_today": requests,
                "errors_today": errors,
                "error_rate": (round(errors / requests, 4) if requests else 0.0),
                "egress": row.get("egress") or "direct",
                "gate": row.get("gate") or "",
            })
        return {
            "ok": True,
            "stats": hopmonitor.summarize(samples),
            "hops": samples[-30:],
            "channels": rows,
            "egress": self.config.public()["egress"],
            "inflight": int(stats.get("inflight") or 0),
            "requests_total": int(stats.get("requests_total") or 0),
            "upstream_errors": int(stats.get("upstream_errors") or 0),
            "generated_at": int(time.time()),
        }

    def probe_model(self, channel: str, model: str) -> dict:
        """单模型真打一次 1-token 推理，取回「为什么不可用」的一手证据。

        不写账本、不动渠道健康计数：这是排障探针不是流量，混进用量会让统计撒谎。
        """
        entry = self.config.channel(channel)
        if not entry:
            raise ConfigError("未知渠道: %s" % channel)
        model = str(model or "").strip()
        if not model:
            raise ConfigError("model 不能为空")
        if model not in entry["models"]:
            raise ConfigError("渠道 %s 未收录模型 %s，先收录模型再复检" % (channel, model))
        route = self.router.resolve("%s/%s" % (channel, model))
        if route.channel != channel or route.request_model != model:
            raise ConfigError("探针拒绝改道：%s/%s 解析到了 %s/%s"
                              % (channel, model, route.channel, route.request_model))
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 1, "stream": False}
        if route.gate == "oc-direct":
            payload, _ = ocbridge.apply_gate(payload)
        req = urllib.request.Request(
            route.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST")
        req.add_header("accept", "application/json")
        req.add_header("content-type", "application/json")
        req.add_header("user-agent", "kwsa-lite/%s (+model-probe)" % VERSION)
        if route.gate == "oc-direct":
            for name, value in ocbridge.gate_headers(
                    user_agent=self.config.oc_bridge.get("user_agent") or "").items():
                req.add_header(name, value)
        if route.token:
            req.add_header("authorization", "Bearer " + route.token)
        for name, value in route.headers.items():
            if name.lower() not in RESERVED_HEADERS:
                req.add_header(name, value)
        started = time.perf_counter()
        status = 0
        text = ""
        network_error = ""
        try:
            with self.egress.open(req, min(20.0, float(self.config.routing["timeout_s"])),
                                  route.egress) as res:
                status = int(getattr(res, "status", 200))
                text = res.read(65536).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            text = exc.read(65536).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 探不通本身就是结论
            network_error = str(exc)[:200]
        latency = round((time.perf_counter() - started) * 1000, 1)
        ok = 200 <= status < 300
        verdict = ({"code": "ok", "label": "可用", "reason": "",
                    "evidence": "HTTP %s" % status, "fix": ""}
                   if ok else classify_upstream_error(status, text, network_error))
        return {"channel": channel, "model": model, "ok": ok, "status": status,
                "latency_ms": latency, **verdict}

    def probe_channel(self, name: str) -> dict:
        """对一个渠道跑真实分段探针；不打推理，只打探针路径。"""
        entry = self.config.channel(name)
        if not entry:
            raise ConfigError("未知渠道: %s" % name)
        headers = {}
        if entry.get("token"):
            headers["Authorization"] = "Bearer " + entry["token"]
        probe_path = entry.get("probe_path") or "/health"
        result = hopmonitor.probe_chain(
            entry["base_url"], probe_path,
            headers=headers, egress=self.egress,
            leg_id=entry.get("egress") or "",
            timeout=max(3.0, float(self.config.probe["timeout_s"]) + 4),
        )
        if not result["ok"] and result.get("status") in (404, 405) and probe_path != "/v1/models":
            # 渠道探针路径填错很常见（/health 在 /v1 基址上往往是 404）；回退到模型列表。
            fallback = hopmonitor.probe_chain(
                entry["base_url"], "/v1/models", headers=headers, egress=self.egress,
                leg_id=entry.get("egress") or "",
                timeout=max(3.0, float(self.config.probe["timeout_s"]) + 4))
            fallback["fallback_from"] = probe_path
            fallback["note"] = "探针路径 %s 不可用，已回退 /v1/models" % probe_path
            result = fallback
        result["channel"] = name
        self._mark_channel(name, result["ok"],
                           next((h["ms"] for h in result["hops"] if h["name"] == "TTFB"), None),
                           result.get("error") or "", source="probe")
        return result

    def probe_oc(self) -> dict:
        """探 OC 桥：桥模式探桥，直连模式探上游基址（只看连通，不烧额度）。"""
        oc = self.config.oc_bridge
        headers = {}
        if oc["mode"] == "bridge":
            base = oc["bridge_url"]
            if oc["token"]:
                headers["Authorization"] = "Bearer " + oc["token"]
        else:
            base = oc["upstream"]
            headers["Authorization"] = "Bearer " + ocbridge.PUBLIC_TOKEN
            headers.update(ocbridge.gate_headers(user_agent=oc["user_agent"]))
        result = hopmonitor.probe_chain(base, "/v1/models", headers=headers,
                                       egress=self.egress, leg_id=oc.get("egress") or "",
                                       timeout=10.0)
        result["mode"] = oc["mode"]
        result["channel"] = oc["channel"]
        result["models"] = []
        if result.get("ok"):
            # 探活顺带拉一次模型清单，省得手敲；拉不到只标明原因，不影响探活结论。
            url = (base.rstrip("/") + "/models" if base.rstrip("/").endswith("/v1")
                   else base.rstrip("/") + "/v1/models")
            try:
                req = urllib.request.Request(
                    url, headers={**headers, "Accept": "application/json"}, method="GET")
                with self.egress.open(req, 10.0, oc.get("egress") or "") as res:
                    blob = json.loads(res.read(2 * 1024 * 1024).decode("utf-8", "replace") or "{}")
                rows = blob.get("data") if isinstance(blob, dict) else []
                ids = []
                for item in (rows or []):
                    mid = str((item or {}).get("id") if isinstance(item, dict) else "").strip()
                    if mid and mid not in ids:
                        ids.append(mid)
                result["models"] = ids[:200]
                result["model_count"] = len(ids)
            except Exception as exc:  # noqa: BLE001 拉清单失败不影响探活
                result["models_error"] = str(exc)[:160]
        return result

    # ── 模型目录 / 官方接口预检 ─────────────────────────────
    def unavailable_payload(self) -> dict:
        """把「为什么不可用」聚合成一屏：按 (渠道, 原因码) 分组，带证据与恢复路径。"""
        rows = []
        for name, entry in self.config.channels.items():
            state = self.config.channel_state(name)
            if not state["enabled"]:
                rows.append({**state, "model_count": len(entry["models"]),
                             "models": list(entry["models"])[:8]})
                continue
            for model in entry["models"]:
                if self.config.model_enabled(name, model):
                    continue
                state = self.config.model_state(name, model)
                rows.append({**state, "model": model, "model_count": 1, "models": [model]})
        groups = {}
        for row in rows:
            key = (row["scope"], row["channel"], row.get("code") or "manual")
            bucket = groups.setdefault(key, {
                "scope": row["scope"],
                "channel": row["channel"],
                "code": row.get("code") or "manual",
                "label": row.get("label") or unavailable_label(row.get("code") or "manual"),
                "reason": row.get("reason") or "未记录原因",
                "evidence": row.get("evidence") or "",
                "fix": row.get("fix") or "",
                "since": int(row.get("since") or 0),
                "tested_at": int(row.get("tested_at") or 0),
                "count": 0,
                "models": [],
            })
            bucket["count"] += int(row.get("model_count") or 0)
            if row["scope"] == "model":
                bucket["models"].append(row["model"])
            if int(row.get("tested_at") or 0) > bucket["tested_at"]:
                bucket["tested_at"] = int(row["tested_at"])
        ordered = sorted(groups.values(),
                         key=lambda g: (-g["count"], g["channel"], g["code"]))
        total_models = sum(len(entry["models"]) for entry in self.config.channels.values())
        off = sum(int(group["count"]) for group in ordered)
        return {
            "total_off": off,
            "total_models": total_models,
            "channels_off": sum(1 for c in self.config.channels if not self.config.channel_enabled(c)),
            "groups": ordered,
            "generated_at": int(time.time()),
        }

    def catalog_payload(self) -> dict:
        models = []
        for name, entry in self.config.channels.items():
            for model in entry["models"]:
                state = self.config.model_state(name, model)
                models.append({
                    "id": model,
                    "channel": name,
                    "pricing_channel": entry["pricing_channel"],
                    "enabled": state["enabled"],
                    "channel_enabled": self.config.channel_enabled(name),
                    "unavailable": None if state["enabled"] else {
                        "scope": state["scope"],
                        "code": state.get("code") or "manual",
                        "label": state.get("label") or "",
                        "reason": state.get("reason") or "",
                        "evidence": state.get("evidence") or "",
                        "fix": state.get("fix") or "",
                        "since": int(state.get("since") or 0),
                        "tested_at": int(state.get("tested_at") or 0),
                    },
                    "decision": bool(entry.get("decision")),
                    "endpoint": entry.get("endpoint") or "/v1/chat/completions",
                    "price": self.ledger.pricebook.describe(model, name),
                })
        groups = [
            {"id": gid, "name": g["name"], "policy": dict(g.get("policy") or {}),
             "members": [dict(m) for m in g["members"]]}
            for gid, g in self.config.groups.items()
        ]
        return {
            "models": models,
            "groups": groups,
            "unavailable": self.unavailable_payload(),
            "pricing": self.ledger.pricebook.summary(),
            "routes": self.router.health_check(self.ledger.pricebook),
            "generated_at": int(time.time()),
        }

    def official_payload(self) -> dict:
        rows = []
        for name, entry in self.config.channels.items():
            configured = bool(entry.get("login_url") and entry.get("usage_path")
                              and entry.get("login_present"))
            rows.append({
                "channel": name,
                "base_url": entry["base_url"],
                "login_url": entry.get("login_url") or "",
                "usage_path": entry.get("usage_path") or "",
                "login_present": bool(entry.get("login_present")),
                "login_user_masked": _mask_user(entry.get("login_user") or ""),
                "password_present": bool(entry.get("login_password")),
                "configured": configured,
                "state": "ready" if configured else "missing",
                "connected": False,
                "source": "none",
                "reason": ("接口已配置，未接取数" if configured
                           else "缺登录地址 / 账单路径 / 账号密码"),
            })
        return {
            "connected": False,
            "source": "none",
            "configured": sum(1 for r in rows if r["configured"]),
            "total": len(rows),
            "rows": rows,
            "note": "接口已预留；本版本不发起官方取数请求。",
            "generated_at": int(time.time()),
        }

    def official_usage_payload(self) -> dict:
        """官方用量占位：接口固定回 none，绝不拿 0 冒充或偷发请求。"""
        rows = []
        for name in self.config.channels:
            rows.append({
                "channel": name,
                "source": "none",
                "connected": False,
                "state": "not_connected",
                "reason": "接口预留，本版本未接官方取数",
                "period": None,
                "usage": None,
            })
        return {"connected": False, "source": "none", "rows": rows,
                "note": "本版本不发起任何官方账单请求；需要实际数据仍走 8888。",
                "generated_at": int(time.time())}

    # ── 观测面 ──────────────────────────────────────────────
    def _mark_channel(self, name: str, ok: bool, latency_ms=None,
                      error="", source="passive") -> None:
        if not name:
            return
        with self._health_lock:
            self.channel_health[name] = {
                "ok": bool(ok),
                "latency_ms": None if latency_ms is None else round(float(latency_ms), 1),
                "checked_at": int(time.time()),
                "error": str(error or "")[:240],
                "source": source,
            }

    def channel_health_of(self, name: str) -> dict:
        with self._health_lock:
            return dict(self.channel_health.get(name) or {})

    def channels_payload(self, today=None) -> list:
        today = today or self.ledger.summary_today()
        by_channel = {row["channel"]: row for row in today.get("by_channel") or []}
        rows = []
        for name, entry in self.config.channels.items():
            health = self.channel_health_of(name)
            usage = by_channel.get(name) or {}
            rows.append({
                "channel": name,
                "default": name == self.config.default_channel,
                "base_url": entry["base_url"],
                "enabled": self.config.channel_enabled(name),
                "unavailable": None if self.config.channel_enabled(name)
                else self.config.channel_state(name),
                "models": list(entry["models"]),
                "model_count": len(entry["models"]),
                "aliases": dict(entry.get("aliases") or {}),
                "endpoint": entry["endpoint"] or "/v1/chat/completions",
                "decision": entry["decision"],
                "pricing_channel": entry.get("pricing_channel") or name,
                "probe_path": entry.get("probe_path") or "/health",
                "token_env": entry.get("token_env") or "",
                "token_present": bool(entry["token"]),
                "token_source": entry.get("token_source") or "none",
                "token_masked": mask_token(entry["token"]),
                "secret_error": self.config.secret_errors.get(name, ""),
                "login_url": entry.get("login_url") or "",
                "usage_path": entry.get("usage_path") or "",
                "login_present": bool(entry.get("login_present")),
                "login_user_masked": _mask_user(entry.get("login_user") or ""),
                "password_present": bool(entry.get("login_password")),
                "login_source": entry.get("login_source") or "none",
                "login_error": entry.get("login_error") or "",
                "header_env": dict(entry.get("header_env") or {}),
                "headers": {k: "●●●●●●" for k in (entry.get("headers") or {})},
                "status": ("ok" if health.get("ok") else
                           ("error" if health else "unknown")),
                "latency_ms": health.get("latency_ms"),
                "checked_at": health.get("checked_at"),
                "probe_source": health.get("source") or "",
                "error": health.get("error") or "",
                "requests_today": int(usage.get("requests") or 0),
                "errors_today": int(usage.get("errors") or 0),
                "cost_cny": float(usage.get("cost_cny") or 0.0),
                "egress": entry.get("egress") or "",
                "gate": entry.get("gate") or "",
                "virtual": entry.get("virtual") or "",
            })
        return rows

    def status_payload(self) -> dict:
        today = self.ledger.summary_today()
        channels = self.channels_payload(today)
        degraded = any(c["enabled"] and c["status"] == "error" for c in channels)
        with self._stats_lock:
            stats = dict(self.stats)
        return {
            "ok": True,
            "service": SERVICE,
            "version": VERSION,
            "status": "degraded" if degraded else "ok",
            "host": self.config.server["host"],
            "port": self.config.server["port"],
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self.started),
            "generated_at": int(time.time()),
            "channels": channels,
            "default_channel": self.config.default_channel,
            "today": today,
            "catalog": self.catalog_payload(),
            "official": self.official_payload(),
            "recent": self.ledger.recent(limit=30),
            "rate_limit": dict(self.config.rate_limit),
            "data_plane": self.data_plane_payload(),
            "ledger": self.ledger.health(),
            "groups": [
                {"id": gid, "name": g["name"], "members": len(g["members"]),
                 "policy": dict(g.get("policy") or {})}
                for gid, g in self.config.groups.items()
            ],
            "locks": len(self.router.locks.snapshot()),
            "stats": stats,
            "config_path": str(self.config.path or "").replace("\\", "/"),
            "config_file": Path(self.config.path).name if self.config.path else "",
            "pricing_note": self.config.pricing.get("note") or "",
            "probe": dict(self.config.probe),
            "auth": {
                "enabled": bool(self.config.auth["enabled"]),
                "username": self.config.auth["username"],
                "sessions": self.auth.sessions_count() if self.auth else 0,
                "cookie_secure": bool(self.config.auth["cookie_secure"]),
                "trust_proxy": bool(self.config.auth["trust_proxy"]),
            },
            "egress": self.config.public()["egress"],
            "egress_warnings": dict(self.config.egress_warnings),
            "oc_bridge": self.config.public()["oc_bridge"],
            "monitor": {
                "stats": hopmonitor.summarize(list(self._hops)),
                "inflight": int(stats.get("inflight") or 0),
                "requests_total": int(stats.get("requests_total") or 0),
                "upstream_errors": int(stats.get("upstream_errors") or 0),
            },
            "contract": self.contract_checks(),
        }

    def data_plane_payload(self, rows=None) -> dict:
        rows = self.client_keys.snapshot() if rows is None else rows
        active = sum(1 for row in rows if row["enabled"])
        return {
            "require_key": bool(self.config.data_plane["require_key"]),
            "active": active,
            "total": len(rows),
            "default_rps": self.config.data_plane["default_rps"],
            "default_burst": self.config.data_plane["default_burst"],
        }

    def contract_checks(self) -> list:
        """控制台契约核对：只报当前配置能证实的项，不写死全绿。"""
        rate = self.config.rate_limit
        port = self.config.server["port"]
        rate_label = ("令牌桶 %.0f req/s · burst %.0f" % (rate["rps"], rate["burst"])
                      if rate["enabled"] else "令牌桶已关闭")
        return [
            {"id": "forward", "label": "X-Forwarded-For / Proto / Real-IP / Host",
             "ref": "§2.1", "ok": True},
            {"id": "trace", "label": "traceparent 透传 · X-Request-Id 回填",
             "ref": "§2.2", "ok": True},
            {"id": "rate", "label": rate_label, "ref": "§2.3",
             "ok": bool(rate["enabled"])},
            {"id": "sensitive", "label": "/api/admin|actuator · /admin|actuator 403",
             "ref": "§2.4", "ok": True},
            {"id": "reload", "label": "配置 mtime 热重载 · 不重启",
             "ref": "§2.5", "ok": bool(self.config.path)},
            {"id": "log", "label": "JSON Access Log · 7 字段",
             "ref": "§2.6", "ok": True},
            {"id": "exempt", "label": "/health · /healthz · /metrics 豁免",
             "ref": "§2.7", "ok": True},
            {"id": "port", "label": "%s 端口 · 8792 永久禁用" % port,
             "ref": "§2.8", "ok": port not in (8791, 8792, 8795)},
        ]

    def health_payload(self, probe_now: bool = False) -> dict:
        """公开心跳只出运行态，不带渠道、账本、配置路径与官方凭据摘要。"""
        if probe_now:
            self.probe_all()
        degraded = any(
            row["enabled"] and row["status"] == "error"
            for row in self.channels_payload(self.ledger.summary_today())
        )
        return {
            "ok": True,
            "service": SERVICE,
            "version": VERSION,
            "status": "degraded" if degraded else "ok",
            "port": self.config.server["port"],
            "uptime_s": int(time.time() - self.started),
            "generated_at": int(time.time()),
            "default_channel": self.config.default_channel,
            "probe": dict(self.config.probe),
            "panel_auth": "enabled" if self.config.auth["enabled"] else "disabled",
            "data_plane_auth": ("required" if self.config.data_plane["require_key"]
                                else "optional"),
            "contract": self.contract_checks(),
            "security_headers": 6,
            "write_endpoints": 5,
        }

    def metrics_text(self) -> str:
        today = self.ledger.summary_today()
        with self._stats_lock:
            stats = dict(self.stats)
        lines = [
            "# HELP kwsa_lite_up 1 while the gateway process is serving",
            "# TYPE kwsa_lite_up gauge",
            "kwsa_lite_up 1",
            "# TYPE kwsa_lite_requests_total counter",
            "kwsa_lite_requests_total %d" % stats["requests_total"],
            "# TYPE kwsa_lite_requests_in_flight gauge",
            "kwsa_lite_requests_in_flight %d" % stats["inflight"],
            "# TYPE kwsa_lite_upstream_errors_total counter",
            "kwsa_lite_upstream_errors_total %d" % stats["upstream_errors"],
            "# TYPE kwsa_lite_cost_cny_today gauge",
            "kwsa_lite_cost_cny_today %.6f" % float(today.get("cost_cny") or 0.0),
        ]
        for channel in self.channels_payload(today):
            value = 1 if channel["status"] == "ok" else 0
            lines.append('kwsa_lite_channel_up{channel="%s"} %d'
                         % (channel["channel"].replace('"', ""), value))
        return "\n".join(lines) + "\n"

    # ── 探针 ────────────────────────────────────────────────
    def probe_all(self) -> dict:
        results = {}
        timeout = self.config.probe["timeout_s"]
        for name, entry in self.config.channels.items():
            parts = urlsplit(entry["base_url"])
            root = "%s://%s" % (parts.scheme, parts.netloc)
            url = root.rstrip("/") + entry.get("probe_path", "/health")
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(url, timeout=timeout) as res:
                    status = int(getattr(res, "status", 200))
                    ok = 200 <= status < 400
                    error = "" if ok else "HTTP %s" % status
            except urllib.error.HTTPError as exc:
                ok, error = False, "HTTP %s" % exc.code
            except Exception as exc:  # noqa: BLE001 探针失败不该影响请求
                ok, error = False, str(exc)[:200]
            latency = (time.perf_counter() - started) * 1000
            self._mark_channel(name, ok, latency, error, source="probe")
            results[name] = {"ok": ok, "latency_ms": round(latency, 1),
                             "url": url, "error": error}
        return results

    def start_probes(self) -> None:
        if not self.config.probe["enabled"]:
            return

        def _loop():
            while not self._stop.is_set():
                try:
                    self.probe_all()
                except Exception:  # noqa: BLE001
                    pass
                if self._stop.wait(self.config.probe["interval_s"]):
                    break

        self._probe_thread = threading.Thread(target=_loop, name="kwsa-lite-probe", daemon=True)
        self._probe_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── 统计 ────────────────────────────────────────────────
    def begin_request(self) -> None:
        with self._stats_lock:
            self.stats["requests_total"] += 1
            self.stats["inflight"] += 1

    def end_request(self, upstream_ok=True) -> None:
        with self._stats_lock:
            self.stats["inflight"] = max(0, self.stats["inflight"] - 1)
            if not upstream_ok:
                self.stats["upstream_errors"] += 1


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KWSA-lite"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    @property
    def gateway(self) -> Gateway:
        return self.server.gateway

    # ── 基础响应 ────────────────────────────────────────────
    def end_headers(self):
        rid = getattr(self, "req_id", "")
        buf = b"".join(getattr(self, "_headers_buffer", []) or []).lower()
        if rid and b"x-request-id" not in buf:
            self.send_header("X-Request-Id", rid)
        try:
            hardening_api = (getattr(self, "path", "") or "").startswith("/api/")
            inject_security_headers(self, is_api=hardening_api)
        except Exception:
            pass
        buf = b"".join(getattr(self, "_headers_buffer", []) or []).lower()
        if b"cache-control" not in buf:
            self.send_header("cache-control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Access Log 已覆盖 API / 代理；默认日志只留启动与异常。
        pass

    def _request_id(self) -> str:
        rid = (self.headers.get("x-request-id") or "").strip()
        return rid or str(uuid.uuid4())

    def _client_ip(self) -> str:
        forwarded = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return forwarded or self._peer_ip()

    def _peer_ip(self) -> str:
        """登录熔断只信 TCP 对端，不信可伪造的 X-Forwarded-For。"""
        return self.client_address[0] if self.client_address else "127.0.0.1"

    def _is_loopback(self) -> bool:
        ip = self._peer_ip()
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _cookie_secure(self) -> bool:
        """TLS 反代终止时自动加 Secure；直连 HTTP 不误加。"""
        if self.gateway.config.auth["cookie_secure"]:
            return True
        proto = (self.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https" and self._is_loopback()

    def _throttle_ip(self) -> str:
        """只有本机反代且显式 trust_proxy 时才认 X-Forwarded-For。"""
        auth = self.gateway.config.auth
        if auth.get("trust_proxy") and self._is_loopback():
            return self._client_ip()
        return self._peer_ip()

    def _audit(self, event: str, *, username: str = "", detail: str = "") -> None:
        try:
            self.gateway.audit.record(
                event,
                username=username,
                ip=self._client_ip(),
                peer_ip=self._peer_ip(),
                detail=detail,
                user_agent=self.headers.get("user-agent") or "",
            )
        except Exception:  # noqa: BLE001 审计失败不拦登录
            pass

    def _cookies(self) -> dict:
        return parse_cookies(self.headers.get("cookie") or "")

    def _session(self):
        auth = self.gateway.auth
        if auth is None:
            return None
        return auth.session(self._cookies().get(COOKIE_NAME) or "")

    def _panel_identity(self):
        """返回 `{kind,user,expires_at}`；没有有效会话/令牌返回 None。"""
        gw = self.gateway
        if not gw.config.auth["enabled"]:
            return {"kind": "local", "user": "local", "expires_at": None}
        auth = gw.auth
        if auth is not None:
            row = self._session()
            if row:
                return {"kind": "session", "user": row["user"],
                        "expires_at": row["expires_at"]}
        token = os.environ.get("KWSA_ADMIN_TOKEN") or ""
        supplied = self.headers.get("authorization") or ""
        if token and supplied.startswith("Bearer "):
            if hmac.compare_digest(supplied[7:].strip(), token):
                return {"kind": "token", "user": auth.username if auth else "admin",
                        "expires_at": None}
        return None

    def _same_origin(self) -> bool:
        host = (self.headers.get("host") or "").strip().lower()
        if not host:
            return False
        for name in ("origin", "referer"):
            raw = (self.headers.get(name) or "").strip()
            if not raw:
                continue
            try:
                parsed = urlsplit(raw)
            except ValueError:
                return False
            return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host
        # 页面带 Referrer-Policy: no-referrer；同源 fetch 靠 Sec-Fetch-Site 兜底。
        return (self.headers.get("sec-fetch-site") or "").strip().lower() == "same-origin"

    def _require_panel_read(self, head_only: bool = False) -> bool:
        if not self.gateway.config.auth["enabled"]:
            return True
        if self._panel_identity():
            return True
        self._error(401, "panel login required", "login_required", head_only=head_only)
        return False

    def _require_panel_write(self) -> bool:
        gw = self.gateway
        if not gw.config.auth["enabled"]:
            if not self._is_loopback():
                self._error(403, "control requires loopback when auth is disabled",
                            "loopback_required")
                return False
            if self.headers.get("x-kwsa-ui") != "1":
                self._error(403, "missing X-KWSA-UI header", "csrf_guard")
                return False
            return True
        identity = self._panel_identity()
        if not identity:
            self._error(401, "panel login required", "login_required")
            return False
        if identity["kind"] == "token":
            return True
        if self.headers.get("x-kwsa-ui") != "1":
            self._error(403, "missing X-KWSA-UI header", "csrf_guard")
            return False
        if not self._same_origin():
            self._error(403, "cross-origin control write rejected", "csrf_origin")
            return False
        return True

    def _serve_html(self, filename: str, status: int = 200, head_only: bool = False):
        target = WEB_DIR / filename
        try:
            body = target.read_bytes()
        except OSError:
            self._error(500, "page missing: %s" % filename, "page_missing")
            return
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        if getattr(self, "req_id", ""):
            self.send_header("X-Request-Id", self.req_id)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _read_json(self, max_bytes: int):
        length = int(self.headers.get("content-length") or 0)
        if length > max_bytes:
            self._error(413, "request body too large", "payload_too_large")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._error(400, "body must be JSON", "invalid_json")
            return None
        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object", "invalid_json")
            return None
        return data

    def _prepare(self):
        gw = self.gateway
        self.req_id = self._request_id()
        self.client_key_name = ""
        started = time.perf_counter()
        try:
            gw.refresh()
        except ConfigError as exc:
            self._error(503, "config reload failed: %s" % exc, "config_error")
            return None
        path = urlsplit(self.path).path
        if path in EXEMPT_PATHS:
            return gw
        allowed, retry_after = gw.rate.check(self._client_ip(), self.path, dict(self.headers))
        if not allowed:
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            payload = json.dumps({"error": {
                "message": "Rate limit exceeded (%.0f req/s max)" % gw.rate.rate,
                "retry_after": retry_after,
            }}).encode("utf-8")
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("retry-after", str(int(retry_after) or 1))
            self.send_header("cache-control", "no-store")
            self.send_header("X-Request-Id", self.req_id)
            self.end_headers()
            self.wfile.write(payload)
            self._access_log(429, None, started, len(payload), self._client_ip())
            return None
        if path.startswith("/v1/") and not self._data_key_gate(path, started):
            return None
        return gw

    def _data_key_gate(self, path: str, started: float) -> bool:
        """`/v1/*` 准入：Bearer 或 X-API-Key，随后按 Key 独立限流。"""
        gw = self.gateway
        supplied = ""
        raw_auth = self.headers.get("authorization") or ""
        if raw_auth.lower().startswith("bearer "):
            supplied = raw_auth[7:].strip()
        if not supplied:
            supplied = (self.headers.get("x-api-key") or "").strip()
        head_only = self.command == "HEAD"
        if supplied:
            try:
                row = gw.client_keys.authenticate(supplied)
            except ClientKeyError as exc:
                self._error(503, "client key store unreadable: %s" % exc,
                            "key_store_unreadable", head_only=head_only)
                return False
            if not row:
                if self.command not in ("GET", "HEAD", "OPTIONS"):
                    self.close_connection = True
                self._access_log(401, None, started, 0, self._client_ip())
                self._error(401, "invalid client API key", "invalid_api_key",
                            head_only=head_only,
                            headers={"WWW-Authenticate": 'Bearer realm="kwsa-lite"'})
                return False
            allowed, retry_after = gw.client_keys.allow(row, path)
            if not allowed:
                if self.command not in ("GET", "HEAD", "OPTIONS"):
                    self.close_connection = True
                self._access_log(429, None, started, 0, self._client_ip())
                self._error(429, "client key rate limit exceeded", "key_rate_limited",
                            head_only=head_only,
                            headers={"retry-after": str(int(retry_after) or 1)})
                return False
            gw.client_keys.record_use(row)
            self.client_key_name = row["name"]
            return True
        if not gw.config.data_plane["require_key"]:
            return True
        try:
            active_keys = gw.client_keys.active_count()
        except ClientKeyError as exc:
            self._error(503, "client key store unreadable: %s" % exc,
                        "key_store_unreadable", head_only=head_only)
            return False
        if active_keys == 0:
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._access_log(503, None, started, 0, self._client_ip())
            self._error(503, "data plane closed: no client key issued",
                        "client_key_missing", head_only=head_only,
                        headers={"retry-after": "30"})
            return False
        if self.command not in ("GET", "HEAD", "OPTIONS"):
            self.close_connection = True
        self._access_log(401, None, started, 0, self._client_ip())
        self._error(401, "client API key required", "api_key_required",
                    head_only=head_only,
                    headers={"WWW-Authenticate": 'Bearer realm="kwsa-lite"'})
        return False

    def _send_json(self, status: int, data: dict, head_only: bool = False,
                   cookies=None, headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(str(key), str(value))
        for cookie in (cookies or []):
            self.send_header("set-cookie", cookie)
        if getattr(self, "req_id", ""):
            self.send_header("X-Request-Id", self.req_id)
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _error(self, status: int, message: str, code: str = "", head_only: bool = False,
               headers=None):
        self._send_json(status, {"error": {"message": message, "code": code or ""}},
                        head_only=head_only, headers=headers)

    def _access_log(self, status, upstream_status, started, nbytes,
                    client_ip, route=None, model="", stream=False, upstream_ms=None):
        try:
            entry = {
                "timestamp": clock.iso_t(),
                "req_id": getattr(self, "req_id", ""),
                "client_ip": client_ip or "",
                "method": self.command,
                "path": urlsplit(self.path).path,
                "status": int(status or 0),
                "upstream_status": upstream_status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "upstream_latency_ms": (None if upstream_ms is None
                                        else round(float(upstream_ms), 1)),
                "bytes": int(nbytes or 0),
                "channel": route.channel if route else "",
                "model": model,
                "group": route.group if route else "",
                "stream": bool(stream),
                "client_key": getattr(self, "client_key_name", ""),
            }
            sys.stderr.write(json.dumps(entry, ensure_ascii=False) + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 日志失败不拦请求
            pass

    # ── 路由分发 ────────────────────────────────────────────
    def do_GET(self):
        gw = self._prepare()
        if gw is None:
            return
        path = urlsplit(self.path).path
        query = urlsplit(self.path).query
        if path in ("/health", "/healthz"):
            self._send_json(200, gw.health_payload(probe_now="probe=1" in query))
            return
        if path == "/metrics":
            body = gw.metrics_text().encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("X-Request-Id", self.req_id)
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/login", "/login.html"):
            if gw.config.auth["enabled"] and self._panel_identity():
                self.send_response(302)
                self.send_header("location", "/")
                self.send_header("content-length", "0")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                return
            self._serve_html("login.html")
            return
        if path == "/api/session":
            identity = self._panel_identity()
            self._send_json(200, {
                "ok": True,
                "authenticated": bool(identity),
                "user": identity["user"] if identity else "",
                "expires_at": identity["expires_at"] if identity else None,
                "auth_enabled": bool(gw.config.auth["enabled"]),
            })
            return
        if path in ("/api/login", "/api/logout"):
            self._send_json(405, {"error": {"message": "use POST", "code": "method_not_allowed"}},
                            headers={"allow": "POST"})
            return
        if path in ("/", "/index.html") and gw.config.auth["enabled"] and not self._panel_identity():
            self._serve_html("login.html")
            return
        if self._sensitive(path):
            self._error(403, "sensitive endpoint is disabled in KWSA lite", "forbidden")
            return
        if path.startswith("/api/") and not self._require_panel_read():
            return
        if path == "/api/status":
            self._send_json(200, gw.status_payload())
            return
        if path == "/api/usage/today":
            limit = self._limit(query, 8)
            self._send_json(200, {
                "ok": True,
                "summary": gw.ledger.summary_today(),
                "recent": gw.ledger.recent(limit=limit),
                "generated_at": int(time.time()),
            })
            return
        if path == "/api/usage":
            limit = self._limit(query, 50)
            doc = parse_qs(query)
            channel = (doc.get("channel") or [""])[0]
            status = (doc.get("status") or [""])[0]
            only_errors = (doc.get("errors") or [""])[0].lower() in ("1", "true", "yes")
            rows = gw.ledger.recent(limit=limit, channel=channel,
                                    status=int(status) if status.isdigit() else None,
                                    only_errors=only_errors)
            self._send_json(200, {"ok": True, "rows": rows, "count": len(rows),
                                  "generated_at": int(time.time())})
            return
        if path == "/api/channels":
            today = gw.ledger.summary_today()
            self._send_json(200, {"ok": True, "channels": gw.channels_payload(today),
                                  "today": today, "generated_at": int(time.time())})
            return
        if path == "/api/models":
            self._send_json(200, {"ok": True, "data": gw.router.catalog(),
                                  "pricing": gw.ledger.pricebook.summary(),
                                  "generated_at": int(time.time())})
            return
        if path == "/api/catalog":
            self._send_json(200, {"ok": True, **gw.catalog_payload()})
            return
        if path == "/api/official/sources":
            self._send_json(200, {"ok": True, **gw.official_payload()})
            return
        if path == "/api/official/usage":
            self._send_json(200, {"ok": True, **gw.official_usage_payload()})
            return
        if path == "/api/routes":
            self._send_json(200, {"ok": True, **gw.router.summary(),
                                  "check": gw.router.health_check(gw.ledger.pricebook),
                                  "default_channel": gw.config.default_channel,
                                  "generated_at": int(time.time())})
            return
        if path == "/api/groups":
            self._send_json(200, {"ok": True, "groups": gw.config.public()["groups"],
                                  "locks": gw.router.locks.snapshot()})
            return
        if path == "/api/locks":
            self._send_json(200, {"ok": True, "locks": gw.router.locks.snapshot()})
            return
        if path == "/api/pricing":
            self._send_json(200, {"ok": True, "summary": gw.ledger.pricebook.summary(),
                                  "rows": gw.ledger.pricebook.rows(),
                                  "generated_at": int(time.time())})
            return
        if path == "/api/control":
            self._send_json(200, {"ok": True, "control": gw.config.control,
                                  "config_path": str(gw.config.path or "")})
            return
        if path == "/api/monitor":
            self._send_json(200, gw.monitor_payload())
            return
        if path == "/api/usage/range":
            doc = parse_qs(query)

            def _num(name, default):
                try:
                    return float((doc.get(name) or [default])[0])
                except (TypeError, ValueError):
                    return float(default)

            tz = int(max(-43200, min(50400, _num("tz", 28800))))
            end = _num("to", time.time())
            start = _num("from", end - _num("span", 86400))
            if start >= end:
                start = end - 86400
            self._send_json(200, {"ok": True, **gw.ledger.window(start, end, tz),
                                  "generated_at": int(time.time())})
            return
        if path == "/api/security/keys":
            try:
                rows = gw.client_keys.snapshot()
            except ClientKeyError as exc:
                self._error(503, str(exc), "key_store_unreadable")
                return
            self._send_json(200, {
                "ok": True,
                **gw.data_plane_payload(rows),
                "keys": rows,
                "generated_at": int(time.time()),
            })
            return
        if path == "/api/security/audit":
            query_doc = parse_qs(query)
            limit = self._limit(query, 80)
            event = (query_doc.get("event") or [""])[0].strip()
            try:
                rows = gw.audit.tail(limit=limit, event=event)
            except AuditError as exc:
                self._error(500, str(exc), "audit_unreadable")
                return
            self._send_json(200, {
                "ok": True,
                "events": rows,
                "count": len(rows),
                "available_events": list(AUDIT_EVENTS),
                "generated_at": int(time.time()),
            })
            return
        if path == "/v1/models":
            self._send_json(200, {"object": "list",
                                  "data": gw.router.catalog(only_enabled=True),
                                  "generated_at": int(time.time())})
            return
        if path.startswith("/api/"):
            self._error(404, "unknown api path", "not_found")
            return
        if path.startswith("/v1/"):
            self._error(405, "only /v1/models is served locally; use POST for inference",
                        "method_not_allowed")
            return
        return super().do_GET()

    def do_HEAD(self):
        gw = self._prepare()
        if gw is None:
            return
        path = urlsplit(self.path).path
        if path in ("/health", "/healthz"):
            self._send_json(200, gw.health_payload(), head_only=True)
        elif path in ("/login", "/login.html"):
            self._serve_html("login.html", head_only=True)
        elif path in ("/", "/index.html") and gw.config.auth["enabled"] and not self._panel_identity():
            self._serve_html("login.html", head_only=True)
        elif path == "/v1/models":
            self._send_json(200, {"object": "list",
                                  "data": gw.router.catalog(only_enabled=True)},
                            head_only=True)
        elif self._sensitive(path):
            self._error(403, "sensitive endpoint is disabled in KWSA lite",
                        "forbidden", head_only=True)
        elif path.startswith("/api/"):
            if not self._require_panel_read(head_only=True):
                return
            self._error(404, "unknown api path", "not_found", head_only=True)
        else:
            return super().do_HEAD()

    def do_POST(self):
        gw = self._prepare()
        if gw is None:
            return
        path = urlsplit(self.path).path
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/session":
            self._send_json(405, {"error": {"message": "use GET", "code": "method_not_allowed"}},
                            headers={"allow": "GET"})
            return
        if path == "/api/auth/password":
            self._handle_password_change()
            return
        if path == "/api/security/keys":
            self._handle_security_keys()
            return
        if self._sensitive(path):
            self._error(403, "sensitive endpoint is disabled in KWSA lite", "forbidden")
            return
        if path.startswith("/api/control/") or path == "/api/locks/release":
            self._handle_control(path)
            return
        if path.startswith("/v1/"):
            self._proxy("POST")
            return
        if path.startswith("/api/"):
            if self.gateway.config.auth["enabled"] and not self._panel_identity():
                self._error(401, "panel login required", "login_required")
                return
            self._error(403, "write endpoint not allowed; KWSA lite exposes 3 panel controls",
                        "write_not_allowed")
            return
        self._error(404, "not found", "not_found")

    def do_DELETE(self):
        if self._prepare() is None:
            return
        self._error(405, "method not allowed", "method_not_allowed")

    @staticmethod
    def _sensitive(path: str) -> bool:
        clean = str(path or "").rstrip("/") or "/"
        if any(clean == root or clean.startswith(root + "/") for root in SENSITIVE_ROOTS):
            return True
        return bool(SENSITIVE_RE.match(clean))

    def _limit(self, query: str, default: int) -> int:
        raw = (parse_qs(query).get("limit") or [str(default)])[0]
        try:
            return max(1, min(int(raw), 200))
        except (TypeError, ValueError):
            return default

    # ── 代理 ────────────────────────────────────────────────
    def _proxy(self, method: str):
        gw = self.gateway
        started = time.perf_counter()
        client_ip = self._client_ip()
        max_body = int(gw.config.server["max_body_bytes"])
        length = int(self.headers.get("content-length") or 0)
        if length > max_body:
            self._access_log(413, None, started, 0, client_ip)
            self._error(413, "request body too large (%d > %d bytes)" % (length, max_body),
                        "payload_too_large")
            return
        body = self.rfile.read(length) if length > 0 else b""
        path = urlsplit(self.path).path
        if ".." in path:
            self._access_log(400, None, started, 0, client_ip)
            self._error(400, "invalid upstream path", "invalid_path")
            return

        requested_model = _body_model(body)
        route = None
        try:
            if is_group_id(requested_model):
                task_id = (self.headers.get("x-hub-task-id")
                           or self.headers.get("x-kwsa-task-id") or "").strip()
                route = gw.router.resolve_group(requested_model, task_id, path)
                body = _set_body_model(body, route.request_model)
            else:
                route = gw.router.resolve(requested_model, path)
                if requested_model and route.request_model != requested_model:
                    body = _set_body_model(body, route.request_model)
        except UnknownGroup as exc:
            self._access_log(400, None, started, 0, client_ip)
            self._error(400, str(exc), "unknown_group")
            return
        except NoActiveMembers as exc:
            self._access_log(503, None, started, 0, client_ip)
            self._error(503, str(exc), "no_active_members")
            return
        except Exception as exc:  # noqa: BLE001 路由异常不暴露堆栈
            self._access_log(503, None, started, 0, client_ip)
            self._error(503, "routing failed: %s" % exc, "routing_failed")
            return

        if route.decision:
            adapted = self._adapt_decision(body, route)
            if adapted is None:
                self._access_log(400, None, started, 0, client_ip, route=route,
                                 model=route.request_model)
                return
            body = adapted

        # OC 直连模式：客户端请求体必须能按门禁改写（强制 stream、补 read 锚点）。
        gate_forced = False
        if route.gate == "oc-direct":
            try:
                oc_payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError):
                oc_payload = None
            if not isinstance(oc_payload, dict):
                self._access_log(400, None, started, 0, client_ip, route=route,
                                 model=route.request_model)
                self._error(400, "OC 直连模式要求 JSON 请求体", "oc_payload_invalid")
                return
            client_stream = oc_payload.get("stream") is not False
            oc_payload, _gate_reused = ocbridge.apply_gate(oc_payload)
            body = json.dumps(oc_payload, ensure_ascii=False).encode("utf-8")
            gate_forced = not client_stream

        if not gw.config.channel_enabled(route.channel):
            self._access_log(403, None, started, 0, client_ip, route=route,
                             model=route.request_model)
            self._error(403, "channel disabled by local control: %s" % route.channel,
                        "channel_disabled")
            return
        if not gw.config.model_enabled(route.channel, route.request_model):
            self._access_log(403, None, started, 0, client_ip, route=route,
                             model=route.request_model)
            self._error(403, "model disabled by local control: %s" % route.request_model,
                        "model_disabled")
            return

        target = route.url
        if "?" in self.path and not route.decision:
            query = self.path.split("?", 1)[1]
            target += ("&" if "?" in target else "?") + query
        req = urllib.request.Request(target, data=body if body else None, method=method)
        if route.gate == "oc-direct":
            for name, value in ocbridge.gate_headers(
                    session=(self.headers.get("x-opencode-session") or ""),
                    user_agent=gw.config.oc_bridge.get("user_agent") or "").items():
                req.add_header(name, value)
        req.add_header("accept", self.headers.get("accept", "application/json"))
        if body:
            req.add_header("content-type", self.headers.get("content-type", "application/json"))
        if route.token:
            req.add_header("authorization", "Bearer " + route.token)
        for name, value in route.headers.items():
            if name.lower() not in RESERVED_HEADERS:
                req.add_header(name, value)
        req.add_header("X-Request-Id", self.req_id)
        for name in ("traceparent", "tracestate", "user-agent"):
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)
        upstream_host = urlsplit(route.base_url).netloc
        original_host = self.headers.get("host")
        if original_host:
            req.add_header("X-Forwarded-Host", original_host)
        if upstream_host:
            req.add_header("host", upstream_host)
        incoming_xff = (self.headers.get("x-forwarded-for") or "").strip()
        req.add_header("X-Forwarded-For", (incoming_xff + ", " + client_ip).strip(", "))
        req.add_header("X-Real-IP", client_ip)
        req.add_header("X-Forwarded-Proto", self.headers.get("x-forwarded-proto") or "http")

        route_ms = (time.perf_counter() - started) * 1000
        gw.begin_request()
        upstream_started = time.perf_counter()
        upstream_ms = None
        try:
            with gw.egress.open(req, gw.config.routing["timeout_s"], route.egress) as res:
                status = int(getattr(res, "status", 200))
                ctype = res.headers.get("content-type", "application/json")
                if "text/event-stream" in ctype.lower() and method != "HEAD" and not gate_forced:
                    payload, ttft, sent, error = self._stream_sse(
                        res, status, ctype, route, upstream_started)
                    upstream_ms = ttft
                    ok = 200 <= status < 300
                    self._record(body, payload, status, started, client_ip, route,
                                 ok=ok, error=error, ttft_ms=ttft,
                                 requested_model=requested_model)
                    gw._mark_channel(route.channel, ok, ttft, error,
                                     source="passive")
                    self._access_log(status, status, started, sent, client_ip,
                                     route=route, model=route.request_model,
                                     stream=True, upstream_ms=ttft)
                    gw.record_hop(channel=route.channel, model=route.request_model,
                                  ok=ok, status=status, route_ms=route_ms,
                                  upstream_ms=ttft, ttft_ms=ttft,
                                  total_ms=(time.perf_counter() - started) * 1000)
                    gw.end_request(upstream_ok=ok)
                    return
                payload = res.read()
                if gate_forced and "text/event-stream" in ctype.lower():
                    payload = json.dumps(ocbridge.collect_sse(payload, route.request_model),
                                         ensure_ascii=False).encode("utf-8")
                    ctype = "application/json; charset=utf-8"
                first_at = time.perf_counter()
                upstream_ms = (first_at - upstream_started) * 1000
                ok = 200 <= status < 300
                self._record(body, payload, status, started, client_ip, route, ok=ok,
                             error="" if ok else _short_error(payload),
                             ttft_ms=None, requested_model=requested_model)
                gw._mark_channel(route.channel, ok, upstream_ms,
                                 "" if ok else _short_error(payload), source="passive")
                gw.record_hop(channel=route.channel, model=route.request_model,
                              ok=ok, status=status, route_ms=route_ms,
                              upstream_ms=upstream_ms,
                              total_ms=(time.perf_counter() - started) * 1000)
                self._access_log(status, status, started, len(payload), client_ip,
                                 route=route, model=route.request_model,
                                 upstream_ms=upstream_ms)
                gw.end_request(upstream_ok=ok)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.send_header("cache-control", "no-store")
                self.send_header("X-Request-Id", self.req_id)
                self._emit_route_headers(route)
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            upstream_ms = (time.perf_counter() - upstream_started) * 1000
            error = _short_error(payload) or ("HTTP %s" % exc.code)
            self._record(body, payload, exc.code, started, client_ip, route,
                         ok=False, error=error, ttft_ms=None,
                         requested_model=requested_model)
            gw._mark_channel(route.channel, False, upstream_ms, error, source="passive")
            gw.record_hop(channel=route.channel, model=route.request_model,
                          ok=False, status=exc.code, route_ms=route_ms,
                          upstream_ms=upstream_ms,
                          total_ms=(time.perf_counter() - started) * 1000)
            self._access_log(exc.code, exc.code, started, len(payload), client_ip,
                             route=route, model=route.request_model,
                             upstream_ms=upstream_ms)
            gw.end_request(upstream_ok=False)
            self.send_response(exc.code)
            self.send_header("content-type", exc.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.send_header("X-Request-Id", self.req_id)
            self._emit_route_headers(route)
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001 上游连接失败统一 502
            self._record(body, b"", 0, started, client_ip, route, ok=False,
                         error=str(exc), ttft_ms=None,
                         requested_model=requested_model)
            gw._mark_channel(route.channel, False, None, str(exc), source="passive")
            gw.record_hop(channel=route.channel, model=route.request_model,
                          ok=False, status=502, route_ms=route_ms,
                          total_ms=(time.perf_counter() - started) * 1000)
            self._access_log(502, None, started, 0, client_ip, route=route,
                             model=route.request_model)
            gw.end_request(upstream_ok=False)
            self._error(502, "upstream connect error: %s" % exc, "bad_gateway")

    def _stream_sse(self, res, status, ctype, route, upstream_started):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("transfer-encoding", "chunked")
        self.send_header("X-Request-Id", self.req_id)
        self._emit_route_headers(route)
        self.end_headers()
        chunks, ttft, sent, error = [], None, 0, ""
        try:
            while True:
                chunk = res.read(4096)
                if not chunk:
                    break
                if ttft is None:
                    ttft = (time.perf_counter() - upstream_started) * 1000
                chunks.append(chunk)
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                sent += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = "client disconnected: %s" % exc
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            error = "stream error: %s" % exc
            self.close_connection = True
        return b"".join(chunks), ttft, sent, error

    def _emit_route_headers(self, route):
        if not route:
            return
        self.send_header("X-KWSA-Channel", route.channel)
        self.send_header("X-KWSA-Model", route.request_model)
        if route.group:
            self.send_header("X-KWSA-Group", route.group)
            self.send_header("X-KWSA-Locked", "1" if route.locked else "0")
            if route.tier:
                self.send_header("X-KWSA-Tier", route.tier)

    def _adapt_decision(self, body: bytes, route):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(400, "decision request body must be JSON", "decision_payload_invalid")
            return None
        if not isinstance(payload, dict):
            self._error(400, "decision request body must be a JSON object",
                        "decision_payload_invalid")
            return None
        if not payload.get("state") or not isinstance(payload.get("questions"), dict):
            self._error(
                400,
                "%s is a decision model; send state + questions to %s"
                % (route.request_model, route.path),
                "decision_payload_required",
            )
            return None
        for key in ("stream", "messages", "max_tokens", "temperature", "top_p",
                    "tools", "tool_choice", "response_format"):
            payload.pop(key, None)
        payload["model"] = route.request_model
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _record(self, body: bytes, payload: bytes, status: int, started: float,
                client_ip: str, route, ok: bool, error: str, ttft_ms=None,
                requested_model=""):
        requested = requested_model or _body_model(body) or route.request_model
        try:
            self.gateway.ledger.record_from_payload(
                payload, request_model=requested, status=status, ok=ok, error=error,
                elapsed_ms=(time.perf_counter() - started) * 1000, ttft_ms=ttft_ms,
                rid=self.req_id, channel=route.channel, upstream=route.base_url,
                client_ip=client_ip, group_id=route.group, locked=route.locked,
            )
        except Exception as exc:  # noqa: BLE001 记账失败不拦请求
            sys.stderr.write("[KWSA lite] ledger error: %s\n" % exc)
            sys.stderr.flush()

    # ── 面板登录 / 退出 ────────────────────────────────────
    def _handle_login(self):
        started = time.perf_counter()
        gw = self.gateway
        auth = gw.auth
        if not gw.config.auth["enabled"] or auth is None:
            self._error(400, "panel auth is disabled", "auth_disabled")
            return
        ip = self._throttle_ip()
        retry = auth.retry_after(ip)
        if retry:
            self._audit("login_throttled", detail="retry_after=%d" % retry)
            self._access_log(429, None, started, 0, ip)
            self._send_json(429, {"error": {
                "message": "登录失败次数过多，请稍后再试",
                "code": "login_throttled",
                "retry_after": retry,
            }}, headers={"retry-after": str(retry)})
            return
        data = self._read_json(8192)
        if data is None:
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not auth.verify(username, password):
            auth.note_failure(ip)
            wait = auth.retry_after(ip)
            self._audit("login_failed", username=username, detail="bad_credentials")
            self._access_log(401, None, started, 0, ip)
            headers = {"retry-after": str(wait)} if wait else None
            self._send_json(401, {"error": {
                "message": "账号或密码错误",
                "code": "bad_credentials",
                "retry_after": wait,
            }}, headers=headers)
            return
        auth.clear_failures(ip)
        row = auth.issue(auth.username)
        self._audit("login_success", username=auth.username, detail="panel session")
        self._access_log(200, None, started, 0, ip)
        self._send_json(200, {
            "ok": True,
            "user": row["user"],
            "expires_at": int(row["expires_at"]),
            "cookie_secure": self._cookie_secure(),
        }, cookies=[auth.cookie_header(row["sid"], secure=self._cookie_secure())])

    def _handle_logout(self):
        auth = self.gateway.auth
        row = self._session() if auth is not None else None
        sid = self._cookies().get(COOKIE_NAME) or ""
        if auth is not None:
            auth.revoke(sid)
        if row:
            self._audit("logout", username=row.get("user") or "", detail="session revoked")
        self._send_json(200, {"ok": True},
                        cookies=[auth.clear_cookie_header(secure=self._cookie_secure())]
                        if auth else None)

    def _handle_password_change(self):
        if not self._require_panel_write():
            return
        gw = self.gateway
        auth = gw.auth
        identity = self._panel_identity()
        if auth is None or not gw.config.auth["enabled"] or not identity:
            self._error(400, "panel auth is disabled", "auth_disabled")
            return
        data = self._read_json(8192)
        if data is None:
            return
        current = str(data.get("current_password") or "")
        new = str(data.get("new_password") or "")
        confirm = str(data.get("confirm_password") or "")
        user = identity.get("user") or auth.username
        if confirm and confirm != new:
            self._audit("password_change_failed", username=user, detail="mismatch")
            self._error(400, "两次新口令不一致", "password_mismatch")
            return
        if len(new) < PANEL_MIN_PASSWORD:
            self._audit("password_change_failed", username=user, detail="new_too_short")
            self._error(400, "新口令至少 %d 位" % PANEL_MIN_PASSWORD,
                        "password_too_short")
            return
        if hmac.compare_digest(current, new):
            self._audit("password_change_failed", username=user, detail="same_password")
            self._error(400, "新口令不能与当前口令相同", "same_password")
            return
        if not auth.verify(auth.username, current):
            self._audit("password_change_failed", username=user, detail="bad_current")
            self._error(403, "当前口令错误", "bad_current_password")
            return
        revoked = auth.sessions_count()
        try:
            auth.set_password(new, username=user)
        except AuthError as exc:
            self._audit("password_change_failed", username=user, detail=str(exc)[:120])
            self._error(400, str(exc), "password_rejected")
            return
        row = auth.issue(auth.username)
        self._audit("password_changed", username=user,
                    detail="sessions_revoked=%d" % revoked)
        self._send_json(200, {
            "ok": True,
            "user": auth.username,
            "changed_at": int(time.time()),
            "sessions_revoked": revoked,
            "cookie_secure": self._cookie_secure(),
        }, cookies=[auth.cookie_header(row["sid"], secure=self._cookie_secure())])

    def _handle_security_keys(self):
        if not self._require_panel_write():
            return
        gw = self.gateway
        identity = self._panel_identity()
        if not identity:
            self._error(401, "panel login required", "login_required")
            return
        data = self._read_json(64 * 1024)
        if data is None:
            return
        action = str(data.get("action") or "create").strip().lower()
        user = identity.get("user") or ""
        try:
            if action == "create":
                secret, row = gw.client_keys.create(
                    str(data.get("name") or ""),
                    data.get("rps"), data.get("burst"))
                self._audit("client_key_created", username=user,
                            detail="%s (%s)" % (row["name"], row["id"]))
                self._send_json(201, {
                    "ok": True,
                    "key": row,
                    "secret": secret,
                    "notice": "密钥只显示一次；请立即复制，服务端只保存哈希。",
                })
                return
            if action in ("toggle", "enable", "disable"):
                enabled = (action != "disable")
                if action == "toggle":
                    enabled = _as_bool(data.get("enabled"), True)
                row = gw.client_keys.set_enabled(str(data.get("id") or ""), enabled)
                if not row:
                    self._error(404, "client key not found", "key_not_found")
                    return
                self._audit("client_key_toggled", username=user,
                            detail="%s (%s) enabled=%s"
                            % (row["name"], row["id"], "1" if row["enabled"] else "0"))
                self._send_json(200, {"ok": True, "key": row})
                return
            if action in ("revoke", "delete"):
                row = gw.client_keys.revoke(str(data.get("id") or ""))
                if not row:
                    self._error(404, "client key not found", "key_not_found")
                    return
                self._audit("client_key_revoked", username=user,
                            detail="%s (%s)" % (row["name"], row["id"]))
                self._send_json(200, {"ok": True, "key": row})
                return
            self._error(400, "unknown client key action: %s" % action,
                        "unknown_action")
        except ClientKeyError as exc:
            self._error(400, str(exc), "client_key_error")
        except OSError as exc:
            self._error(500, "client key store failed: %s" % exc, "key_store_error")

    # ── 控制面（面板会话或 Bearer 管理令牌）─────────────────
    def _handle_control(self, path: str):
        if not self._require_panel_write():
            return
        data = self._read_control_body()
        if data is None:
            return
        gw = self.gateway
        try:
            if path == "/api/control/channel":
                action = str(data.get("action") or "").strip().lower()
                if action == "probe":
                    self._send_json(200, {"ok": True, **self._probe_upstream(data)})
                    return
                if action in ("save", "upsert", "edit"):
                    result = gw.config.upsert_channel(data)
                elif action in ("delete", "remove"):
                    result = gw.config.delete_channel(str(data.get("name") or ""))
                elif action == "default":
                    result = gw.config.set_default_channel(str(data.get("name") or ""))
                elif action == "forget_token":
                    result = gw.config.forget_channel_token(str(data.get("name") or ""))
                else:
                    result = gw.config.set_channel_enabled(
                        str(data.get("name") or ""), _as_bool(data.get("enabled")),
                        code=str(data.get("code") or ""),
                        reason=str(data.get("reason") or ""),
                        evidence=str(data.get("evidence") or ""),
                        fix=str(data.get("fix") or ""))
            elif path == "/api/control/model":
                result = gw.config.set_model_enabled(
                    str(data.get("channel") or ""), str(data.get("model") or ""),
                    _as_bool(data.get("enabled")),
                    code=str(data.get("code") or ""),
                    reason=str(data.get("reason") or ""),
                    evidence=str(data.get("evidence") or ""),
                    fix=str(data.get("fix") or ""))
            elif path == "/api/control/model/probe":
                channel = str(data.get("channel") or "")
                model = str(data.get("model") or "")
                result = gw.probe_model(channel, model)
                # 探针结论直接回写注释：面板上的「为什么不可用」永远来自最近一次实测，
                # 而不是上次手写的备注。已启用的模型不写（启用了就不该有停用理由）。
                if not result["ok"]:
                    gw.config.annotate_model(channel, model, result["code"],
                                             result["reason"], result["evidence"],
                                             result["fix"])
                elif not gw.config.model_enabled(channel, model):
                    gw.config.annotate_model(channel, model, "probe_ok",
                                             "复检通过，可启用", result["evidence"],
                                             "面板点「启用」即恢复")
            elif path == "/api/control/pricing":
                action = str(data.get("action") or "save").strip().lower()
                if action in ("save", "upsert", "edit"):
                    result = gw.config.set_price(data)
                elif action in ("delete", "remove"):
                    result = gw.config.delete_price(str(data.get("channel") or ""),
                                                    str(data.get("model") or ""))
                elif action in ("peak_rule", "rule"):
                    result = gw.config.set_peak_rule(str(data.get("channel") or ""),
                                                     data.get("rule"))
                else:
                    raise ConfigError("未知价目动作: %s" % action)
            elif path == "/api/control/group":
                action = str(data.get("action") or "add_member").strip().lower()
                if action in ("add_member", "upsert_member", "add"):
                    result = gw.config.add_group_member(data)
                elif action in ("remove_member", "delete_member", "remove"):
                    result = gw.config.remove_group_member(
                        str(data.get("group") or ""),
                        str(data.get("channel") or ""),
                        str(data.get("id") or data.get("model") or ""))
                elif action == "policy":
                    result = gw.config.set_group_policy(
                        str(data.get("group") or ""), data.get("policy") or {})
                elif action in ("delete_group", "drop"):
                    result = gw.config.delete_group(str(data.get("group") or ""))
                else:
                    raise ConfigError("未知路由矩阵动作: %s" % action)
            elif path == "/api/control/egress":
                action = str(data.get("action") or "save").strip().lower()
                if action in ("save", "upsert", "edit"):
                    result = gw.config.save_egress_leg(data)
                elif action in ("delete", "remove"):
                    result = gw.config.delete_egress_leg(str(data.get("id") or ""))
                elif action == "default":
                    result = gw.config.set_default_egress(str(data.get("id") or ""))
                elif action in ("channel", "bind"):
                    result = gw.config.set_channel_egress(
                        str(data.get("channel") or ""), str(data.get("id") or ""))
                else:
                    raise ConfigError("未知出口分路动作: %s" % action)
            elif path == "/api/control/oc":
                action = str(data.get("action") or "save").strip().lower()
                if action == "probe":
                    self._send_json(200, {"ok": True, **gw.probe_oc()})
                    return
                if action in ("save", "enable", "disable"):
                    payload = dict(data)
                    if action == "enable":
                        payload["enabled"] = True
                    elif action == "disable":
                        payload["enabled"] = False
                    result = gw.config.save_oc_bridge(payload)
                else:
                    raise ConfigError("未知 OC 桥动作: %s" % action)
            elif path == "/api/control/monitor":
                action = str(data.get("action") or "probe").strip().lower()
                if action != "probe":
                    raise ConfigError("未知监视动作: %s" % action)
                target = str(data.get("channel") or gw.config.default_channel)
                self._send_json(200, {"ok": True, **gw.probe_channel(target)})
                return
            elif path == "/api/locks/release":
                released = gw.router.release_lock(
                    str(data.get("task_id") or ""), str(data.get("group_id") or ""))
                result = {"released": released}
            else:
                self._error(404, "unknown control endpoint", "not_found")
                return
            gw.config.save()
            gw.after_control()
            self._send_json(200, {"ok": True, **result})
        except ConfigError as exc:
            self._error(400, str(exc), "config_error")
        except VaultError as exc:
            self._error(500, "credential vault error: %s" % exc, "vault_error")
        except OSError as exc:
            self._error(500, "config save failed: %s" % exc, "config_save_failed")

    def _probe_upstream(self, data: dict) -> dict:
        """只读探针：GET <base>/v1/models，不落盘、不建渠道。"""
        base = str(data.get("base_url") or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ConfigError("上游地址必须是 http(s) 绝对地址")
        name = str(data.get("name") or "").strip()
        token = str(data.get("token") or "").strip()
        if token in ("●●●●●●", "********"):
            token = ""
        entry = self.gateway.config.channel(name) if name else {}
        if not token:
            token = str(entry.get("token") or "")
        if not token:
            env_name = str(data.get("token_env") or entry.get("token_env") or "").strip()
            if env_name:
                token = str(os.environ.get(env_name) or "").strip()
        headers = {
            "Accept": "application/json",
            "User-Agent": "kwsa-lite/%s (+local)" % VERSION,
        }
        raw_headers = data.get("headers")
        if isinstance(raw_headers, dict):
            for hk, hv in raw_headers.items():
                hkey = str(hk or "").strip()
                hval = str(hv or "").strip()
                if not hkey or hkey.lower() in RESERVED_HEADERS:
                    continue
                if hval.startswith("env:"):
                    hval = str(os.environ.get(hval[4:].strip()) or "").strip()
                if hval:
                    headers[hkey] = hval
        if token:
            headers["Authorization"] = "Bearer " + token
        url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
        started = time.perf_counter()
        http_status = None
        error = ""
        models = []
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=8) as res:
                http_status = int(getattr(res, "status", 200))
                raw = res.read(2 * 1024 * 1024)
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                payload = {}
            rows = payload.get("data") if isinstance(payload, dict) else []
            seen = set()
            for item in (rows or []):
                model_id = str(item.get("id") if isinstance(item, dict) else "").strip()
                if model_id and model_id not in seen:
                    seen.add(model_id)
                    models.append(model_id)
            if http_status in (401, 403):
                status = "auth_error"
                error = "HTTP %s" % http_status
            elif http_status == 200 and models:
                status = "ok"
            elif http_status == 200:
                status = "no_models"
            else:
                status = "down"
                error = "HTTP %s" % http_status
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            status = "auth_error" if exc.code in (401, 403) else "down"
            error = "HTTP %s" % exc.code
        except Exception as exc:  # noqa: BLE001 探针失败不抛栈
            status = "down"
            error = str(exc)[:240]
        latency = (time.perf_counter() - started) * 1000
        return {
            "ok": status == "ok",
            "status": status,
            "models": models if status == "ok" else [],
            "url": url,
            "http_status": http_status,
            "latency_ms": round(latency, 1),
            "error": error,
        }

    def _read_control_body(self):
        length = int(self.headers.get("content-length") or 0)
        if length > 1024 * 1024:
            self._error(413, "control body too large", "payload_too_large")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._error(400, "control body must be JSON", "invalid_json")
            return None
        if not isinstance(data, dict):
            self._error(400, "control body must be a JSON object", "invalid_json")
            return None
        return data


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "off", "no", "")


def _mask_user(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "●" * len(text)
    return text[0] + "●" * min(6, len(text) - 1)


def _body_model(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model") or "").strip()


def _set_body_model(body: bytes, model: str) -> bytes:
    if not body or not model:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict) or payload.get("model") == model:
        return body
    payload["model"] = model
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def classify_upstream_error(status: int, text: str = "", network_error: str = "") -> dict:
    """把一次实测失败翻译成「原因码 + 一句人话 + 恢复路径」。

    只根据上游真实回包判定：拿不到报文就是 ``network_error``，绝不猜成限流或停用。
    面板「为什么不可用」直接用这份结果，避免解释与行为分家。
    """
    blob = str(text or "")
    lowered = blob.lower()
    upstream_code = ""
    message = ""
    try:
        data = json.loads(blob or "{}")
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                upstream_code = str(error.get("code") or error.get("type") or "").strip()
                message = str(error.get("message") or "").strip()
            elif isinstance(error, str):
                message = error.strip()
            elif data.get("message"):
                message = str(data["message"]).strip()
    except ValueError:
        pass
    evidence = " ".join(part for part in (
        ("HTTP %s" % status) if status else "", upstream_code, message) if part)[:240]
    probe = (upstream_code + " " + message).lower()
    if not status:
        code = "network_error"
        fix = "查出口分路 / DNS / 证书；修好后点「复检」"
    elif status == 401:
        code = "auth_error"
        fix = "更新该渠道密钥（密钥仓或环境变量），再点「复检」"
    elif status == 402 or "model_not_in_plan" in probe:
        code = "upstream_plan"
        fix = "升级或更换该渠道套餐；不在套餐内的模型只能等上游放开"
    elif "model_access_denied" in probe or "no permission" in probe or "not allowed" in probe:
        code = "upstream_denied"
        fix = "在这家平台给该 Key 放开模型权限，或整条渠道停用"
    elif status == 403 and ("1010" in probe or "<html" in lowered or "cloudflare" in lowered):
        code = "upstream_blocked"
        fix = "上游风控拦截：改走 OC 桥或换 UA，别用直连"
    elif "missingsessionid" in probe or "x-opencode-session" in probe or "missing session" in probe:
        code = "upstream_gate"
        fix = "把该渠道 gate 设为 oc-direct（或改走 OC 桥）；设了门禁仍 401 则再换有效 Key"
    elif "being rolled" in probe or "rolling out" in probe or "grey release" in probe:
        code = "upstream_rollout"
        fix = "上游灰测放量中，暂不可用；等放量后点「复检」"
    elif status == 404 or "model_not_found" in probe or "does not exist" in probe:
        code = "upstream_missing"
        fix = "从渠道模型清单移除；上游若恢复，点「复检」复核"
    elif status == 429:
        code = "rate_limited"
        fix = "降低并发或换渠道；限流是临时状态，可先启用观察"
    elif status == 403:
        code = "upstream_denied"
        fix = "该 Key 无此模型权限，去平台放开后「复检」"
    else:
        code = "upstream_error"
        fix = "看证据里的上游原文；持续失败就保持停用"
    if not message:
        message = network_error or ("HTTP %s" % status if status else "无法连接")
    return {
        "code": code,
        "label": unavailable_label(code),
        "reason": message[:240],
        "evidence": evidence or message[:240],
        "fix": fix,
    }


def _short_error(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or "")[:240]
        if isinstance(error, str):
            return error[:240]
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])[:240]
    except Exception:  # noqa: BLE001
        pass
    return payload.decode("utf-8", "replace")[:240].strip()


def create_server(config: Config, gateway: Gateway):
    try:
        httpd = SingleInstanceHTTPServer(
            (config.server["host"], config.server["port"]), Handler
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or exc.errno == 98:
            raise GatewayAlreadyRunningError(
                "端口 %s 已被占用" % config.server["port"]) from exc
        raise
    httpd.gateway = gateway
    httpd.daemon_threads = True
    return httpd


def audit_models(config_path=None, channel: str = "", limit: int = 0) -> int:
    """批量刷新「为什么不可用」：对每个停用模型真打 1-token 推理并回写证据。

    面板上的「复检」是单点 / 单组的手动动作；这里是换了密钥或怀疑上游恢复后的
    一键重查。只写注释与实测时间，不改启停——恢复与否仍由人决定。
    """
    path = Path(config_path or os.environ.get("KWSA_LITE_CONFIG") or DEFAULT_CONFIG)
    config = Config.load(path)
    vault_path = Path(os.environ.get("KWSA_LITE_VAULT") or DEFAULT_VAULT)
    try:
        config.attach_vault(SecretVault(vault_path))
    except VaultError as exc:
        raise ConfigError("渠道密钥仓不可用: %s" % exc) from exc
    gateway = Gateway(config)
    targets = []
    for name, entry in config.channels.items():
        if channel and name != channel:
            continue
        if not config.channel_enabled(name):
            continue
        for model in entry["models"]:
            if not config.model_enabled(name, model):
                targets.append((name, model))
    if limit > 0:
        targets = targets[:limit]
    print("[audit] %s v%s | 停用模型 %d 个待复检" % (SERVICE, VERSION, len(targets)), flush=True)
    ok = failed = skipped = 0
    for index, (name, model) in enumerate(targets, 1):
        try:
            result = gateway.probe_model(name, model)
        except ConfigError as exc:
            skipped += 1
            print("[audit] %d/%d %s/%s skip: %s" % (index, len(targets), name, model, exc),
                  flush=True)
            continue
        if result["ok"]:
            ok += 1
            gateway.config.annotate_model(name, model, "probe_ok", "复检通过，可启用",
                                          result["evidence"], "面板点「启用」即恢复")
        else:
            failed += 1
            gateway.config.annotate_model(name, model, result["code"], result["reason"],
                                          result["evidence"], result["fix"])
        print("[audit] %d/%d %s/%s -> %s | %s"
              % (index, len(targets), name, model,
                 "OK" if result["ok"] else result["code"], result["evidence"][:70]), flush=True)
        if index % 20 == 0:
            gateway.config.save()
    gateway.config.save()
    print("[audit] done: 可用 %d / 仍不可用 %d / 跳过 %d" % (ok, failed, skipped), flush=True)
    return 0


def run(config_path=None):
    path = Path(config_path or os.environ.get("KWSA_LITE_CONFIG") or DEFAULT_CONFIG)
    config = Config.load(path)
    vault_path = Path(os.environ.get("KWSA_LITE_VAULT") or DEFAULT_VAULT)
    try:
        config.attach_vault(SecretVault(vault_path))
    except VaultError as exc:
        raise ConfigError("渠道密钥仓不可用: %s" % exc) from exc
    auth = None
    generated = ""
    if config.auth["enabled"]:
        auth_path = Path(os.environ.get("KWSA_LITE_AUTH_FILE") or DEFAULT_AUTH)
        auth = PanelAuth(
            auth_path,
            username=config.auth["username"],
            ttl=config.auth["session_ttl_s"],
            cookie_secure=config.auth["cookie_secure"],
            max_failures=config.auth["login_max_failures"],
            window_s=config.auth["login_window_s"],
        )
        try:
            generated = auth.ensure_credentials(
                allow_generate=is_loopback_host(config.server["host"])
            )
        except AuthError as exc:
            raise ConfigError(str(exc)) from exc
    gateway = Gateway(config, auth=auth)
    httpd = create_server(config, gateway)
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    print("[KWSA lite] %s v%s" % (SERVICE, VERSION), flush=True)
    print("[KWSA lite] listening http://%s:%s (single instance)"
          % (config.server["host"], config.server["port"]), flush=True)
    print("[KWSA lite] default channel: %s | channels: %s | groups: %d"
          % (config.default_channel, ", ".join(config.channels), len(config.groups)), flush=True)
    print("[KWSA lite] ledger: %s | pricing: %s"
          % (config.ledger["path"], config.pricing.get("unit") or "—"), flush=True)
    print("[KWSA lite] data plane: %s | keys=%d | store=%s"
          % ("KEY REQUIRED" if config.data_plane["require_key"] else "optional",
             gateway.client_keys.active_count(), config.data_plane["keys_path"]), flush=True)
    if config.auth["enabled"]:
        print("[KWSA lite] panel auth: enabled | user=%s | sessions=%d"
              % (auth.username, auth.sessions_count()), flush=True)
        if generated:
            print("[KWSA lite] first-run panel password: %s" % generated, flush=True)
            print("[KWSA lite] change it before LAN use: "
                  "python 运维区/scripts/start_kwsa_lite.py --set-password", flush=True)
        if not is_loopback_host(config.server["host"]):
            print("[KWSA lite] WARNING: non-loopback bind on plain HTTP; "
                  "use a TLS reverse proxy for untrusted networks", flush=True)
    else:
        print("[KWSA lite] panel auth: disabled (loopback only)", flush=True)

    def _stop(signum=None, frame=None):  # noqa: ARG001
        print("\n[KWSA lite] signal %s, shutting down..." % signum, flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, AttributeError, OSError):
            pass
    gateway.start_probes()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        gateway.stop()
        httpd.server_close()
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
        print("[KWSA lite] stopped", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="KWSA lite gateway")
    parser.add_argument("--config", default=None, help="path to kwsa_lite.json")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--username", default=None, help="panel admin username")
    parser.add_argument("--set-password", action="store_true",
                        help="prompt for a new panel password and exit")
    parser.add_argument("--create-client-key", default=None, metavar="NAME",
                        help="issue a data-plane client key, print it once, and exit")
    parser.add_argument("--key-rps", type=float, default=None,
                        help="per-key rate limit for --create-client-key")
    parser.add_argument("--key-burst", type=float, default=None,
                        help="per-key burst for --create-client-key")
    parser.add_argument("--audit-models", action="store_true",
                        help="对每个停用模型真打一次探针，回写停用原因后退出")
    parser.add_argument("--audit-channel", default="",
                        help="--audit-models 只复检指定渠道")
    parser.add_argument("--audit-limit", type=int, default=0,
                        help="--audit-models 最多复检多少个（0 = 全部）")
    args = parser.parse_args(argv)
    if args.port:
        os.environ["KWSA_LITE_PORT"] = str(args.port)
    if args.host:
        os.environ["KWSA_LITE_HOST"] = args.host
    if args.username:
        os.environ["KWSA_LITE_AUTH_USER"] = args.username
    if args.set_password:
        try:
            config = Config.load(Path(args.config or os.environ.get("KWSA_LITE_CONFIG")
                                      or DEFAULT_CONFIG))
            auth_path = Path(os.environ.get("KWSA_LITE_AUTH_FILE") or DEFAULT_AUTH)
            auth = PanelAuth(auth_path, username=args.username or config.auth["username"])
            auth.load()
            first = getpass.getpass("新面板密码（至少 10 位）: ")
            second = getpass.getpass("再次输入: ")
            if first != second:
                print("[KWSA lite] 两次输入不一致，未修改", file=sys.stderr)
                return 2
            info = auth.set_password(first, username=args.username)
        except (ConfigError, AuthError) as exc:
            print("[KWSA lite] auth error: %s" % exc, file=sys.stderr)
            return 2
        print("[KWSA lite] panel password updated: user=%s path=%s"
              % (info["username"], info["path"]), flush=True)
        return 0
    if args.create_client_key:
        try:
            config = Config.load(Path(args.config or os.environ.get("KWSA_LITE_CONFIG")
                                      or DEFAULT_CONFIG))
            store = ClientKeyStore(
                config.data_plane["keys_path"],
                default_rps=config.data_plane["default_rps"],
                default_burst=config.data_plane["default_burst"],
            )
            secret, row = store.create(args.create_client_key,
                                       args.key_rps, args.key_burst)
        except (ConfigError, ClientKeyError, OSError) as exc:
            print("[KWSA lite] client key error: %s" % exc, file=sys.stderr)
            return 2
        print("[KWSA lite] client key issued: name=%s id=%s rps=%s burst=%s"
              % (row["name"], row["id"], row["rps"], row["burst"]), flush=True)
        print("[KWSA lite] client secret (shown once): %s" % secret, flush=True)
        return 0
    if args.audit_models:
        try:
            return audit_models(args.config, args.audit_channel, args.audit_limit)
        except ConfigError as exc:
            print("[KWSA lite] config error: %s" % exc, file=sys.stderr)
            return 2
    try:
        run(args.config)
    except ConfigError as exc:
        print("[KWSA lite] config error: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
    except GatewayAlreadyRunningError as exc:
        print("[KWSA lite] %s" % exc, file=sys.stderr)
        raise SystemExit(3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
