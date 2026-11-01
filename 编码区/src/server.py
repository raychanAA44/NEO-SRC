#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - HUB NEO 核心网关与反代服务 (端口 8888)
集成静态前端服务、/health 探针、W3C Trace 注入、POST 白名单硬闸与上游反代。
"""

import errno
import hmac
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

# 导入本包契约逻辑
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gateway import inject_trace_context, is_port_allowed
from core.catalog import ModelGroupCatalog, validate_group_definition
from core import egress
from core import accounts, hardening, adminauth
from core import context_fold, sse_heal
from core import quota as quota_engine
from core.official_ledger import OfficialLedger
from core.vendor_creds import VendorCreds
from core.vendor_session import ProfileUnavailable, read_cookies
from core.dispatch_ledger import DispatchLedger
from core.dispatcher import RequestDispatcher, is_group_id
from core import official_windows
from core.local_control import LocalControl
from core.local_ledger import LocalLedger
from core.local_readmodel import LocalReadModel, READ_ROUTES, channels_overview
from core.model_upstreams import ModelUpstreamRouter, join_url
from core.custom_upstreams import CustomUpstreamError, CustomUpstreamStore
from core import clock, price_feeds, price_sync
from core.local_keys import LocalKeyStore
from core.oc_bridge import OcBridgeStore
from core.rate_limiter import RateLimiter
from core.conn_pool import GLOBAL_CONN_POOL
from core import paths
from core.server_runtime import (
    GatewayAlreadyRunningError,
    SingleInstanceHTTPServer,
    create_server,
)

DEFAULT_PORT = 8888
VERSION = "1.6.0"
# 默认上游不再指向已下线的 hub-ng 8795：配置缺失时走云端，避免 /api/* 打到死端口。
DEFAULT_UPSTREAM = "https://gateway.example.invalid"
# 本地回退目标仍保留 8795；只有显式切 local 或配置 fallback_url 时才使用。
DEFAULT_UPSTREAM_LOCAL = "http://127.0.0.1:8795"
UI_DIR = paths.UI_DIR

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


TOKEN = (os.environ.get("HUB_ADMIN_TOKEN") or "").strip()
# 本地 hub-ng 通常使用独立 token；缺失时退回云端 token。
LOCAL_TOKEN = (os.environ.get("HUB_LOCAL_ADMIN_TOKEN") or "").strip()
# 云端/本地是两个固定角色：HUB_UPSTREAM 决定启动时的当前上游，
# HUB_UPSTREAM_CLOUD / HUB_UPSTREAM_LOCAL 保存一键切换的目标。
UPSTREAM_CLOUD = (os.environ.get("HUB_UPSTREAM_CLOUD")
                  or os.environ.get("HUB_UPSTREAM")
                  or DEFAULT_UPSTREAM).rstrip("/")
UPSTREAM_LOCAL = (os.environ.get("HUB_UPSTREAM_LOCAL")
                  or DEFAULT_UPSTREAM_LOCAL).rstrip("/")
UPSTREAM = (os.environ.get("HUB_UPSTREAM") or UPSTREAM_CLOUD).rstrip("/")

# 推理链路（/v1/*）与账本读接口走本地：模型上游是第三方渠道，账本是自己记的。
# 两者都与“云端/8795 反代”解耦；HUB_MODEL_UPSTREAM 未配置时退回通用上游。
MODEL_UPSTREAM = (os.environ.get("HUB_MODEL_UPSTREAM") or UPSTREAM).rstrip("/")
MODEL_TOKEN = (os.environ.get("HUB_MODEL_TOKEN") or "").strip()
# 账本模式：local（8888 自查自记）或 proxy（把 /api/* 原样转给上游，旧行为）。
LEDGER_MODE = (os.environ.get("HUB_LEDGER_MODE") or "local").strip().lower()
LEDGER_CHANNEL = (os.environ.get("HUB_MODEL_CHANNEL") or "open-design").strip()
LEDGER = LocalLedger()
RATE_LIMITER = RateLimiter()
# 本地控制面：渠道启停、模型开关、预算上限写自己的 control.json，不反代云端。
CONTROL_PATH = Path(
    os.environ.get("HUB_CONTROL_CONFIG")
    or (paths.CONFIG_DIR / "control.json")
).resolve()
CONTROL = LocalControl(CONTROL_PATH, default_channel=LEDGER_CHANNEL)
# 模型上游路由：未列出的模型走默认上游；clef:27b 显式改道 Ollama
# /v1/systemone。token 只从进程环境变量读取，配置文件不落明文。
MODEL_UPSTREAMS_PATH = Path(
    os.environ.get("HUB_MODEL_UPSTREAMS")
    or (paths.CONFIG_DIR / "model_upstreams.json")
).resolve()
# 第三方渠道在线接入：元数据写 model_upstreams.json，密钥封存在 data 仓并注入环境。
# 必须在 MODEL_ROUTER 构造前 load_into_environ，路由表才能读到 token_env。
CUSTOM_UPSTREAMS_PATH = Path(
    os.environ.get("HUB_CUSTOM_UPSTREAMS")
    or (paths.DATA_DIR / "custom_upstreams.json")
).resolve()
CUSTOM_UPSTREAMS = CustomUpstreamStore(
    MODEL_UPSTREAMS_PATH, keys_path=CUSTOM_UPSTREAMS_PATH)
CUSTOM_UPSTREAMS_LOADED = CUSTOM_UPSTREAMS.load_into_environ()
MODEL_ROUTER = ModelUpstreamRouter(
    MODEL_UPSTREAMS_PATH,
    default_upstream=MODEL_UPSTREAM,
    default_channel=LEDGER_CHANNEL,
    default_token=MODEL_TOKEN,
)
MODEL_UPSTREAMS_WRITE_LOCK = threading.Lock()
# 本地模式 KEY 单表：签发/启停/吊销一律落 data 仓，明文只在签发响应出现一次。
LOCAL_KEYS = LocalKeyStore(
    os.environ.get("HUB_LOCAL_KEYS")
    or (paths.DATA_DIR / "local_keys.json"))
# OC 桥令牌：在线查看/更新，密文落 data 仓；空 = 未配置。
OC_BRIDGE = OcBridgeStore(
    os.environ.get("HUB_OC_BRIDGE")
    or (paths.DATA_DIR / "oc_bridge.json"))
# 已存令牌注入进程环境，重启后消费者不用再读密文仓。
_OC_BRIDGE_TOKEN = OC_BRIDGE.get_token()
if _OC_BRIDGE_TOKEN:
    os.environ["HUB_OC_BRIDGE_TOKEN"] = _OC_BRIDGE_TOKEN
# 第三方凭据保险仓（登录即同步用）：2026-10-25 NEO-F3。
VENDOR_CREDS = VendorCreds()
# 官方渠道 → 浏览器 cookie 域名（只读探针用；拿不到就如实报 profile_unavailable）。
# 语义：**域名子串**（大小写不敏感，见 vendor_session._read_firefox_cookies）。
# 渠道名本身就是域名前缀的可省（`ollama`→ollama.com、`opencode`→opencode.ai、
# `tokenharbor`→tokenharbor.ai），省略时由 `vendor_login` 的 `or channel` 兜底。
# 只有「渠道名 ≠ 域名」的才必须列：workbuddy-cn→codebuddy、trae→trae.ai。
VENDOR_COOKIE_HOSTS = {
    "workbuddy": "workbuddy", "workbuddy-intl": "workbuddy",
    "workbuddy-cn": "codebuddy",
    "trae": "trae.ai", "tokenharbor": "tokenharbor",
    "open-design": "open-design", "opendesign": "open-design",
}
# 官方额度/对账结果缓存 60s：面板刷新不打爆官方端点。
QUOTA_TTL = 60.0
_QUOTA_CACHE = {}
_QUOTA_LOCK = threading.Lock()
# 登录态取价：会话缓存（30 分钟）+ 串行锁，避免每次刷新都重新登录。
_PRICING_SESSION_TTL = 1800.0
_PRICING_SESSION_CACHE = {}
_PRICING_SESSION_LOCK = threading.Lock()
_OFFICIAL_LEDGER = {"ledger": None, "obj": None}
# 本地读模型：云端 kswa 的读接口全部由账本 + 渠道配置就地派生，不再反代。
LOCAL_READ_ROUTES = READ_ROUTES if LEDGER_MODE == "local" else frozenset()
# 本地模式下一律由 8888 自己作答的写端点：不再访问云端 kswa。
# 典型两类：落本地配置（开关/预算/出口/错误确认）与本地确实无数据的空回执（账号池/官方额度）。
LOCAL_WRITE_ROUTES = frozenset({
    "/api/models/toggle", "/api/models/toggle_bulk", "/api/models/bench",
    "/api/visibility", "/api/budgets",
    "/api/accounts/checkin", "/api/accounts/inspect", "/api/accounts/import_text",
    "/api/accounts/delete",
    "/api/errors/fix", "/api/egress/proxy-url",
    "/api/official/quota-snapshot", "/api/official/vendor-login",
    "/api/channels/toggle", "/api/routes/policy",
    "/api/system/reload", "/api/system/cache/clear", "/api/system/logs/clear",
    "/api/egress/channel-override", "/api/prompt-opt/mode", "/api/rate-limit/config",
}) if LEDGER_MODE == "local" else frozenset()
# 这些读接口由网关自己作答（数据来源于本地账本），其余 /api/* 仍走反代。
LEDGER_ROUTES = frozenset({
    "/api/usage", "/api/status", "/api/readmodel/dashboard",
    "/api/routes", "/api/readmodel/catalog",
})
# 预算硬闸门：今日支出 ≥ 上限就不再打上游。
# 支出是事后记账，5s 缓存避免每个 /v1 请求都去数一遍库；代价是最多滞后 5 秒。
BUDGET_CACHE_TTL = 5.0
_BUDGET_STATE = {"at": 0.0, "day": "", "total": 0.0, "channels": {}}
_BUDGET_LOCK = threading.Lock()


def today_spend(now=None):
    """今日账本实际支出 (总额, 按渠道)；5s 内复用上一次结果。"""
    now = time.time() if now is None else float(now)
    # 预算日界与账本「今日」同源（CST 00:00），否则硬闸门会在错误的时刻触发。
    day = clock.date(now)
    with _BUDGET_LOCK:
        if _BUDGET_STATE["day"] == day and now - _BUDGET_STATE["at"] < BUDGET_CACHE_TTL:
            return _BUDGET_STATE["total"], dict(_BUDGET_STATE["channels"])
    since = clock.day_start(now)
    try:
        total, channels = LEDGER.spend_since(since)
    except Exception:                      # 账本不可读时不因预算把请求全挡下
        total, channels = _BUDGET_STATE["total"], dict(_BUDGET_STATE["channels"])
    with _BUDGET_LOCK:
        _BUDGET_STATE.update({"at": now, "day": day, "total": total,
                              "channels": channels})
    return total, dict(channels)


def reset_budget_cache():
    """丢掉今日支出缓存（测试与配置重置用；正常运行靠 5s TTL 自然过期）。"""
    with _BUDGET_LOCK:
        _BUDGET_STATE.update({"at": 0.0, "day": "", "total": 0.0, "channels": {}})


def budget_block(channel=""):
    """预算闸门判定：未设上限的档不参与，到上限才拦。"""
    channel = (channel or LEDGER_CHANNEL).strip()
    total, channels = today_spend()
    return CONTROL.budget_gate(channel, total, channels.get(channel, 0.0))


def budget_health(channel=""):
    """`/health` 用的闸门现状：已设上限、今日支出、当前是否已在拦。"""
    channel = (channel or LEDGER_CHANNEL).strip()
    gate = budget_block(channel)
    caps = CONTROL.budget_caps(channel)
    total, _ = today_spend()
    return {"caps": [{"key": key, "amount": amount, "period": period}
                     for key, amount, period in caps],
            "cap_count": len(caps), "enforced": True,
            "spent_today_cny": total, "blocked": bool(gate.get("blocked")),
            "blocked_by": gate.get("key") if gate.get("blocked") else None,
            "used_pct": gate.get("used_pct"), "warn": bool(gate.get("warn")),
            "warn_pct": gate.get("warn_pct"), "period": "day"}
def _official_ledger():
    """对账门面：LEDGER 被测试换掉时跟着重建，不沿用旧库缓存。"""
    if _OFFICIAL_LEDGER["ledger"] is not LEDGER:
        _OFFICIAL_LEDGER.update({"ledger": LEDGER, "obj": OfficialLedger(LEDGER)})
    return _OFFICIAL_LEDGER["obj"]


def _router_channels() -> list:
    try:
        return [row.get("channel") for row in MODEL_ROUTER.summary().get("channels") or []
                if row.get("channel")]
    except Exception:                       # noqa: BLE001 路由表坏了不让读模型 500
        return []


def _quota_doc(channel: str) -> dict:
    """文档口径兜底：只认运维手写的 `quota_doc.json`，没有就空（不编数字）。"""
    path = Path(os.environ.get("HUB_QUOTA_DOC")
                or (paths.CONFIG_DIR / "quota_doc.json"))
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    channels = doc.get("channels") if isinstance(doc, dict) else None
    row = (channels or {}).get(channel)
    return row if isinstance(row, dict) else {}


def _window_creds(channel: str, creds: dict) -> dict:
    """给分窗取数准备认证：open-design 要会话 cookie，ollama 要 API key。

    会话优先用凭据仓里那份；ollama 的 key 在自定义上游（加密）里，就地解封。
    """
    out = dict(creds or {})
    if channel == "open-design" and not out.get("session"):
        try:
            out["session"] = price_sync.session_from_creds(out)
        except Exception:                       # noqa: BLE001
            pass
    if channel == "ollama" and not out.get("api_key"):
        try:
            token, _headers = CUSTOM_UPSTREAMS.probe_auth(channel)
            out["api_key"] = str(token or "")
        except Exception:                       # noqa: BLE001
            pass
    return out


def _fetch_quota(channel: str) -> dict:
    """单渠道额度：凭据仓 token + 路由表真实头 → live → manual → doc → none。"""
    entry = {}
    try:
        entry = MODEL_ROUTER.channel(channel) or {}
    except Exception:                       # noqa: BLE001
        entry = {}
    creds = {}
    try:
        creds = VENDOR_CREDS.get(channel) or {}
    except Exception:                       # noqa: BLE001 密钥不可用不当成登录失败之外的事情
        creds = {}
    token = str(creds.get("token") or creds.get("access_token")
                or creds.get("api_key") or entry.get("token") or "")
    # 只有“登录即同步”存下来的凭据才去探官方：路由表里的推理 token 不代表能查额度，
    # 拿它去探只会是 401，还白白拖慢面板首屏。
    has_auth = bool(creds.get("token") or creds.get("access_token") or creds.get("api_key")
                    or creds.get("cookie") or creds.get("cookies")
                    or creds.get("session") or creds.get("password"))
    quota = quota_engine.fetch_channel_quota(
        channel, base_url=str(entry.get("base_url") or "") if has_auth else "",
        token=token, headers=dict(entry.get("headers") or {}), timeout=4.0,
        manual=quota_engine.load_snapshots().get(channel) or {},
        doc=_quota_doc(channel))
    # 分窗用量（今日/5 小时/周/月）：官方端点另有一套，不阻塞面板（先回旧值再后台刷）。
    try:
        wcreds = _window_creds(channel, creds)
        if channel == "ollama" and not wcreds.get("api_key"):
            # ollama 的官方用量只认 API key（Bearer）；路由表里那把就是同源的。
            wcreds["api_key"] = token
        wins = official_windows.collect(channel, creds=wcreds)
    except Exception:                           # noqa: BLE001 分窗挂了不影响总额度
        wins = {"ok": False, "windows": {}, "missing": [{"window": "*", "why": "分窗取数异常"}]}
    quota["windows"] = wins.get("windows") or {}
    quota["windows_ok"] = bool(wins.get("ok"))
    quota["windows_pending"] = bool(wins.get("pending"))
    quota["windows_missing"] = wins.get("missing") or []
    quota["windows_tried"] = wins.get("tried") or []
    quota["windows_extra"] = wins.get("extra") or {}
    quota["windows_at"] = wins.get("checked_at")
    return quota


def channel_quota_map(channels=None, refresh=None) -> dict:
    """多渠道额度（60s 缓存）；`refresh` 里的渠道强制现取。"""
    names = list(channels or _router_channels())
    force = {str(n) for n in (refresh or ()) if n}
    now, out = time.time(), {}
    for name in names:
        with _QUOTA_LOCK:
            hit = _QUOTA_CACHE.get(name)
        if hit and name not in force and now - hit[0] < QUOTA_TTL:
            out[name] = hit[1]
            continue
        out[name] = _fetch_quota(name)
        row = out[name]
        # 分窗用量是后台线程取的：pending 的行不能按 60s 缓存，否则面板会把
        # 「首次取数进行中」钉住一整分钟，而官方数字其实 1 秒就到了。
        stamp = now - QUOTA_TTL + 3.0 if row.get("windows_pending") else now
        with _QUOTA_LOCK:
            _QUOTA_CACHE[name] = (stamp, row)
    return out


def recorded_channels() -> set:
    """已收录进本地配置的渠道代号：control.json 的 channels + 自定义上游。

    收录过一次就留档：下次开机它仍算「已收录」，界面不需要重新加入即可再拉额度。
    """
    names = set()
    try:
        names |= {str(k) for k in (CONTROL.snapshot().get("channels") or {})}
    except Exception:                       # noqa: BLE001 配置读不到不等于没收录
        pass
    try:
        for row in CUSTOM_UPSTREAMS.list_channels() or []:
            if isinstance(row, dict) and row.get("name"):
                names.add(str(row["name"]))
    except Exception:                       # noqa: BLE001
        pass
    return names


def _mark_recorded(rows) -> list:
    """给渠道行打 recorded 标记：未收录的渠道不进官方额度版面。"""
    known = recorded_channels()
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            row = dict(row)
            row["recorded"] = str(row.get("channel") or "") in known
        out.append(row)
    return out


WINDOW_ORDER = (("today", "今日"), ("h5", "近 5 小时"),
                ("week", "本周"), ("month", "本月"))


def _decorate_official(rows) -> list:
    """给每个渠道补上 recorded 与分窗用量（今日 / 近 5 小时 / 本周 / 本月）。

    官方报了几窗就用官方的剩余/上限，没报的窗口把本地实测用量放进去并标
    source=local；两边都不造数字。
    """
    known = recorded_channels()
    cache: dict = {}
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        name = str(row.get("channel") or "")
        row = dict(row)
        row["recorded"] = name in known
        local_windows = cache.get(name)
        if local_windows is None:
            try:
                local_windows = LEDGER.window_stats(name)
            except Exception:               # noqa: BLE001 账本读不到不编数
                local_windows = {}
            cache[name] = local_windows
        quota_row = row.get("official") or {}
        official_windows = (quota_row.get("windows") or {})
        row["official_missing"] = quota_row.get("windows_missing") or []
        row["official_pending"] = bool(quota_row.get("windows_pending"))
        row["official_windows_at"] = quota_row.get("windows_at")
        win = {}
        for key, label in WINDOW_ORDER:
            off = official_windows.get(key) or None
            loc = local_windows.get(key) or None
            win[key] = {
                "label": label,
                "source": "official" if off else "local",
                "official": off,
                "local": loc,
                "used": (off or {}).get("used"),
                "limit": (off or {}).get("limit"),
                "remaining": (off or {}).get("remaining"),
                "used_pct": (off or {}).get("used_pct"),
                "unit": (off or {}).get("unit") or "",
                "reset_at": (off or {}).get("reset_at") or "",
            }
        row["windows"] = win
        out.append(row)
    return out


def official_summary_payload() -> dict:
    """官方额度 vs 本地账本的差额汇总（登录即同步与 06 面板共用）。"""
    quotas = channel_quota_map()
    payload = _official_ledger().summary(quotas)
    payload["channels"] = _decorate_official(payload.get("channels"))
    return {"ok": True, **payload}


def today_report_payload() -> dict:
    """纪事信笺：账本全量聚合 + 官方对账 + 预算水位，一次请求给全。

    前端只负责排版，不再拉 50 条样本自己拼——样本截断会撒谎。
    """
    try:
        report = LEDGER.daily_summary()
    except Exception as exc:                # noqa: BLE001 账本读失败不伪造数字
        return {"ok": False, "error": {"message": "账本聚合失败: %s" % exc},
                "generated_at": int(time.time())}
    channels = [row for row in report.get("channels") or []
                if row.get("channel") not in ("", "—")]
    quotas = {}
    if channels:
        try:
            quotas = channel_quota_map([row["channel"] for row in channels])
        except Exception:                   # noqa: BLE001 官方不可达不拖垮整封信
            quotas = {}
    official = _official_ledger()
    rows = []
    for row in channels:
        try:
            entry = official.entry(row["channel"], quotas.get(row["channel"]) or {})
        except Exception:                   # noqa: BLE001
            entry = {}
        recon = entry.get("reconcile") or {}
        rows.append({
            "channel": row["channel"],
            "calls": row["calls"],
            "tokens": row["total"],
            "cost_cny": row["cost_cny"],
            "ok": row["ok"],
            "errors": row["errors"],
            "official_used": recon.get("official_used"),
            "official_unit": recon.get("official_unit") or "",
            "official_source": recon.get("official_source") or "none",
            "delta_requests": recon.get("delta_requests"),
            "verdict": recon.get("verdict") or "no_official",
            "comparable": bool(recon.get("comparable")),
            "note": recon.get("note") or "",
        })
    rows.sort(key=lambda item: (-item["calls"], item["channel"]))
    sources = [item["official_source"] for item in rows]
    report["reconciliation"] = {
        "rows": rows,
        "total": len(rows),
        "official_ahead": [item["channel"] for item in rows
                           if item["verdict"] == "official_ahead"],
        "source_mix": {source: sources.count(source) for source in sorted(set(sources))},
        "generated_at": int(time.time()),
        "note": "差值为 null 表示官方与本地口径不同，不硬算",
    }
    try:
        report["budgets"] = budget_health(LEDGER_CHANNEL)
    except Exception:                       # noqa: BLE001
        report["budgets"] = {}
    report.update({
        "dispatch_no": report.get("date"),
        "port": PORT,
        "service": "hub-neo-gateway",
        "upstream_role": upstream_role(),
        "ledger_channel": LEDGER_CHANNEL,
    })
    return report


# ── 出站探针：直连腿 / 代理腿共用同一个回显 IP 端点 ─────────────
EGRESS_PROBE_URL = (os.environ.get("HUB_EGRESS_PROBE_URL")
                    or "https://api.ipify.org?format=json")


def _egress_probe_once(proxy_url: str = "", timeout: float = 6.0) -> dict:
    """打一发极小的回显请求，返回真实出口 IP 与耗时；失败如实记 error。"""
    started = time.perf_counter()
    req = urllib.request.Request(EGRESS_PROBE_URL, headers={
        "accept": "application/json",
        "user-agent": "hub-neo-egress-probe/1.0",
    })
    try:
        with egress.open_url(req, timeout=timeout, proxy_url=proxy_url) as res:
            raw = res.read(2048).decode("utf-8", "replace")
            status = int(getattr(res, "status", 0) or 0)
        ip = ""
        try:
            ip = str((json.loads(raw) or {}).get("ip") or "")
        except (ValueError, TypeError):
            ip = raw.strip()[:64]
        return {"ok": 200 <= status < 400,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "ip": ip, "http_status": status, "proxy_url": proxy_url, "error": ""}
    except Exception as exc:                # noqa: BLE001 探针失败就是失败，不伪造 OK
        return {"ok": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "ip": "", "http_status": None, "proxy_url": proxy_url,
                "error": str(exc)[:200]}


def egress_probe_payload(data: dict) -> dict:
    """双腿并行探活：直连腿恒走直连；代理腿走渠道覆盖或传入 URL。"""
    channel = str(data.get("channel") or "").strip()
    url = str(data.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("url 必须是 http:// 或 https://")
    proxy_url = url or CONTROL.egress_proxy(channel)
    results = {}

    def run(key, target):
        results[key] = _egress_probe_once(target)

    threads = [threading.Thread(target=run, args=("direct", "")),
               threading.Thread(target=run, args=("proxy", proxy_url))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(9)
    direct = results.get("direct") or {
        "ok": False, "error": "timeout", "ip": "", "elapsed_ms": None, "proxy_url": ""}
    proxy = results.get("proxy") or {
        "ok": False, "error": "timeout", "ip": "", "elapsed_ms": None,
        "proxy_url": proxy_url}
    return {"ok": bool(direct.get("ok") or proxy.get("ok")),
            "channel": channel, "direct": direct, "proxy": proxy,
            "proxy_url": proxy_url, "proxy_configured": bool(proxy_url),
            "effective": "proxy" if proxy_url else "direct",
            "probe_url": EGRESS_PROBE_URL, "at": int(time.time())}


def channels_overview_payload() -> dict:
    """06 单入口读模型：官方源额度与本地 8888 预算两条轴并列。"""
    quotas = channel_quota_map()
    ledger = _official_ledger()
    reconcile = {name: ledger.entry(name, quota)["reconcile"]
                 for name, quota in quotas.items()}
    payload = channels_overview(ledger=LEDGER, control=CONTROL, router=MODEL_ROUTER,
                                accounts_mod=accounts, creds=VENDOR_CREDS,
                                quota_map=quotas, reconcile=reconcile)
    payload["channels"] = _mark_recorded(payload.get("channels"))
    return payload


LOGS_DIR = paths.DATA_DIR / "test_runs"


def system_cache_clear() -> dict:
    """清掉进程内读缓存（模型名单 / 额度 / 对账 / 预算）：下一次请求重新取真数。"""
    with _MODEL_LIST_LOCK:
        had_models = _MODEL_LIST_CACHE.get("models") is not None
        _MODEL_LIST_CACHE.update({"at": 0.0, "models": None})
    with _MODEL_NAMES_LOCK:
        had_names = _MODEL_NAMES_CACHE.get("names") is not None
        _MODEL_NAMES_CACHE.update({"at": 0.0, "names": None})
    with _QUOTA_LOCK:
        quota_n = len(_QUOTA_CACHE)
        _QUOTA_CACHE.clear()
    _official_ledger().invalidate()
    with _BUDGET_LOCK:
        _BUDGET_STATE.update({"at": 0.0, "day": "", "total": 0.0, "channels": {}})
    return {"ok": True, "cleared": {"model_list": had_models, "model_names": had_names,
                                     "quota_channels": quota_n, "official_ledger": True,
                                     "budget": True},
            "at": int(time.time())}


def system_reload() -> dict:
    """配置热重载：路由表 / 分组 / 限流参数就地生效，不断流、不重启进程。"""
    out = {"ok": True, "at": int(time.time())}
    try:
        summary = MODEL_ROUTER.reload() or {}
        out["channels"] = len(summary.get("channels") or [])
    except Exception as exc:                    # noqa: BLE001 单块失败不拦其它块
        out["channels_error"] = str(exc)[:120]
    try:
        out["groups"] = reload_model_groups(force=True).get("count")
    except Exception as exc:                    # noqa: BLE001
        out["groups_error"] = str(exc)[:120]
    try:
        policy = (CONTROL.snapshot().get("routing") or {})
        info = RATE_LIMITER.set_rate(int(policy.get("rate_limit_rps") or RATE_LIMITER.rate))
        out["rate_limit"] = {"rps": info["rate"], "burst": info["burst"]}
    except Exception as exc:                    # noqa: BLE001
        out["rate_limit_error"] = str(exc)[:120]
    out["cache"] = system_cache_clear()["cleared"]
    return out


def system_logs_clear(keep: int = 5, log_dir=None) -> dict:
    """清历史运行日志：只删 `run-*.log`，保留最近 keep 份，正在写的日志不动。"""
    folder = Path(log_dir or LOGS_DIR)
    keep = max(0, int(keep))
    try:
        files = sorted(folder.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        return {"ok": False, "error": "日志目录不可读: %s" % exc, "dir": str(folder)}
    stale = files[keep:]
    freed, removed, failed = 0, 0, []
    for path in stale:
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
            removed += 1
        except OSError as exc:
            failed.append("%s: %s" % (path.name, exc))
    return {"ok": not failed, "removed": removed, "freed_bytes": freed,
            "kept": min(len(files), keep), "dir": str(folder),
            "failed": failed[:5], "at": int(time.time())}


def vendor_login(payload: dict) -> dict:
    """登录即同步：验凭据 → 加密落盘 → 立即抓额度 → 对比本地账本。"""
    channel = str((payload or {}).get("channel") or "").strip()
    if not channel:
        raise ValueError("channel 必填")
    creds = {key: str(payload.get(key)) for key in
             ("username", "password", "token", "access_token", "api_key",
              "cookie", "cookies", "session", "realm")
             if payload.get(key) not in (None, "")}
    if payload.get("from_browser"):
        host = VENDOR_COOKIE_HOSTS.get(channel) or channel
        try:
            cookies = read_cookies(host)
        except ProfileUnavailable as exc:
            return {"ok": False, "channel": channel, "code": "profile_unavailable",
                    "error": str(exc)}
        creds["cookie"] = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
    if not creds:
        raise ValueError("至少提供 token / 用户名+密码 / 浏览器会话之一")
    saved = VENDOR_CREDS.save(channel, creds)
    # 登录即同步的第二步：有账号密码就用它现换会话 cookie（价目表走登录态抓 unitPrices）。
    pricing_session = {"ok": False, "code": "no_credentials", "error": "只存了密钥，没有账号密码"}
    email = str(creds.get("username") or creds.get("email") or "").strip()
    password = str(creds.get("password") or "")
    if email and password:
        base_url = ""
        try:
            base_url = str((CUSTOM_UPSTREAMS._read_config()["channels"].get(channel) or {})
                           .get("base_url") or "")
        except (OSError, ValueError):
            base_url = ""
        got = price_sync.login_session(base_url or price_sync.OD_API, email, password)
        pricing_session = {"ok": bool(got.get("ok")), "code": got.get("code") or "",
                           "error": got.get("error") or "",
                           "endpoint": got.get("endpoint") or ""}
        if got.get("ok"):
            creds["session"] = got["session"]
            _PRICING_SESSION_CACHE[channel] = {"session": got["session"], "at": time.time()}
            saved = VENDOR_CREDS.save(channel, creds)
    # 登录即同步的第三步：拿会话顺手抽一遍官方账单 unitPrices（只读；要落盘得显式 sync_prices）。
    prices = {"ok": False, "code": "no_session", "count": 0, "error": "", "applied": None}
    session = price_sync.session_from_creds(creds)
    if session:
        found = price_sync.fetch_account_prices(session, timeout=20)
        prices = {"ok": bool(found["ok"]), "code": found.get("code") or "",
                  "count": len(found["rows"]), "source_url": found["source_url"],
                  "error": found["error"], "applied": None}
        if found["ok"] and payload.get("sync_prices"):
            prices["applied"] = price_sync.apply_prices(
                LEDGER.pricing_path, channel, found["rows"], found["source_url"],
                source_label="登录态官方账单 unitPrices %s" % found["source_url"])
    quota = channel_quota_map([channel], refresh={channel})[channel]
    # 登录这一次就是用户在看：同步取一遍分窗用量（带 10s 上限），别让抽屉显示「取数中」。
    try:
        official_windows.invalidate(channel)
        wins = official_windows.collect(channel, blocking=True, timeout=10.0,
                                        creds=_window_creds(channel, creds))
        quota.update({"windows": wins.get("windows") or {},
                      "windows_ok": bool(wins.get("ok")),
                      "windows_pending": bool(wins.get("pending")),
                      "windows_missing": wins.get("missing") or [],
                      "windows_tried": wins.get("tried") or [],
                      "windows_extra": wins.get("extra") or {},
                      "windows_at": wins.get("checked_at")})
    except Exception:                           # noqa: BLE001 分窗失败不推翻登录结果
        pass
    if quota.get("source") == quota_engine.SOURCE_LIVE:
        # 登录当场抓到的 live 额度同时落成 manual 快照：官方下次不可达也有底。
        try:
            quota_engine.save_snapshot(channel, quota)
        except (OSError, ValueError):
            pass
    return {"ok": True, "channel": channel, "creds": saved,
            "login": pricing_session, "prices": prices,
            "quota": quota, "quota_label": quota_engine.source_label(quota.get("source")),
            "reconcile": _official_ledger().entry(channel, quota)["reconcile"],
            "at": int(time.time())}


# P1-3(审计): 模型列表缓存统一加锁,与 _MODEL_NAMES_LOCK 同纪律
_MODEL_LIST_LOCK = threading.Lock()
_MODEL_LIST_CACHE = {"at": 0.0, "models": None}


def model_token() -> str:
    """推理上游的 Bearer：优先模型专用 token，缺失才退回通用 token。"""
    return MODEL_TOKEN or upstream_token()
PORT = int(os.environ.get("HUB_NEO_PORT", str(DEFAULT_PORT)))
UPSTREAM_PROBE_INTERVAL = max(5, _env_int("HUB_UPSTREAM_PROBE_INTERVAL", 30))
UPSTREAM_PROBE_TIMEOUT = max(1, _env_int("HUB_UPSTREAM_PROBE_TIMEOUT", 4))
# R-1/NEO-GW-05: 探针路径可配置(第三方上游可能没有 /health)
PROBE_PATH = (os.environ.get("HUB_UPSTREAM_PROBE_PATH") or "/health").strip() or "/health"
# P0-1(审计 2026-10-24): 请求体上限,超限 413,防大包内存耗尽
MAX_REQUEST_BYTES = max(1024, _env_int("HUB_MAX_REQUEST_BYTES", 10 * 1024 * 1024))
MODEL_GROUPS_PATH = Path(
    os.environ.get("HUB_MODEL_GROUPS")
    or (paths.CONFIG_DIR / "model_groups.json")
).resolve()

# 分流裁决器与任务粘性锁:进程级单例,跨请求保住 Prefix Cache(NEO-GW-02 接线)
DISPATCHER = RequestDispatcher()
from core.prompt_opt import PromptOptimizer

def _default_refiner_endpoint() -> str:
    """R-2:Refiner 缺省走模型推理上游(与 /v1 推理同源同凭据)。"""
    base = MODEL_UPSTREAM.rstrip("/")
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


# Prompt self-heal singleton: off|shadow|enforce via HUB_PROMPT_OPT env;
# refiner endpoint/model/token default to the model upstream (NEO-GW-06).
PROMPT_OPTIMIZER = PromptOptimizer(
    mode=os.environ.get("HUB_PROMPT_OPT", "off"),
    refiner_endpoint=(os.environ.get("HUB_REFINER_ENDPOINT")
                      or _default_refiner_endpoint()),
    refiner_model=os.environ.get("HUB_REFINER_MODEL", "auto/fast"),
    refiner_token=(os.environ.get("HUB_REFINER_TOKEN") or MODEL_TOKEN))
# 面板改过的模式优先于环境变量：重启后仍按 control.json 生效。
_PERSISTED_PROMPT_MODE = CONTROL.prompt_opt_mode()
if _PERSISTED_PROMPT_MODE:
    PROMPT_OPTIMIZER.set_mode(_PERSISTED_PROMPT_MODE)

# 流式自愈与工具守卫：默认 shadow（线上字节零变化，先看影子计数）；
# 显式 HUB_SSE_HEAL=enforce / HUB_TOOL_GUARD=enforce 才真修/真拦。
SSE_HEAL_MODE = sse_heal.env_mode("HUB_SSE_HEAL", sse_heal.DEFAULT_HEAL_MODE)
TOOL_GUARD_MODE = sse_heal.env_mode("HUB_TOOL_GUARD", sse_heal.DEFAULT_TOOL_MODE)
# 上下文 L1 折叠：默认 enforce（用户定盘"立省冗余 token"），off/shadow 可回退。
CONTEXT_FOLD_MODE = context_fold.MODE

# 派发台账:只记 auto/* 的真实裁决与上游回执,供 02 视图的洋葱轨迹读取。
# 默认落盘到运维区/data,重启后旧 usage 行仍能凭 rid 找回轨迹;HUB_DISPATCH_PERSIST=0 可关。
DISPATCH_DB = Path(
    os.environ.get("HUB_DISPATCH_DB")
    or (paths.DATA_DIR / "dispatch_traces.db")
).resolve()
DISPATCH_PERSIST = (os.environ.get("HUB_DISPATCH_PERSIST", "1").strip().lower()
                    not in ("0", "false", "off", "no"))
DISPATCH_LEDGER = DispatchLedger(
    persist_path=DISPATCH_DB if DISPATCH_PERSIST else None)

POST_WHITELIST = (
    "/api/budgets",
    "/api/models/toggle",
    "/api/models/toggle_bulk",
    "/api/models/bench",
    "/api/models/groups/reload",
    "/api/upstream/switch",
    "/api/groups/save",
    "/api/task_locks/release",
    "/api/accounts/checkin",
    "/api/accounts/inspect",
    "/api/errors/fix",
    "/api/visibility",
    "/api/egress/proxy-url",
    "/api/official/quota-snapshot",
)
KEY_REALM_RE = re.compile(r"^/api/keys/[^/]+/realm$")
KEY_TOGGLE_RE = re.compile(r"^/api/keys/[^/]+/toggle$")
SENSITIVE_GET_RE = re.compile(r"^/api/keys/[^/]+/reveal$")
SENSITIVE_GET_PREFIXES = ("/api/vault", "/api/admin/", "/api/actuator/")


def is_write_allowed(path: str) -> bool:
    """写闸门只放行契约内的端点:POST_WHITELIST 全部固定写端点 + 单把 KEY 的 realm 变更;计数口径唯一正本 = len(POST_WHITELIST)。"""
    return path in POST_WHITELIST or bool(KEY_REALM_RE.match(path))


def is_read_blocked(path: str) -> bool:
    """浏览器侧不接收 KEY 明文、密码本与管理端原始代理，二次授权在宿主完成。"""
    return bool(SENSITIVE_GET_RE.match(path)) or path.startswith(SENSITIVE_GET_PREFIXES)


# ── 模型分组热加载目录 ─────────────────────────────────────────
MODEL_GROUPS_LOCK = threading.Lock()
MODEL_GROUPS_WRITE_LOCK = threading.Lock()
_model_catalog = ModelGroupCatalog()
_model_catalog_stamp = None
_model_catalog_loaded = False
_model_catalog_error = ""


def _model_groups_stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _model_groups_snapshot_locked() -> dict:
    groups = _model_catalog.list_groups()
    return {
        "version": _model_catalog.version,
        "count": len(groups),
        "groups": groups,
    }


def model_groups_snapshot() -> dict:
    """返回当前内存目录快照（深拷贝，调用方无法污染缓存）。"""
    with MODEL_GROUPS_LOCK:
        return _model_groups_snapshot_locked()


_MODEL_NAMES_LOCK = threading.Lock()
_MODEL_NAMES_CACHE = {"at": 0.0, "names": None}


def _merge_configured_models(models: list) -> list:
    """把显式接入的额外渠道模型合进目录；同 id 不重复。"""
    merged = [m for m in (models or []) if isinstance(m, dict)]
    seen = {str(m.get("id") or "") for m in merged}
    for row in MODEL_ROUTER.catalog_models():
        mid = str(row.get("id") or "")
        if mid and mid not in seen:
            merged.append(row)
            seen.add(mid)
    return merged


def model_catalog_models() -> list:
    """模型目录取自推理上游的 /v1/models；60s 缓存。

    失败时保留上一份目录，只提前 10s 重试：不把一次超时钉成 60s 空目录。
    """
    with _MODEL_LIST_LOCK:
        cached = _MODEL_LIST_CACHE.get("models")
        if cached is not None and time.time() - _MODEL_LIST_CACHE.get("at", 0) < 60:
            return cached
    # 与反代同一条拼接规则：base 已带 /v1 时不再重复。
    models_url = MODEL_UPSTREAM + ("/models" if MODEL_UPSTREAM.endswith("/v1") else "/v1/models")
    req = urllib.request.Request(models_url, headers={"accept": "application/json"})
    token = model_token()
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with egress.open_url(req, timeout=8,
                             proxy_url=CONTROL.egress_proxy()) as res:
            payload = json.loads(res.read(2 * 1024 * 1024).decode("utf-8", "replace"))
        models = payload.get("data") if isinstance(payload, dict) else None
        models = models if isinstance(models, list) else []
        models = _merge_configured_models(models)
    except Exception as exc:
        print("[HUB NEO] model catalog error: %s" % exc, file=sys.stderr, flush=True)
        with _MODEL_LIST_LOCK:
            _MODEL_LIST_CACHE.update({
                "at": time.time() - 50,
                "models": _MODEL_LIST_CACHE.get("models") or [],
            })
        return _merge_configured_models(_MODEL_LIST_CACHE.get("models") or [])
    with _MODEL_LIST_LOCK:
        _MODEL_LIST_CACHE.update({"at": time.time(), "models": models})
    return models


def registered_model_names(timeout: int = 6):
    """拉取上游目录的规范全称集合，供分组保存时 Fail-Closed 防呆。

    上游不可达时返回 None（保存只做结构校验，不阻断离线编排）；
    30s 缓存避免连续编排反复拉 285 行目录。
    """
    if LEDGER_MODE == "local":
        # 本地模式不去云端 kswa 取名单：分组常引用尚未接入的渠道，返回 None
        # 表示只做结构校验（与“上游不可达”同一语义），不阻断离线编排。
        return None
    with _MODEL_NAMES_LOCK:
        cached = _MODEL_NAMES_CACHE.get("names")
        if cached is not None and time.time() - _MODEL_NAMES_CACHE.get("at", 0) < 30:
            return cached
    req = urllib.request.Request(UPSTREAM + "/api/readmodel/catalog",
                                 headers={"accept": "application/json"})
    token = upstream_token()
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with egress.open_url(req, timeout=timeout, proxy_url=CONTROL.egress_proxy()) as res:
            payload = json.loads(res.read(4 * 1024 * 1024).decode("utf-8", "replace"))
        models = payload.get("models") if isinstance(payload, dict) else None
        names = {
            "%s/%s" % (m.get("channel"), m.get("id"))
            for m in (models or [])
            if isinstance(m, dict) and m.get("channel") and m.get("id")
        }
        if not names:
            return None
    except Exception:
        return None
    with _MODEL_NAMES_LOCK:
        _MODEL_NAMES_CACHE["at"] = time.time()
        _MODEL_NAMES_CACHE["names"] = names
    return names


def model_groups_health() -> dict:
    """供 /health 使用的分组目录状态，不暴露本机绝对路径。"""
    with MODEL_GROUPS_LOCK:
        healthy = _model_catalog_loaded and not _model_catalog_error
        groups = _model_catalog.list_groups() if healthy else []
        return {
            "count": len(groups),
            "version": _model_catalog.version if healthy else "",
            "error": _model_catalog_error or None,
        }


def reload_model_groups(force: bool = False) -> dict:
    """按 mtime/size 热加载 model_groups.json；失败保留旧缓存并抛 ValueError。"""
    global _model_catalog, _model_catalog_stamp, _model_catalog_loaded, _model_catalog_error
    path = Path(MODEL_GROUPS_PATH)
    try:
        stamp = _model_groups_stamp(path)
    except OSError as exc:
        message = "无法读取模型分组配置: %s: %s" % (path.name, exc)
        with MODEL_GROUPS_LOCK:
            _model_catalog_error = message
        raise ValueError(message) from exc

    with MODEL_GROUPS_LOCK:
        if not force and _model_catalog_loaded and _model_catalog_stamp == stamp:
            return _model_groups_snapshot_locked()
        try:
            catalog = ModelGroupCatalog().load_from_file(str(path))
        except ValueError as exc:
            # 不向 HTTP 客户端泄露本机绝对路径。
            message = str(exc).replace(str(path), path.name)
            _model_catalog_error = message
            raise ValueError(message) from exc
        _model_catalog = catalog
        _model_catalog_stamp = stamp
        _model_catalog_loaded = True
        _model_catalog_error = ""
        payload = _model_groups_snapshot_locked()
    print("[HUB NEO] model groups reloaded: %d group(s)" % payload["count"], flush=True)
    return payload


try:
    reload_model_groups(force=True)
except ValueError as exc:
    print("[HUB NEO] model groups unavailable: %s" % exc, file=sys.stderr, flush=True)


# ── 上游健康探针与一键回退 ─────────────────────────────────────
_UPSTREAM_LOCK = threading.Lock()
_PROBE_LOCK = threading.Lock()
_PROBE_STOP = threading.Event()
_PROBE_THREAD = None
_PROBE_STATE = {
    "ok": None,
    "status": "checking",
    "target": UPSTREAM,
    "role": "cloud",
    "checked_at": None,
    "latency_ms": None,
    "http_status": None,
    "error": "",
    "consecutive_failures": 0,
}


class UpstreamUnavailableError(RuntimeError):
    """目标上游探活失败，且调用方没有要求强制切换。"""

    def __init__(self, message: str, probe: dict):
        super().__init__(message)
        self.probe = probe


def upstream_role(url: str | None = None) -> str:
    target = (url or UPSTREAM).rstrip("/")
    if target == UPSTREAM_CLOUD:
        return "cloud"
    if target == UPSTREAM_LOCAL:
        return "local"
    return "custom"


def upstream_token(url: str | None = None) -> str:
    """按目标选择 Bearer token，避免回退到本地后误用云端 token。"""
    target = (url or UPSTREAM).rstrip("/")
    if target == UPSTREAM_LOCAL and LOCAL_TOKEN:
        return LOCAL_TOKEN
    return TOKEN


def upstream_health_snapshot() -> dict:
    """返回最近一次探针结果；stale 只表示读数过期，不代表上游已挂。"""
    with _PROBE_LOCK:
        state = dict(_PROBE_STATE)
    checked_at = state.get("checked_at")
    age = None if checked_at is None else max(0.0, time.time() - checked_at)
    state["age_s"] = None if age is None else round(age, 1)
    state["stale"] = age is None or age > UPSTREAM_PROBE_INTERVAL * 2
    state["interval_s"] = UPSTREAM_PROBE_INTERVAL
    return state


def _probe_once(target: str, timeout: int) -> dict:
    started = time.monotonic()
    http_status = None
    error = ""
    ok = False
    status = "down"
    # R-1/NEO-GW-05:探针路径可配置(第三方上游可能没有 /health);
    # /v1/models 类探针:200 或 401(鉴权存活)均视为服务可达。
    probe_path = PROBE_PATH
    models_probe = probe_path.rstrip("/").endswith("/v1/models")
    req = urllib.request.Request(
        target + probe_path,
        headers={
            "accept": "application/json",
            "user-agent": "HUB-NEO-UpstreamProbe/1.0",
        },
    )
    token = upstream_token(target)
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with egress.open_url(req, timeout=timeout, proxy_url=CONTROL.egress_proxy()) as res:
            http_status = res.status
            raw = res.read(65536)
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                payload = {}
            if res.status == 200:
                # NEO-GW-05:200 即视为可达,不再强制 JSON status=ok(有则更佳)
                ok = True
                status = "ok"
            elif models_probe and res.status == 401:
                ok = True                            # 鉴权存活=服务存活
                status = "ok"
            else:
                status = "down"
                error = "HTTP %d" % res.status
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        if models_probe and exc.code == 401:
            ok = True                                # /v1/models 401=鉴权存活
            status = "ok"
        else:
            status = "auth_error" if exc.code in (401, 403) else "down"
            error = "HTTP %d" % exc.code
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
    return {
        "ok": ok,
        "status": status,
        "target": target,
        "role": upstream_role(target),
        "checked_at": time.time(),
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "http_status": http_status,
        "error": error,
        "consecutive_failures": 0,
        "probe_path": probe_path,
    }


PROBE_HEADERS = {
    "accept": "application/json",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}


def origin_of_text(url: str) -> str:
    """`https://host/a/b` → `https://host`；解不出给空串。"""
    parts = urlsplit(str(url or ""))
    if not parts.scheme or not parts.netloc:
        return ""
    return "%s://%s" % (parts.scheme, parts.netloc)


# 探测顺序：不同家把模型清单挂在不同路径上（ollama 原生 /api/tags、
# Azure 风格 /openai/models、聚合网关 /v1/providers/models …）。
# 只读 GET，按顺序试，谁先给出名单就用谁；全失败按最可信的状态回。
PROBE_MODEL_PATHS = (
    "/v1/models", "/models", "/api/v1/models", "/openai/v1/models",
    "/openai/models", "/v1/providers/models", "/api/models", "/api/tags",
)


def _probe_model_rows(doc) -> list:
    """从各种形状的清单文档里取出模型条目（list 或 {data|models|result}）。"""
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return []
    for key in ("data", "models", "result", "items", "available"):
        rows = doc.get(key)
        if isinstance(rows, list):
            return rows
    return []


def _probe_model_meta(item) -> dict:
    """把上游声明成条记录：常用字段全收，缺的一概不猜。"""
    if not isinstance(item, dict):
        mid = str(item or "").strip()
        return {"id": mid} if mid else {}
    mid = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
    if not mid:
        return {}
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    row = {"id": mid}
    for key, names in (("context", ("context_limit", "context_length", "context_window",
                                    "max_context_tokens", "max_input_tokens", "n_ctx")),
                       ("max_out", ("output_budget", "max_output_tokens", "max_completion_tokens",
                                     "max_tokens", "output_token_limit"))):
        for name in names:
            val = meta.get(name) if name in meta else item.get(name)
            if isinstance(val, (int, float)) and val > 0:
                row[key] = int(val)
                break
    mods = arch.get("input_modalities") or item.get("input_modalities") or item.get("modalities")
    if isinstance(mods, list):
        row["vision"] = any(str(m).lower() in ("image", "vision") for m in mods)
    elif isinstance(item.get("vision"), bool):
        row["vision"] = item["vision"]
    if isinstance(item.get("supports_vision"), bool):
        row["vision"] = item["supports_vision"]
    for key, names in (("tools", ("tools", "function_calling", "tool_use", "supports_tools")),
                       ("reasoning", ("reasoning", "thinking", "supports_reasoning"))):
        for name in names:
            for bag in (caps, item):
                if isinstance(bag.get(name), bool):
                    row[key] = bag[name]
        if key not in row:
            caps_list = item.get("capabilities") if isinstance(item.get("capabilities"), list) else []
            if caps_list and any(str(c).lower() == key for c in caps_list):
                row[key] = True
    # 价目只认上游自己写出来的数字，不当“输出=输入×N”推算。
    price = {}
    for key, names in (("in", ("input", "prompt", "input_usd_per_1m", "input_price")),
                       ("cache", ("cache_read", "cached_input", "cache_read_input_token_cost",
                                  "cache_read_usd_per_1m")),
                       ("out", ("output", "completion", "output_usd_per_1m", "output_price"))):
        for name in names:
            val = pricing.get(name, item.get(name))
            if isinstance(val, (int, float)) and val >= 0:
                price[key] = float(val)
                break
    if price:
        row["pricing"] = price
        unit = pricing.get("unit") or item.get("price_unit")
        if unit:
            row["price_unit"] = str(unit)
    for key in ("owned_by", "type", "family", "endpoint", "provider"):
        if item.get(key):
            row[key] = str(item[key])
    if isinstance(item.get("created"), (int, float)):
        row["created"] = int(item["created"])
    return row


def channel_models_probe(base_url: str, token: str = "", headers: dict | None = None,
                         timeout: float = 8.0, fetcher=None) -> dict:
    """渠道接入前探测：按已知端点清单试读模型目录，把名单与元数据回给表单。

    200 解析名单；401/403 = 鉴权不通（auth_error，绝不刷新凭证）；
    其他 = down。`fetcher` 供单测注入假上游，不打真实网络。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise ValueError("上游地址必须以 http:// 或 https:// 开头")
    started = time.monotonic()
    send = fetcher or urllib.request.urlopen
    models: list = []
    details: dict = {}
    tried: list = []
    status, http_status, error, url = "down", None, "", ""
    for path in PROBE_MODEL_PATHS:
        url = join_url(base, path)          # base 已带 /v1 时不重复
        # 必须带浏览器 UA：云盾（Cloudflare）会把 Python-urllib 直接 403，
        # 与密钥对错无关（opencode / ollama 实测都是这个坑）。
        req = urllib.request.Request(url, headers=dict(PROBE_HEADERS))
        req.add_header("referer", base + "/")
        req.add_header("origin", origin_of_text(base))
        if token:
            req.add_header("authorization", "Bearer " + str(token))
        for key, value in (headers or {}).items():
            req.add_header(str(key), str(value))
        attempt = {"path": path, "url": url, "status": "down", "http_status": None,
                   "count": 0, "error": ""}
        try:
            with send(req, timeout=timeout) as res:
                attempt["http_status"] = getattr(res, "status", None) or 200
                doc = json.loads(res.read(2 * 1024 * 1024).decode("utf-8", "replace"))
            rows = _probe_model_rows(doc)
            for item in rows:
                meta = _probe_model_meta(item)
                mid = meta.get("id")
                if not mid:
                    continue
                if mid not in models:
                    models.append(mid)
                details.setdefault(mid, meta)
            attempt["count"] = len(models)
            attempt["status"] = "ok" if models else "no_models"
            if models:
                status, http_status, error = "ok", attempt["http_status"], ""
                tried.append(attempt)
                break
        except urllib.error.HTTPError as exc:
            attempt["http_status"] = exc.code
            attempt["status"] = "auth_error" if exc.code in (401, 403) else "down"
            attempt["error"] = "HTTP %d" % exc.code
            if attempt["status"] == "auth_error":
                status, http_status, error = "auth_error", exc.code, attempt["error"]
        except Exception as exc:                # noqa: BLE001 探测失败按状态回，不掐连接
            attempt["error"] = str(exc) or exc.__class__.__name__
            if status != "auth_error":
                status, error = "down", attempt["error"]
        tried.append(attempt)
    if not models:
        # 有端点回了 200 但名单为空 = 上游真没模型，不是网络不通；否则按 down/鉴权报。
        if status != "auth_error":
            status = ("no_models" if any(a.get("http_status") == 200 for a in tried)
                      else "down")
    return {
        "ok": status == "ok",
        "status": status,
        "url": url,
        "models": models,
        "count": len(models),
        "details": [details[m] for m in models if m in details],
        "fields": sorted({k for d in details.values() for k in d if k != "id"}),
        "tried": tried,
        "http_status": http_status,
        "error": error,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "checked_at": int(time.time()),
    }


def probe_upstream(url: str | None = None, timeout: int | None = None) -> dict:
    """GET 目标 /health；只把 200 + status=ok 记为健康。

    单次 TLS EOF / 连接抖动不直接判死：失败后再探一次；仍失败才累计
    连续失败。探针结果会写入全局状态，但仅当目标仍是当前 UPSTREAM
    时才覆盖；切换目标后的旧探针结果不能把新上游的状态写脏。
    """
    target = (url or UPSTREAM).rstrip("/")
    attempts = 2
    result = None
    for attempt in range(attempts):
        result = _probe_once(target, timeout or UPSTREAM_PROBE_TIMEOUT)
        result["attempts"] = attempt + 1
        if result["ok"] or result["status"] == "auth_error":
            break
        if attempt < attempts - 1:
            time.sleep(0.35)
    with _PROBE_LOCK:
        if target == UPSTREAM:
            previous = int(_PROBE_STATE.get("consecutive_failures") or 0)
            result = dict(result)
            result["consecutive_failures"] = 0 if result["ok"] else previous + 1
            _PROBE_STATE.clear()
            _PROBE_STATE.update(result)
    return result


def _probe_loop():
    while not _PROBE_STOP.is_set():
        try:
            probe_upstream()
        except Exception as exc:
            print("[HUB NEO] upstream probe error: %s" % exc,
                  file=sys.stderr, flush=True)
        _PROBE_STOP.wait(UPSTREAM_PROBE_INTERVAL)


def start_upstream_probe():
    global _PROBE_THREAD
    if _PROBE_THREAD and _PROBE_THREAD.is_alive():
        return
    _PROBE_STOP.clear()
    _PROBE_THREAD = threading.Thread(
        target=_probe_loop, name="hub-neo-upstream-probe", daemon=True)
    _PROBE_THREAD.start()


def switch_upstream(target: str, force: bool = False) -> dict:
    """切换到 cloud/local；默认先探活，目标不可用则拒绝切换。"""
    global UPSTREAM
    target = str(target or "").strip().lower()
    if target not in ("cloud", "local"):
        raise ValueError("target 必须是 cloud 或 local")
    desired = UPSTREAM_CLOUD if target == "cloud" else UPSTREAM_LOCAL
    if not desired:
        raise ValueError("目标上游未配置")
    probe = probe_upstream(desired)
    if not probe["ok"] and not force:
        raise UpstreamUnavailableError(
            "目标上游不可达：%s（%s）" % (desired, probe["error"] or "health 探针失败"),
            probe,
        )
    with _UPSTREAM_LOCK:
        changed = desired != UPSTREAM
        if changed:
            UPSTREAM = desired
    with _PROBE_LOCK:
        _PROBE_STATE.clear()
        _PROBE_STATE.update(probe)
    return {
        "ok": True,
        "changed": changed,
        "target": target,
        "forced": bool(force and not probe["ok"]),
        "upstream": UPSTREAM,
        "upstream_role": upstream_role(),
        "upstream_health": probe,
    }


def upstream_state() -> dict:
    return {
        "upstream": UPSTREAM,
        "upstream_role": upstream_role(),
        "upstream_cloud": UPSTREAM_CLOUD,
        "upstream_local": UPSTREAM_LOCAL,
        "upstream_health": upstream_health_snapshot(),
    }


class HubNeoHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def end_headers(self):
        try:
            is_api = (getattr(self, "path", "") or "").startswith("/api/")
            hardening.inject_security_headers(self, is_api=is_api)
        except Exception:
            pass
        # 静态资源走 heuristic 缓存会导致改完 JS/CSS 仍跑旧文件（账页曾因此停在旧口径）；
        # 一律要求浏览器回源校验，命中 304 时才复用。
        if not (getattr(self, "path", "") or "").startswith("/api/"):
            # 壳页（/ 与 *.html）必须每次回源：只靠 ?v= 升版时，浏览器缓存的旧壳会
            # 继续引用旧 JS/CSS，改完页面对不上（曾因此以为布局又坏了）。
            # 注意只能发一个 cache-control：两条同时出现时浏览器只看第一条。
            path = (getattr(self, "path", "") or "").split("?")[0]
            if path in ("", "/") or path.endswith(".html"):
                self.send_header("cache-control", "no-store, must-revalidate")
            else:
                self.send_header("cache-control", "no-cache")
        super().end_headers()

    def _proxy(self, method: str):
        # 每个 HTTP keep-alive 请求都要重置，避免上一笔轨迹粘到这一笔。
        self._dispatch_trace = None
        self._route = None
        # 1. 读取请求体
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_REQUEST_BYTES:                    # P0-1: 大包防护
            self._json(413, {"error": {"message": "Request body too large (%d > %d bytes)" % (length, MAX_REQUEST_BYTES)}})
            return
        body = self.rfile.read(length) if length > 0 else None
        # 追踪号必须先于分流裁决生成：台账与响应头共用同一个 X-Request-Id。
        req_id = inject_trace_context(dict(self.headers)).get("X-Request-Id") \
            or self.headers.get("x-request-id") or str(uuid.uuid4())

        # 2. 构造上游请求；拒绝路径穿越，查询串原样透传
        request_path = urlsplit(self.path).path
        if ".." in request_path:
            self._json(400, {"error": {"message": "Invalid upstream path"}})
            return
        # 2a group-model dispatch (redline 7): non auto/ models pass through
        #    untouched; auto/* goes through RequestDispatcher (NEO-GW-02 wiring).
        #    先裁决再选上游：分组可能把 auto/* 换成 clef:27b。
        if method == "POST" and body:
            body = self._resolve_group_model(body, req_id)
            if body is None:                     # dispatch already answered
                return
        # 2b 模型级上游路由：显式接入的模型改道，其余零篡改走默认上游。
        infer = request_path.startswith("/v1/")
        route = None
        if infer:
            if method == "POST" and body:
                route = MODEL_ROUTER.resolve(
                    self._body_model(body), request_path,
                    default_upstream=MODEL_UPSTREAM,
                    default_token=model_token())
            else:
                route = MODEL_ROUTER.default_route(
                    request_path, upstream=MODEL_UPSTREAM,
                    token=model_token())
            self._route = route
        # 2c 预算硬闸门：今日支出已到上限就连提示词自愈也不触发，直接不打上游。
        #    闸门是事后计数，允许最后一笔穿透上限；跨日由 today_spend 按当天零点重算。
        if method == "POST" and body and infer:
            gate = budget_block(route.channel if route else LEDGER_CHANNEL)
            if gate.get("blocked"):
                self._json(402, {"error": {
                    "message": "budget exceeded: %s spent %.6f >= cap %.6f CNY/%s" % (
                        gate["key"], gate["spent_cny"], gate["cap_cny"], gate["period"]),
                    "code": "budget_exceeded", "key": gate["key"],
                    "cap_cny": gate["cap_cny"], "spent_cny": gate["spent_cny"],
                    "period": gate["period"], "config": str(CONTROL_PATH)}})
                return
        # 2d prompt self-heal (NEO-GW-04): first-turn gate + 1.5s fail-open.
        #    is_group judged on the raw body model so auto/* still triggers.
        prompt_opt_headers = {
            "x-hub-prompt-opt": self.headers.get("x-hub-prompt-opt") or "",
            "internal_refiner": self.headers.get("internal_refiner") or "",
        }
        if method == "POST" and body:
            body = self._maybe_heal_prompt(body, prompt_opt_headers)
        # 2d-2 上下文 L1 堆栈折叠（NEO-GW-02.5）：只折旧 role=tool 回显，
        #      前 3 条非 system 消息与 tool_call_id 一律不碰。默认 enforce，
        #      HUB_CONTEXT_FOLD=shadow|off 可回退为只统计/不介入。
        if method == "POST" and body and infer:
            body = self._maybe_fold_context(body)
        # 2e 决策模型：入口仍是 /v1/chat/completions，落到渠道声明的
        #    /v1/systemone，并把 model 改写成规范 id。
        if route is not None and route.decision and body:
            body = self._adapt_decision_body(body, route)
            if body is None:                     # 已直接应答
                return
        # 2f 本地面板停用的渠道/模型直接拒：否则“面板 OFF 但还能打”就是假开关。
        if method == "POST" and body and infer:
            blocked = self._control_blocked_model(body, route.channel if route else "")
            if blocked:
                self._json(403, {"error": {
                    "message": "model disabled by local control: %s" % blocked,
                    "code": "model_disabled",
                    "config": str(CONTROL_PATH)}})
                return
        # 推理链路按模型选上游；其余 /api/* 仍按通用上游（可切云端/本地）。
        target = route.base_url if route else (MODEL_UPSTREAM if infer else UPSTREAM)
        # 模型上游常带 /v1（如 https://host/v1），而入口路径也是 /v1/*：
        # 直接相加会拼成 /v1/v1/...，故重复段只保留一份。
        if route is not None:
            url = route.url
        else:
            url = (target + self.path[3:]) if (target.endswith("/v1") and self.path.startswith("/v1/")) \
                else (target + self.path)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("accept", self.headers.get("accept", "application/json"))

        # 3. 注入认证与反代头（目标切换时随上游换 token）
        token = route.token if route else (model_token() if infer else upstream_token())
        if token:
            req.add_header("authorization", f"Bearer {token}")
        # 3a. 第三方订阅的自定义出站头（OpenCode 等）：只注入该渠道声明的头，
        #     host / content-length 这类由网关自己管的头配置了也不生效。
        if route is not None:
            for header_name, header_value in route.safe_headers().items():
                req.add_header(header_name, header_value)
        if body is not None:
            req.add_header("content-type", self.headers.get("content-type", "application/json"))

        # 4. W3C 链路追踪与反代标头注入
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        if req_id:
            req.add_header("X-Request-Id", req_id)
        for name in ("traceparent", "tracestate", "user-agent"):
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)
        # 上游可能是云端域名：Host 必须指向上游，否则 Cloudflare/虚拟主机返回 403。
        # 原始 Host 用 X-Forwarded-Host 保留，便于上游审计。
        upstream_host = urlsplit(target).netloc
        host = self.headers.get("host")
        if host:
            req.add_header("X-Forwarded-Host", host)
        if upstream_host:
            req.add_header("host", upstream_host)
        req.add_header("X-Forwarded-For", client_ip)
        req.add_header("X-Real-IP", client_ip)
        req.add_header("X-Forwarded-Proto", self.headers.get("x-forwarded-proto") or "http")

        # 5. 执行代理转发（超时可从路由策略面板调，缺省 180s 支持批量任务）
        upstream_started = time.perf_counter()
        try:
            with egress.open_url(req,
                                 timeout=float(CONTROL.routing_policy().get("timeout_s") or 180),
                                 proxy_url=CONTROL.egress_proxy(route.channel if route else "")) as res:
                # 本地账本模式用网关自己的 X-Request-Id 记账，不能被上游 rid 改写；
                # 反代模式才把上游 hub-ng 的 x-hub-rid 绑成 usage 行的 join 键。
                if LEDGER_MODE != "local":
                    self._bind_dispatch_rid(res.headers.get("x-hub-rid"))
                ctype = res.headers.get("content-type", "application/json")
                # SSE（流式推理）不缓冲：上游一块就转一块，客户端才看得到首 token；
                # 其余 JSON 响应仍整包下发（要改 content-length / 做 trace 回填）。
                if "text/event-stream" in ctype.lower() and method != "HEAD":
                    payload, ttft, sent, stream_err = self._stream_sse(
                        res, ctype, req_id, upstream_started, res.status)
                    if infer and method == "POST":
                        self._record_inference(payload, body, res.status, upstream_started,
                                               req_id, client_ip, ttft_ms=ttft,
                                               ok=200 <= int(res.status) < 300, error=stream_err,
                                               channel=route.channel if route else LEDGER_CHANNEL,
                                               upstream=target,
                                               model_override=route.request_model if route else "")
                    self._finish_dispatch(res.status, upstream_started, sent, stream_err,
                                          state="failed" if stream_err else "done")
                    return
                payload, ttft = self._read_upstream(res, upstream_started)
                # 非流式回执也过同一套工具审计：完整 arguments 命中危险命令时可安全改写，
                # 不留“只有流式有防线、普通 JSON 裸奔”的口子。shadow 只计数。
                if infer and method == "POST" and TOOL_GUARD_MODE != "off" \
                        and "json" in ctype.lower():
                    scrubbed = sse_heal.scrub_payload(payload, TOOL_GUARD_MODE)
                    if scrubbed is not payload:
                        payload = scrubbed
                        self._tool_guard_rewrote = True
                if method == "GET" and urlsplit(self.path).path == "/api/usage":
                    payload = self._attach_usage_traces(payload)
                if infer and method == "POST":
                    self._record_inference(payload, body, res.status, upstream_started, req_id,
                                           client_ip, ttft_ms=ttft,
                                           channel=route.channel if route else LEDGER_CHANNEL,
                                           upstream=target,
                                           model_override=route.request_model if route else "")
                self._finish_dispatch(res.status, upstream_started, len(payload))
                self._access_log(res.status, res.status, upstream_started, len(payload), req_id, client_ip)
                self.send_response(res.status)
                self.send_header("content-type", res.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(payload)))
                self.send_header("cache-control", "no-store")
                self.send_header("x-content-type-options", "nosniff")
                if req_id:
                    self.send_header("X-Request-Id", req_id)
                self._emit_dispatch_headers()
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            # 反代模式的错误计费行也要能 join；本地模式的 rid 由本网关记账，不改写。
            if LEDGER_MODE != "local":
                self._bind_dispatch_rid(exc.headers.get("x-hub-rid"))
            if infer and method == "POST":
                self._record_inference(payload, body, exc.code, upstream_started,
                                       req_id, client_ip, ok=False,
                                       channel=route.channel if route else LEDGER_CHANNEL,
                                       upstream=target,
                                       model_override=route.request_model if route else "")
            self._finish_dispatch(exc.code, upstream_started, len(payload),
                                  self._short_upstream_error(payload))
            self._access_log(exc.code, exc.code, upstream_started, len(payload), req_id, client_ip)
            self.send_response(exc.code)
            self.send_header("content-type", exc.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            if req_id:
                self.send_header("X-Request-Id", req_id)
            self._emit_dispatch_headers()
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)
        except Exception as exc:
            if infer and method == "POST":
                self._record_inference(b"", body, 0, upstream_started, req_id,
                                       client_ip, ok=False, error=str(exc),
                                       channel=route.channel if route else LEDGER_CHANNEL,
                                       upstream=target,
                                       model_override=route.request_model if route else "")
            self._finish_dispatch(None, upstream_started, 0, str(exc), state="failed")
            self._access_log(502, None, upstream_started, 0, req_id, client_ip)
            payload = json.dumps({"error": {"message": f"Upstream connect error: {exc}"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            if req_id:
                self.send_header("X-Request-Id", req_id)
            self._emit_dispatch_headers()
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)

    def _maybe_heal_prompt(self, body: bytes, opt_headers: dict):
        """NEO-GW-04 首轮提示词自愈:skip 判定 -> 1.5s fail-open 调用 -> 改写。

        仅 POST 且末轮为 user 的请求可能被改写;任何失败原样返回 body。
        enforce 生效时置 _prompt_opt_applied 供响应头回显。"""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return body
        if not isinstance(payload, dict):
            return body
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return body
        model = payload.get("model")
        is_group = isinstance(model, str) and is_group_id(model)
        task_id = (self.headers.get("x-hub-task-id") or "").strip()
        text, applied, reason = PROMPT_OPTIMIZER.maybe_optimize(
            messages, opt_headers, is_group, task_id)
        if applied:
            messages[-1]["content"] = text
            self._prompt_opt_applied = True
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if reason == "shadow_recorded":
            self._prompt_opt_shadow = True
        return body

    def _maybe_fold_context(self, body: bytes):
        """L1 上下文折叠：只改写 messages 里旧 role=tool 的 content。

        返回 bytes（enforce 且真折了才重新序列化；其余原样返回）。
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return body
        if not isinstance(payload, dict):
            return body
        info = context_fold.apply(payload, mode=CONTEXT_FOLD_MODE)
        if not info.get("folded"):
            return body
        self._ctx_fold_saved = int(info.get("saved_tokens") or 0)
        if info.get("mode") != "enforce":
            self._ctx_fold_shadow = True
            return body
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _resolve_group_model(self, body: bytes, req_id: str = ""):
        """auto/ 分组模型经 RequestDispatcher 换算;其余原样返回(零篡改)。

        返回 None 表示裁决失败且已直接应答客户端(400 未定义分组 / 503 无可用成员)。
        task_id 取自 x-hub-task-id 头;缺失则视为无主请求,不绑粘性锁。
        成功裁决会登记一笔 `DispatchLedger` 轨迹;直通单模型不留痕。
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return body
        if not isinstance(payload, dict):
            return body
        model = payload.get("model")
        if not isinstance(model, str) or not is_group_id(model):
            return body
        task_id = (self.headers.get("x-hub-task-id") or "").strip()
        started = time.perf_counter()
        try:
            target = DISPATCHER.dispatch(model, payload, task_id, _model_catalog)
        except KeyError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return None
        except RuntimeError as exc:
            self._json(503, {"error": {"message": str(exc)}})
            return None
        dispatch_ms = (time.perf_counter() - started) * 1000
        lock_ttl = 0
        if task_id:
            for row in DISPATCHER.task_lock.snapshot():
                if row.get("task_id") == task_id and row.get("group_id") == target.group_id:
                    lock_ttl = int(row.get("ttl_remaining") or 0)
                    break
        lock_enabled = bool((target.policy or {}).get("task_lock"))
        if target.locked:
            lock_state = "HIT"
        elif not lock_enabled:
            lock_state = "OFF"
        elif not task_id:
            lock_state = "BYPASS"
        else:
            lock_state = "BOUND"
        self._dispatch_trace = DISPATCH_LEDGER.start(
            rid=req_id,
            task_id=task_id,
            requested_model=model,
            model=target.model,
            group_id=target.group_id,
            locked=target.locked,
            tier=target.tier,
            priority=target.priority,
            policy=target.policy,
            lock_state=lock_state,
            lock_ttl=lock_ttl,
            dispatch_ms=dispatch_ms,
            path=urlsplit(self.path).path,
        )
        payload["model"] = target.model
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _body_model(body: bytes) -> str:
        """只读请求体里的 model；解析失败按空处理（走默认上游）。"""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("model") or "").strip()

    def _adapt_decision_body(self, body: bytes, route):
        """把 chat 入口请求改写成 systemone 请求；缺 state/questions 直接 400。

        不把 messages 猜成 state：决策语义与聊天不同，猜出来的结果不可信。
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {
                "message": "decision request body must be JSON",
                "code": "decision_payload_invalid", "channel": route.channel}})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": {
                "message": "decision request body must be a JSON object",
                "code": "decision_payload_invalid", "channel": route.channel}})
            return None
        if not payload.get("state") or not isinstance(payload.get("questions"), dict):
            self._json(400, {"error": {
                "message": "%s is a decision model; send state + questions to /v1/systemone"
                           % route.request_model,
                "code": "decision_payload_required", "channel": route.channel,
                "endpoint": route.path}})
            return None
        # systemone 回 JSON，不是 SSE；把 chat 专用字段去掉，避免上游 400。
        for key in ("stream", "messages", "max_tokens", "temperature",
                    "top_p", "tools", "tool_choice", "response_format"):
            payload.pop(key, None)
        payload["model"] = route.request_model
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _control_blocked_model(self, body: bytes, channel: str = ""):
        """返回被本地面板停用的模型名；未停用返回 None。

        只拦明确 OFF 的模型与整个渠道：没表态的模型跟随上游，不因未知被拒。
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return None
        channel = (channel or LEDGER_CHANNEL).strip()
        if not CONTROL.channel_enabled(channel):
            return model
        if CONTROL.model_enabled(channel, model) is False:
            return model
        return None

    def _stream_sse(self, res, ctype: str, req_id, started: float, status: int):
        """真流式转发：上游来一块就写一块（HTTP/1.1 chunked），同时累计用于记账。

        TTFT 仍是首个非空块到达时刻——与客户端实际拿到首字节的时机一致。
        客户端中途断开只记一笔带错误文本的账，不把网关自己弄挂。
        """
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("transfer-encoding", "chunked")
        if req_id:
            self.send_header("X-Request-Id", req_id)
        self._emit_dispatch_headers()
        self.end_headers()
        chunks, ttft, sent, error = [], None, 0, ""
        healer = sse_heal.Healer(SSE_HEAL_MODE, TOOL_GUARD_MODE)

        def _flush(out: bytes) -> None:
            nonlocal ttft, sent
            if not out:
                return
            if ttft is None:
                ttft = (time.perf_counter() - started) * 1000
            chunks.append(out)
            self.wfile.write(b"%x\r\n" % len(out) + out + b"\r\n")
            self.wfile.flush()
            sent += len(out)

        try:
            while True:
                chunk = res.read(8192)
                if not chunk:
                    break
                _flush(healer.feed(chunk))
            _flush(healer.finish())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = "client disconnected: %s" % exc
            self.close_connection = True
        except Exception as exc:
            error = "stream error: %s" % exc
            self.close_connection = True
        return b"".join(chunks), ttft, sent, error

    @staticmethod
    def _read_upstream(res, started: float):
        """逐块读完回执，同时记下首字节到达时刻用于 TTFT。

        客户端仍是整包下发（不改既有行为），但 TTFT 是真实测得的：
        从向上游发出请求到第一个非空数据块落地的毫秒数。
        """
        chunks, first_at = [], None
        while True:
            chunk = res.read(8192)
            if not chunk:
                break
            if first_at is None:
                first_at = time.perf_counter()
            chunks.append(chunk)
        payload = b"".join(chunks)
        ttft = None if first_at is None else (first_at - started) * 1000
        return payload, ttft

    def _record_inference(self, payload, body, status, started: float, req_id,
                          client_ip, ok=None, error="", ttft_ms=None,
                          channel="", upstream="", model_override=""):
        """把一次推理回执记进本地账本。解析不到 usage 也留一行，不留黑洞。"""
        elapsed = (time.perf_counter() - started) * 1000
        req_model, stream = "", False
        if body:
            try:
                sent = json.loads(body.decode("utf-8", "replace"))
                req_model = str(sent.get("model") or "")
                stream = bool(sent.get("stream"))
            except Exception:
                pass
        success = (200 <= int(status) < 300) if ok is None else bool(ok)
        try:
            LEDGER.record_from_payload(
                payload, request_model=req_model, status=status, ok=success,
                error=error or ("" if success else self._short_upstream_error(payload)),
                elapsed_ms=elapsed, ttft_ms=ttft_ms, rid=req_id,
                channel=channel or LEDGER_CHANNEL,
                upstream=upstream or MODEL_UPSTREAM,
                model_override=model_override, client_ip=client_ip, stream=stream)
        except Exception as exc:
            print("[HUB NEO] ledger record error: %s" % exc, file=sys.stderr, flush=True)

    def _model_catalog(self) -> list:
        return model_catalog_models()

    def _local_read(self, path: str, query: str = ""):
        """本地读模型：账本 + 渠道/environment 配置，一次请求一次派生。"""
        return LocalReadModel(
            LEDGER, channel=LEDGER_CHANNEL, upstream=MODEL_UPSTREAM,
            models=self._model_catalog(), token=MODEL_TOKEN,
            control=CONTROL,
        ).payload(path, query)

    def _ledger_payload(self, path: str, query: str = ""):
        """账本读接口一律由 8888 自己作答，不转发。"""
        params = parse_qs(query)
        try:
            limit = max(1, min(500, int((params.get("limit") or ["50"])[0] or 50)))
            offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
        except (TypeError, ValueError):
            limit, offset = 50, 0
        if path == "/api/usage":
            # 本地账本行同样带 rid；在读侧按完全相等挂派发轨迹，
            # 否则 02 流水行拿不到 GROUP / LOCKED / TIER。
            payload = LEDGER.usage_payload(limit=limit, offset=offset)
            return self._attach_usage_traces_doc(payload)[0]
        if path == "/api/status":
            return LEDGER.status_payload(LEDGER_CHANNEL)
        if path == "/api/readmodel/dashboard":
            return LEDGER.dashboard_payload(LEDGER_CHANNEL)
        if path == "/api/routes":
            return LEDGER.routes_payload(LEDGER_CHANNEL, MODEL_UPSTREAM,
                                          proxy_url=CONTROL.egress_proxy(),
                                          model_upstreams=MODEL_ROUTER.summary())
        if path == "/api/readmodel/catalog":
            channels = [row.get("channel") for row in MODEL_ROUTER.summary().get("channels", [])]
            return LEDGER.catalog_payload(
                self._model_catalog(), LEDGER_CHANNEL,
                disabled=CONTROL.disabled_models(LEDGER_CHANNEL),
                channel_enabled=CONTROL.channel_enabled(LEDGER_CHANNEL),
                disabled_map={name: CONTROL.disabled_models(name) for name in channels if name},
                enabled_map={name: CONTROL.channel_enabled(name) for name in channels if name})
        return {"rows": [], "generated_at": int(time.time()), "source": "local-ledger"}

    def _bind_dispatch_rid(self, rid):
        """把上游 x-hub-rid 绑到本笔派发轨迹；没有轨迹/没有头时保持原样。"""
        trace = getattr(self, "_dispatch_trace", None)
        if not trace or not rid:
            return
        updated = DISPATCH_LEDGER.bind_rid(trace["trace_id"], rid)
        if updated:
            self._dispatch_trace = updated

    def _finish_dispatch(self, status, started: float, response_bytes: int,
                         error: str = "", state: str = "done"):
        trace = getattr(self, "_dispatch_trace", None)
        if not trace:
            return
        DISPATCH_LEDGER.finish(
            trace["trace_id"],
            status=status,
            ms=(time.perf_counter() - started) * 1000,
            response_bytes=response_bytes,
            error=error,
            state=state,
        )

    def _emit_dispatch_headers(self):
        """回填真实调度与上游标头；直通请求不添加任何伪造的分组值。"""
        route = getattr(self, "_route", None)
        if route is not None:
            self.send_header("X-Hub-Channel", str(route.channel).encode("ascii", "replace").decode("ascii"))
            self.send_header("X-Hub-Upstream-Model", str(route.request_model or "").encode("ascii", "replace").decode("ascii"))
        # 上下文折叠与工具守卫的结果先于 trace 早退发出：直通单模型也要能看见。
        if getattr(self, "_ctx_fold_saved", 0):
            self.send_header("X-Hub-Ctx-Saved", str(int(self._ctx_fold_saved)))
        if getattr(self, "_ctx_fold_shadow", False):
            self.send_header("X-Hub-Ctx-Fold", "shadow")
        if getattr(self, "_tool_guard_rewrote", False):
            self.send_header("X-Hub-Tool-Guard", "rewritten")
        trace = getattr(self, "_dispatch_trace", None)
        if not trace:
            return
        def clean(value):
            return str(value or "").encode("ascii", "replace").decode("ascii")
        self.send_header("X-Hub-Group", clean(trace.get("group_id")))
        self.send_header("X-Hub-Model", clean(trace.get("model")))
        self.send_header("X-Hub-Task-Locked", "1" if trace.get("locked") else "0")
        self.send_header("X-Hub-Lock-State", clean(trace.get("lock_state")))
        self.send_header("X-Hub-Route-Tier", clean(trace.get("tier") or "-"))
        self.send_header("X-Hub-Trace-Id", clean(trace.get("trace_id")))
        # 只有上游真的回了 x-hub-rid 才发；直通/bench 不伪造 usage join key。
        if trace.get("upstream_rid"):
            self.send_header("X-Hub-Rid", clean(trace.get("upstream_rid")))
        if getattr(self, "_prompt_opt_applied", False):
            self.send_header("x-hub-prompt-opt", "1")
        elif getattr(self, "_prompt_opt_shadow", False):
            self.send_header("x-hub-prompt-opt-shadow", "1")

    @staticmethod
    def _attach_usage_traces_doc(doc: dict) -> tuple:
        """把派发轨迹挂到带同一 rid 的计费行；返回 (doc, matched)。

        优先用上游 x-hub-rid；本地账本模式下 rid 就是网关 X-Request-Id。
        只在完全相等时挂，不在时间/模型名上做邻接推断，也不用 uid 冒充。
        """
        if not isinstance(doc, dict):
            return doc, 0
        rows = doc.get("rows")
        if not isinstance(rows, list) or not rows:
            return doc, 0
        by_rid = {}
        for trace in DISPATCH_LEDGER.snapshot(200):
            rid = str(trace.get("upstream_rid") or trace.get("rid") or "")
            if rid:
                by_rid[rid] = trace
        if not by_rid:
            return doc, 0
        matched = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            trace = by_rid.get(str(row.get("rid") or ""))
            if trace is not None:
                row["hub_trace"] = trace
                matched += 1
        return doc, matched

    @classmethod
    def _attach_usage_traces(cls, payload: bytes) -> bytes:
        """反代模式：上游计费行带 rid 时按请求号挂派发轨迹。"""
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return payload
        doc, matched = cls._attach_usage_traces_doc(doc)
        if not matched:
            return payload
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _short_upstream_error(payload: bytes) -> str:
        """只取上游错误摘要，最多 180 字；不记录请求体或响应正文。"""
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or err.get("type")
        elif isinstance(err, str):
            msg = err
        else:
            msg = data.get("message")
        return str(msg or "")[:180]

    def _json(self, status: int, data: dict, head_only: bool = False):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _read_json_body(self) -> dict:
        """统一读 JSON 体：超限 / 非 JSON / 非对象一律 ValueError（400 口径）。"""
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_REQUEST_BYTES:
            raise ValueError("请求体超过上限")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("请求体必须是 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _audit(self, action: str, detail=None):
        """写操作审计：JSON 一行，带操作者 IP 与动作（DoD 硬要求）。"""
        try:
            sys.stderr.write(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "event": "write",
                "action": action,
                "client_ip": self._client_ip(),
                "path": urlsplit(self.path).path,
                "detail": detail or {},
            }, ensure_ascii=False) + "\n")
        except Exception:                       # noqa: BLE001 审计失败不影响业务回执
            pass

    def _serve_custom_save(self):
        """新增 / 覆盖第三方渠道：写 model_upstreams.json 并热加载路由表。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Custom upstream write requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            with MODEL_UPSTREAMS_WRITE_LOCK:
                result = CUSTOM_UPSTREAMS.save(data)
                MODEL_ROUTER.reload()
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "渠道配置落盘失败: %s" % exc}})
            return
        except Exception as exc:                # noqa: BLE001 密钥仓/加密异常不拖垮连接
            self._json(500, {"error": {"message": "渠道配置写入失败: %s" % exc}})
            return
        result["router_reloaded"] = True
        self._audit("custom.save", {"channel": result.get("channel"),
                                    "models": result.get("models"),
                                    "has_key": result.get("has_key")})
        self._json(200, result)

    def _serve_custom_probe(self):
        """接入前探测模型：只读上游 /v1/models，不落盘、不热加载。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Custom probe requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            base_url = str(data.get("base_url") or "").strip()
            name = str(data.get("name") or "").strip()
            token, headers = CUSTOM_UPSTREAMS.probe_auth(name, data.get("api_key"),
                                                         data.get("headers"))
            # 会话请求头自动填：凭据仓里存过这个渠道的会话/cookie，就直接带上，
            # 不要让用户再手贴一遍（面板只报“已带会话 / 未带会话”）。
            has_cookie = any(str(key).lower() == "cookie" for key in headers)
            if name and not has_cookie:
                saved = VENDOR_CREDS.get(name) or {}
                cookie = str(saved.get("cookie") or saved.get("cookies") or "").strip()
                if not cookie:
                    session = price_sync.session_from_creds(saved)
                    cookie = ("%s=%s" % (price_sync.SESSION_COOKIE, session)) if session else ""
                if cookie:
                    headers["cookie"] = cookie
                    has_cookie = True
            result = channel_models_probe(base_url, token, headers)
            result["session_header"] = has_cookie
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        # 价目单独探：探测失败也不影响模型名单。已知渠道走自己的官方价目源
        # （ollama/opencode 是公开网页表格，open-design 是免登录 JSON），
        # 其余才试公开 endpoints。
        feeds = price_feeds.feeds_for(name)
        if feeds:
            found = price_feeds.fetch_feed(feeds[0], base_url=base_url)
            prices = {"ok": bool(found.get("ok")), "source_url": found.get("src") or "",
                      "rows": price_feeds.rows_of(found.get("models") or {}),
                      "error": found.get("error") or "", "feed": feeds[0]}
        else:
            found = price_sync.fetch_official(base_url, timeout=8)
            prices = {"ok": found["ok"], "source_url": found["source_url"],
                      "rows": found["rows"], "error": found["error"]}
        result["prices"] = {"ok": prices["ok"], "source_url": prices["source_url"],
                            "count": len(prices["rows"]), "rows": prices["rows"],
                            "error": prices["error"], "feed": prices.get("feed") or ""}
        self._audit("custom.probe", {"channel": name, "ok": result.get("ok"),
                                     "status": result.get("status"),
                                     "count": result.get("count")})
        self._json(200, result)

    def _pricing_session(self, channel: str, given: str = "") -> tuple:
        """→ `(会话串, 来源, 登录失败原因)`。

        顺序：显式传入 > 环境变量 > 凭据仓里的会话 cookie > **用凭据仓的账号密码现登
        换取会话**（登录态抓价的唯一正路：API Key 打账单端点只会 401）。
        """
        if given:
            return str(given).strip(), "body", None
        env = str(os.environ.get("HUB_OD_SESSION") or "").strip()
        if env:
            return env, "env:HUB_OD_SESSION", None
        if not channel:
            return "", "", None
        creds = VENDOR_CREDS.get(channel) or {}
        token = price_sync.session_from_creds(creds)
        if token:
            return token, "vendor_creds:%s" % channel, None
        email = str(creds.get("username") or creds.get("email") or "").strip()
        password = str(creds.get("password") or "")
        if not (email and password):
            return "", "", None
        now = time.time()
        with _PRICING_SESSION_LOCK:
            cached = _PRICING_SESSION_CACHE.get(channel) or {}
        if cached.get("session") and now - float(cached.get("at") or 0) < _PRICING_SESSION_TTL:
            return cached["session"], "vendor_login_cache:%s" % channel, None
        base_url = ""
        try:
            saved = CUSTOM_UPSTREAMS._read_config()["channels"].get(channel) or {}
            base_url = str(saved.get("base_url") or "")
        except (OSError, ValueError):
            base_url = ""
        got = price_sync.login_session(base_url or price_sync.OD_API, email, password)
        if not got.get("ok"):
            return "", "", {"code": got.get("code") or "login_failed",
                             "error": got.get("error") or "登录失败",
                             "endpoint": got.get("endpoint") or ""}
        with _PRICING_SESSION_LOCK:
            _PRICING_SESSION_CACHE[channel] = {"session": got["session"], "at": now}
        try:                                  # 会话回写凭据仓：下次直接复用，不必每分钟重登
            VENDOR_CREDS.save(channel, dict(creds, session=got["session"]))
        except (OSError, ValueError):
            pass
        return got["session"], "vendor_login:%s" % channel, None

    def _serve_pricing_feeds(self):
        """官方价目抓取：按渠道跑公开价目页/免登录端点，可选落盘。

        不走登录态、不带用户凭据，所以能直接答“官方表上是多少”。
        账号级结算价（unitPrices）仍然走 `/api/pricing/sync?mode=account`。
        """
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Pricing feeds require loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        wanted = data.get("channels") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        custom = {}
        try:
            custom = CUSTOM_UPSTREAMS._read_config()["channels"]
        except (OSError, ValueError):
            custom = {}
        if not wanted:
            known = set(custom) | set((price_sync.read_doc(LEDGER.pricing_path).get("upstreams") or {}))
            wanted = sorted(ch for ch in known if price_feeds.feeds_for(ch))
        apply_it = bool(data.get("apply"))
        results, written_total = {}, 0
        for channel in wanted:
            ch = str(channel or "").strip()
            feeds = price_feeds.feeds_for(ch)
            if not feeds:
                results[ch] = {"ok": False, "code": "no_feed",
                               "error": "该渠道没有公开价目源，只能走登录态或人工录入"}
                continue
            base_url = str((custom.get(ch) or {}).get("base_url") or "")
            found = price_feeds.fetch_feed(feeds[0], base_url=base_url)
            item = {"ok": bool(found.get("ok")), "feed": feeds[0],
                    "source_url": found.get("src") or "",
                    "count": len(found.get("models") or {}),
                    "error": found.get("error") or "", "applied": None}
            if item["ok"] and apply_it:
                item["applied"] = price_sync.apply_prices(
                    LEDGER.pricing_path, ch, price_feeds.rows_of(found["models"]),
                    found.get("src") or "",
                    source_label="官方价目页 %s" % (found.get("src") or ch))
                written_total += int((item["applied"] or {}).get("written") or 0)
            results[ch] = item
        self._audit("pricing.feeds", {"channels": list(results), "apply": apply_it,
                                       "written": written_total})
        self._json(200, {"ok": any(v.get("ok") for v in results.values()),
                         "channels": results, "written": written_total,
                         "at": int(time.time())})

    def _serve_pricing_sync(self):
        """价目同步：默认走**登录态官方账单 unitPrices**，可选落盘。绝不推算、不补 0。

        `mode=account`（默认）= 只认登录态；取不到就如实报 no_session/auth_error，
        不偷偷回退公开目录价。`mode=public` / `allow_public=true` 才用公开价目端点。
        """
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Pricing sync requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            channel = str(data.get("channel") or "").strip().lower()
            mode = str(data.get("mode") or "account").strip().lower()
            if mode not in ("account", "public", "auto"):
                raise ValueError("mode 只支持 account / public / auto")
            base_url = str(data.get("base_url") or "").strip()
            if not base_url and channel:
                saved = CUSTOM_UPSTREAMS._read_config()["channels"].get(channel) or {}
                base_url = str(saved.get("base_url") or "")
            session, session_src, login_fail = self._pricing_session(
                channel, data.get("session"))
            stored_keys = sorted((VENDOR_CREDS.get(channel) or {}).keys()) if channel else []
            found = {"ok": False, "code": "", "rows": [], "skipped": [],
                     "source_url": "", "error": ""}
            source = ""
            if mode in ("account", "auto"):
                if session:
                    found = price_sync.fetch_account_prices(
                        session, workspace=str(data.get("workspace") or ""), timeout=20)
                    source = "account"
                elif login_fail:
                    found = {"ok": False, "code": login_fail["code"], "rows": [], "skipped": [],
                             "source_url": "",
                             "error": "用凭据仓账号密码换登录态失败：%s" % login_fail["error"]}
                    source = "account"
                else:
                    found = {"ok": False, "code": "no_session", "rows": [], "skipped": [],
                             "source_url": "",
                             "error": "没有 OpenDesign 登录态：凭据仓里既无会话 cookie，也无账号密码"}
                    source = "account"
            if not found["ok"]:
                if mode == "account" and not data.get("allow_public"):
                    self._json(200, {
                        "ok": False, "code": found.get("code") or "account_unavailable",
                        "source": "account", "channel": channel, "session_src": session_src,
                        "error": found.get("error") or "登录态取价失败",
                        "stored": stored_keys,
                        "hint": (("凭据仓现有字段 %s，其中没有会话也没有账号密码（API Key 对账单端点只会 401）。"
                                  % ",".join(stored_keys)) if stored_keys and not session else "")
                        + ("到 06 渠道机柜的「官方登录」填 open-design 的账号 + 密码，网关会用"
                           " sign-in/email 现换会话 cookie 再抓 unitPrices；也可以直接粘会话 cookie"),
                        "applied": None})
                    self._audit("pricing.sync", {"channel": channel, "ok": False,
                                                 "code": found.get("code")})
                    return
                if not base_url:
                    raise ValueError("公开价目端点需要上游地址：填 BASE URL 或已保存的渠道标识")
                found = price_sync.fetch_official(base_url, timeout=12)
                found.setdefault("code", "")
                source = "public"
            out = {"ok": found["ok"], "channel": channel, "source": source,
                   "session_src": session_src, "code": found.get("code") or "",
                   "source_url": found["source_url"], "count": len(found["rows"]),
                   "rows": found["rows"], "skipped": found.get("skipped") or [],
                   "error": found["error"], "applied": None}
            if found["ok"] and data.get("apply"):
                if not channel:
                    raise ValueError("落盘需要渠道标识")
                out["applied"] = price_sync.apply_prices(
                    LEDGER.pricing_path, channel, found["rows"], found["source_url"],
                    source_label=("登录态官方账单 unitPrices %s" % found["source_url"]
                                  if source == "account" else None))
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "价目表落盘失败: %s" % exc}})
            return
        self._audit("pricing.sync", {"channel": out.get("channel"), "ok": out["ok"],
                                      "source": out.get("source"),
                                      "count": out["count"],
                                      "written": (out["applied"] or {}).get("written")})
        self._json(200, out)

    def _serve_custom_delete(self, name: str):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Custom upstream delete requires loopback same-origin UI"}})
            return
        try:
            with MODEL_UPSTREAMS_WRITE_LOCK:
                result = CUSTOM_UPSTREAMS.delete(name)
                MODEL_ROUTER.reload()
        except ValueError as exc:
            status = 404 if "不存在" in str(exc) else 400
            self._json(status, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "渠道配置落盘失败: %s" % exc}})
            return
        result["router_reloaded"] = True
        self._audit("custom.delete", {"channel": result.get("deleted")})
        self._json(200, result)

    def _serve_keys_issue(self):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "KEY issue requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            result = LOCAL_KEYS.issue(
                data.get("name"), data.get("realm") or "all",
                data.get("scope") or "", data.get("note") or "")
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "KEY 落盘失败: %s" % exc}})
            return
        self._audit("keys.issue", {"id": result.get("issued"), "name": data.get("name"),
                                   "realm": data.get("realm") or "all"})
        self._json(200, result)

    def _serve_keys_toggle(self, path: str):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "KEY toggle requires loopback same-origin UI"}})
            return
        key_id = unquote(path[len("/api/keys/"):-len("/toggle")])
        try:
            data = self._read_json_body()
            enabled = data.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("enabled 必须是 true/false")
            result = LOCAL_KEYS.toggle(key_id, enabled)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "KEY 落盘失败: %s" % exc}})
            return
        self._audit("keys.toggle", {"id": key_id, "enabled": result.get("enabled")})
        self._json(200, result)

    def _serve_keys_realm(self, path: str):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "KEY realm change requires loopback same-origin UI"}})
            return
        key_id = unquote(path[len("/api/keys/"):-len("/realm")])
        try:
            data = self._read_json_body()
            result = LOCAL_KEYS.set_realm(key_id, data.get("realm") or "all")
        except ValueError as exc:
            status = 404 if "不存在" in str(exc) else 400
            self._json(status, {"error": {"message": str(exc)}})
            return
        self._audit("keys.realm", {"id": key_id, "realm": result.get("realm")})
        self._json(200, result)

    def _serve_keys_revoke(self, key_id: str):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "KEY revoke requires loopback same-origin UI"}})
            return
        try:
            result = LOCAL_KEYS.revoke(key_id)
        except ValueError as exc:
            self._json(404, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "KEY 落盘失败: %s" % exc}})
            return
        self._audit("keys.revoke", {"id": key_id})
        self._json(200, result)

    def _serve_admin_password(self):
        """改管理员口令：回环同源 + 旧口令校验 + scrypt 重算落盘。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Password change requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        old = str(data.get("old_password") or data.get("current_password") or "")
        new = str(data.get("new_password") or data.get("password") or "")
        if adminauth.ADMIN_PASS_HASH and not adminauth.verify_password(
                old, adminauth.ADMIN_PASS_HASH):
            self._json(401, {"error": {"message": "当前口令不正确",
                                       "code": "invalid_old_password"}})
            return
        try:
            result = adminauth.set_password(new)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "口令落盘失败: %s" % exc}})
            return
        self._audit("admin.password", {"updated_at": result.get("updated_at")})
        self._json(200, result)

    def _serve_admin_locked_ips(self):
        if not self._is_loopback():
            self._json(403, {"error": {"message": "Locked IP list requires loopback access"}})
            return
        stats = adminauth.fail_stats()
        rows = [{"ip": ip, "fails": int(rec.get("fails") or 0),
                 "locked_for": int(rec.get("locked") or 0)}
                for ip, rec in stats.items()]
        rows.sort(key=lambda row: (row["locked_for"] > 0, row["fails"]), reverse=True)
        self._json(200, {"ok": True, "rows": rows,
                         "locked": [row for row in rows if row["locked_for"] > 0],
                         "total": len(rows)})

    def _serve_egress_probe(self):
        """双腿探活：不做写入，但仍要求回环同源，避免被外部当免费探针。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Egress probe requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            result = egress_probe_payload(data)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        self._audit("egress.probe", {"channel": result.get("channel"),
                                     "ok": result.get("ok"),
                                     "effective": result.get("effective"),
                                     "proxy_url": result.get("proxy_url")})
        self._json(200, result)

    def _serve_oc_bridge_token(self):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "OC bridge token requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
            if data.get("clear"):
                result = OC_BRIDGE.set_token("")
            else:
                result = OC_BRIDGE.set_token(data.get("token"), data.get("note") or "")
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "Bridge Token 落盘失败: %s" % exc}})
            return
        except Exception as exc:                # noqa: BLE001 密钥仓异常按 500 回，不掐连接
            self._json(500, {"error": {"message": "Bridge Token 写入失败: %s" % exc}})
            return
        self._audit("oc_bridge.token", {"configured": result.get("configured"),
                                        "cleared": result.get("cleared")})
        self._json(200, result)

    def _serve_pricing_single(self):
        """单模型改价：前端按 CNY/百万 token 输入，落盘换算成 USD_per_mtok。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Pricing edit requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        channel = str(data.get("channel") or "").strip()
        model = str(data.get("model") or "").strip()
        if not channel or not model:
            self._json(400, {"error": {"message": "channel 与 model 必填"}})
            return
        doc = LEDGER.pricing_doc()
        try:
            fx = float(doc.get("usd_cny") or 6.72)
        except (TypeError, ValueError):
            fx = 6.72
        fx = fx or 6.72

        def to_usd(key):
            value = data.get(key)
            if value in (None, ""):
                return None
            try:
                num = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("%s 必须是数字" % key) from exc
            if num < 0:
                raise ValueError("%s 不能为负" % key)
            return round(num / fx, 6)

        try:
            in_usd = to_usd("in_cny")
            cache_usd = to_usd("cache_cny")
            out_usd = to_usd("out_cny")
            if in_usd is None or out_usd is None:
                raise ValueError("in_cny 与 out_cny 必填")
            result = LEDGER.import_pricing({
                "channel": channel,
                "rows": [{"model": model, "in": in_usd,
                          "cache": in_usd if cache_usd is None else cache_usd,
                          "out": out_usd}],
            }, mode="merge", dry_run=False, actor=self._client_ip())
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "价目落盘失败: %s" % exc}})
            return
        result.update({"channel": channel, "model": model,
                       "in_cny": data.get("in_cny"), "out_cny": data.get("out_cny"),
                       "cache_cny": data.get("cache_cny"), "usd_cny": fx})
        self._audit("pricing.single", {"channel": channel, "model": model,
                                       "in_cny": data.get("in_cny"),
                                       "out_cny": data.get("out_cny")})
        self._json(200, result)

    def _serve_groups_create(self):
        """新建路由分组：校验后原子写 model_groups.json 并立即热加载。"""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Group create requires loopback same-origin UI"}})
            return
        try:
            data = self._read_json_body()
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        group_id = str(data.get("group_id") or "").strip()
        if not group_id:
            self._json(400, {"error": {"message": "group_id 不可为空"}})
            return
        members = data.get("members") or []
        policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
        endpoint = str(data.get("endpoint") or "").strip()
        if endpoint:
            policy["endpoint"] = endpoint
        payload = {"name": str(data.get("name") or group_id).strip() or group_id,
                   "policy": policy, "members": members}
        try:
            registered = registered_model_names() if members else None
            validated = validate_group_definition(group_id, payload,
                                                  registered if registered is not None else None)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        path = Path(MODEL_GROUPS_PATH)
        with MODEL_GROUPS_WRITE_LOCK:
            try:
                raw_config = path.read_text(encoding="utf-8")
                doc = json.loads(raw_config)
            except (OSError, ValueError) as exc:
                self._json(500, {"error": {"message": "无法读取模型分组配置: %s" % exc}})
                return
            if not isinstance(doc.get("groups"), dict):
                self._json(500, {"error": {"message": "模型分组配置缺少 groups 对象"}})
                return
            if group_id in doc["groups"] and not data.get("overwrite"):
                self._json(409, {"error": {"message": "分组已存在: %s" % group_id}})
                return
            doc["groups"][group_id] = validated
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(str(tmp), str(path))
                result = reload_model_groups(force=True)
            except (OSError, ValueError) as exc:
                try:
                    path.write_text(raw_config, encoding="utf-8")
                    reload_model_groups(force=True)
                except (OSError, ValueError):
                    pass
                self._json(500, {"error": {"message": "保存分组失败: %s" % exc}})
                return
        result["created"] = group_id
        result["endpoint"] = policy.get("endpoint") or "/v1/chat/completions"
        self._audit("groups.create", {"group_id": group_id,
                                      "members": len(validated["members"])})
        self._json(200, result)

    def _serve_group_delete(self, group_id: str):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Group delete requires loopback same-origin UI"}})
            return
        group_id = str(group_id or "").strip()
        if not group_id or not group_id.startswith("auto/"):
            self._json(400, {"error": {"message": "group_id 必须以 auto/ 开头"}})
            return
        path = Path(MODEL_GROUPS_PATH)
        with MODEL_GROUPS_WRITE_LOCK:
            try:
                raw_config = path.read_text(encoding="utf-8")
                doc = json.loads(raw_config)
            except (OSError, ValueError) as exc:
                self._json(500, {"error": {"message": "无法读取模型分组配置: %s" % exc}})
                return
            if group_id not in (doc.get("groups") or {}):
                self._json(404, {"error": {"message": "分组不存在: %s" % group_id}})
                return
            doc["groups"].pop(group_id, None)
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(str(tmp), str(path))
                result = reload_model_groups(force=True)
            except (OSError, ValueError) as exc:
                try:
                    path.write_text(raw_config, encoding="utf-8")
                    reload_model_groups(force=True)
                except (OSError, ValueError):
                    pass
                self._json(500, {"error": {"message": "删除分组失败: %s" % exc}})
                return
        result["deleted"] = group_id
        self._audit("groups.delete", {"group_id": group_id})
        self._json(200, result)

    def _serve_model_groups(self, head_only: bool = False):
        try:
            payload = reload_model_groups(force=False)
        except ValueError as exc:
            self._json(503, {"error": {"message": str(exc)}}, head_only=head_only)
            return
        self._json(200, payload, head_only=head_only)

    def _serve_model_groups_reload(self):
        # 先把请求体消费干净，保证 keep-alive 连接不串包。
        length = int(self.headers.get("content-length") or 0)
        if length > 0:
            self.rfile.read(length)
        if not self._reload_authorized():
            self._json(403, {"error": {"message": "Model group reload requires loopback access and admin token"}})
            return
        try:
            payload = reload_model_groups(force=True)
        except ValueError as exc:
            self._json(500, {"error": {"message": str(exc)}})
            return
        payload["reloaded"] = True
        self._json(200, payload)

    def _is_loopback(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return (host in ("127.0.0.1", "::1")
                or host.startswith("127.")
                or host.startswith("::ffff:127."))

    def _reload_authorized(self) -> bool:
        if not self._is_loopback():
            return False
        if not TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("authorization", ""), "Bearer " + TOKEN)

    def _switch_authorized(self) -> bool:
        """一键回退是本地同源页面动作：回环 + Origin 同源 + 自定义头。

        自定义头会触发跨站预检，而本服务不返回 CORS 允许头，因此普通
        网页无法伪造；仍保留 admin token 供 CLI / 脚本调用。未配置 token
        时，无 Origin 的本地 CLI 仍可用，但带 Origin 的请求必须同源。
        """
        if not self._is_loopback():
            return False
        if TOKEN and hmac.compare_digest(
                self.headers.get("authorization", ""), "Bearer " + TOKEN):
            return True
        if self.headers.get("x-hub-ui") == "1":
            origin = (self.headers.get("origin") or "").rstrip("/").lower()
            host = (self.headers.get("host") or "").strip().lower()
            if origin and origin in ("http://" + host, "https://" + host):
                return True
        return not TOKEN and not self.headers.get("origin")

    def _local_control_authorized(self) -> bool:
        """本地配置写：回环 + 同源 Origin（CLI 可无 Origin）。

        不返回 CORS 头，跨站页面即使能发 POST 也会因 Origin 不匹配被拒；
        这比仅凭 loopback 更严，不依赖前端自定义头。
        """
        if not self._is_loopback():
            return False
        origin = (self.headers.get("origin") or "").rstrip("/").lower()
        if not origin:
            return True
        host = (self.headers.get("host") or "").strip().lower()
        return bool(host) and origin in ("http://" + host, "https://" + host)

    def _serve_task_locks(self, head_only: bool = False):
        """本地内存锁快照：只对回环开放，任务号不扩散到局域网。"""
        if not self._is_loopback():
            self._json(403, {"error": {"message": "Task locks are loopback-only"}})
            return
        locks = DISPATCHER.task_lock.snapshot()
        self._json(200, {"count": len(locks), "locks": locks}, head_only=head_only)

    def _serve_dispatch_traces(self, head_only: bool = False):
        """当前进程的派发轨迹快照；与任务锁一样只对回环开放。"""
        if not self._is_loopback():
            self._json(403, {"error": {"message": "Dispatch traces are loopback-only"}})
            return
        raw_limit = parse_qs(urlsplit(self.path).query).get("limit", ["50"])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))
        traces = DISPATCH_LEDGER.snapshot(limit)
        self._json(200, {
            "generated_at": int(time.time()),
            "count": DISPATCH_LEDGER.count(),
            "limit": limit,
            "traces": traces,
        }, head_only=head_only)

    def _bench_one(self, model: str, timeout: float) -> dict:
        """打一发最小请求，量 TTFT 与总时延；不写账本。"""
        route = MODEL_ROUTER.resolve(
            model, default_upstream=MODEL_UPSTREAM,
            default_token=model_token())
        url = route.url
        if route.decision:
            # 决策模型没有 chat/completions：用一条 noul 问题探活。
            body = json.dumps({
                "model": route.request_model,
                "state": "HUB NEO reachability probe",
                "questions": {"reachable": {
                    "type": "noul",
                    "instructions": "Is this a reachability test?"}},
            }).encode("utf-8")
            accept = "application/json"
        else:
            body = json.dumps({"model": route.request_model or model,
                               "stream": True, "max_tokens": 1,
                               "messages": [{"role": "user", "content": "ping"}]}
                              ).encode("utf-8")
            accept = "text/event-stream"
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json", "accept": accept,
            "user-agent": "HUB-NEO-Bench/1.0"})
        token = route.token or model_token()
        if token:
            req.add_header("authorization", "Bearer " + token)
        started = time.perf_counter()
        ttft, received = None, 0
        try:
            with egress.open_url(req, timeout=timeout,
                                 proxy_url=CONTROL.egress_proxy(route.channel)) as res:
                while True:
                    chunk = res.read(4096)
                    if not chunk:
                        break
                    if ttft is None:
                        ttft = (time.perf_counter() - started) * 1000
                    received += len(chunk)
                return {"id": model, "ok": True, "status": res.status,
                        "ttft_ms": None if ttft is None else round(ttft, 1),
                        "total_ms": round((time.perf_counter() - started) * 1000, 1),
                        "bytes": received}
        except urllib.error.HTTPError as exc:
            detail = exc.read(200).decode("utf-8", "replace") if exc.fp else ""
            return {"id": model, "ok": False, "status": exc.code,
                    "error": (detail or str(exc))[:160]}
        except Exception as exc:  # noqa: BLE001
            return {"id": model, "ok": False, "status": 0, "error": str(exc)[:160]}

    def _serve_bench(self, data: dict):
        """本地测速：直连模型上游，只测不记；停用/隐藏的模型直接 skipped。"""
        ids = []
        for raw in data.get("ids") or []:
            model = str(raw).strip()
            if model and model not in ids:
                ids.append(model)
        if not ids:
            self._json(400, {"error": {"message": "ids 必须是非空列表"}})
            return
        only_enabled = data.get("only_enabled", True) is not False
        try:
            per = float(data.get("timeout") or 30)
        except (TypeError, ValueError):
            per = 30.0
        per = max(5.0, min(60.0, per))
        rows, passed, skipped = [], 0, 0
        for model in ids[:24]:
            channel = MODEL_ROUTER.resolve(model).channel
            if not CONTROL.channel_enabled(channel) or (
                    only_enabled
                    and CONTROL.model_enabled(channel, model) is False):
                skipped += 1
                rows.append({"id": model, "ok": False, "skipped": True,
                             "error": "本地面板已停用"})
                continue
            row = self._bench_one(model, per)
            rows.append(row)
            passed += 1 if row.get("ok") else 0
        self._json(200, {
            "ok": True, "channel": LEDGER_CHANNEL, "total": len(rows),
            "passed": passed, "skipped": skipped, "failed": len(rows) - passed - skipped,
            "recorded": False, "generated_at": int(time.time()),
            "note": "本地测速直连模型上游；诊断流量不记入账本",
            "rows": rows,
        })

    def _serve_control_write(self, path: str):
        """本地控制面写端点：落 control.json，不触达云端。

        回环 + 同源 Origin；金额/名单非法一律 400，绝不半份落盘。
        """
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_REQUEST_BYTES:
            self._json(413, {"error": {"message": "控制面请求体超过上限"}})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Local control requires loopback same-origin UI"}})
            return
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {"message": "请求体必须是 JSON"}})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": {"message": "请求体必须是 JSON 对象"}})
            return
        channel = str(data.get("channel") or LEDGER_CHANNEL).strip()
        try:
            if path == "/api/models/toggle":
                result = CONTROL.toggle_model(channel, data.get("id"), data.get("enabled"))
            elif path == "/api/models/toggle_bulk":
                result = CONTROL.toggle_models(channel, data.get("ids"), data.get("enabled"))
            elif path == "/api/models/bench":
                self._serve_bench(data)
                return
            elif path == "/api/accounts/import_text":
                channel = str(data.get("channel") or LEDGER_CHANNEL).strip()
                result = accounts.import_text(
                    LEDGER, channel, data.get("raw_text") or data.get("text") or "",
                    realm=str(data.get("realm") or "default"),
                    name=str(data.get("name") or ""))
            elif path == "/api/system/reload":
                result = system_reload()
                self._audit("system.reload", {"channels": result.get("channels"),
                                              "groups": result.get("groups")})
            elif path == "/api/system/cache/clear":
                result = system_cache_clear()
                self._audit("system.cache_clear", result.get("cleared"))
            elif path == "/api/system/logs/clear":
                result = system_logs_clear(keep=int(data.get("keep") or 5))
                self._audit("system.logs_clear", {"removed": result.get("removed"),
                                                  "freed_bytes": result.get("freed_bytes")})
                if not result.get("ok"):
                    raise ValueError("; ".join(result.get("failed") or []) or "日志清理失败")
            elif path == "/api/accounts/delete":
                target = data.get("id") or data.get("account_id")
                if not target:
                    raise ValueError("id 必填")
                result = accounts.delete_account(LEDGER, target)
                if not result.get("ok"):
                    raise ValueError(result.get("error") or "删除失败")
            elif path == "/api/official/vendor-login":
                result = vendor_login(data)
            elif path == "/api/channels/toggle":
                target = str(data.get("channel") or "").strip()
                if not target:
                    raise ValueError("channel 必填")
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必填（true=启用 / false=停用）")
                result = CONTROL.toggle_visibility(
                    target, not enabled, str(data.get("reason") or ""),
                    known_models=[m.get("id") if isinstance(m, dict) else str(m)
                                  for m in self._model_catalog()])
            elif path == "/api/accounts/checkin":
                acc_id = data.get("account_id") or data.get("id")
                if acc_id:
                    result = accounts.checkin_account(LEDGER, int(acc_id))
                else:
                    channel_filter = str(data.get("channel") or "").strip()
                    res = accounts.checkin_all(LEDGER, channel_filter)
                    result = {
                        "ok": True,
                        "total": res["total"],
                        "done": res["passed"],
                        "checked": res["results"],
                        "source": "local",
                    }
            elif path == "/api/accounts/inspect":
                channel_filter = str(data.get("channel") or "").strip()
                acc_list = accounts.list_accounts(LEDGER, channel_filter)
                result = {
                    "ok": True,
                    "total": len(acc_list),
                    "passed": sum(1 for a in acc_list if a.get("status") == "active"),
                    "failed": sum(1 for a in acc_list if a.get("status") != "active"),
                    "accounts": acc_list,
                    "source": "local",
                }
            elif path == "/api/errors/fix":
                result = CONTROL.ack_errors(data.get("ids") or [])
            elif path == "/api/egress/proxy-url":
                result = CONTROL.set_egress(data.get("url") or "")
            elif path == "/api/official/quota-snapshot":
                channel = str(data.get("channel") or LEDGER_CHANNEL).strip()
                if data.get("refresh"):
                    quar = channel_quota_map([channel], refresh={channel})[channel]
                    if quar.get("source") in (quota_engine.SOURCE_LIVE, quota_engine.SOURCE_MANUAL):
                        result = {"ok": True, "channel": channel,
                                  "snapshot": quota_engine.save_snapshot(channel, quar),
                                  "quota": quar, "at": int(time.time())}
                    else:
                        result = {"ok": False, "channel": channel, "quota": quar,
                                  "error": "官方不可达且无手工数据（source=%s）" % quar.get("source")}
                else:
                    result = {"ok": True, "channel": channel,
                              "snapshot": quota_engine.save_snapshot(channel, data),
                              "at": int(time.time())}
            elif path == "/api/visibility":
                action = str(data.get("action") or "toggle").strip().lower()
                if action != "toggle":
                    raise ValueError("仅支持 action=toggle")
                hidden = data.get("hidden")
                if not isinstance(hidden, bool):
                    # 启停是强动作：必须显式带 hidden，不因空体盲翻。
                    raise ValueError("hidden 必填（true=停用 / false=恢复）")
                result = CONTROL.toggle_visibility(
                    channel, hidden, str(data.get("reason") or ""),
                    known_models=[m.get("id") if isinstance(m, dict) else str(m)
                                  for m in self._model_catalog()])
            elif path == "/api/routes/policy":
                result = CONTROL.set_routing_policy(data)
            elif path == "/api/egress/channel-override":
                result = CONTROL.set_channel_egress(
                    str(data.get("channel") or ""),
                    str(data.get("mode") or "inherit"),
                    str(data.get("url") or ""))
            elif path == "/api/prompt-opt/mode":
                result = CONTROL.set_prompt_opt_mode(data.get("mode"))
                PROMPT_OPTIMIZER.set_mode(result["mode"])
                result["runtime"] = {"mode": PROMPT_OPTIMIZER.mode,
                                     "refiner_model": PROMPT_OPTIMIZER.refiner_model,
                                     "timeout_s": PROMPT_OPTIMIZER.timeout}
            elif path == "/api/rate-limit/config":
                wanted = {}
                for source, target in (("rps", "rate_limit_rps"),
                                       ("rate_limit_rps", "rate_limit_rps"),
                                       ("burst", "rate_limit_burst"),
                                       ("rate_limit_burst", "rate_limit_burst")):
                    if target not in wanted and data.get(source) not in (None, ""):
                        wanted[target] = data.get(source)
                if not wanted:
                    raise ValueError("rps 或 burst 至少给一个")
                result = CONTROL.set_routing_policy(wanted)
                info = RATE_LIMITER.set_rate(
                    result["routing"].get("rate_limit_rps") or RATE_LIMITER.rate,
                    result["routing"].get("rate_limit_burst") or RATE_LIMITER.burst)
                result["rate_limit"] = {"rps": info["rate"], "burst": info["burst"]}
            else:  # /api/budgets
                result = CONTROL.set_budget(
                    str(data.get("key") or "global"), data.get("amount"),
                    str(data.get("unit") or "CNY"), str(data.get("label") or ""))
            if path == "/api/routes/policy" and isinstance(result, dict):
                # 只有真的改了限流参数才重建令牌桶；单纯调超时不能顺手清空桶。
                if any(key in data for key in ("rate_limit_rps", "rate_limit_burst")):
                    info = RATE_LIMITER.set_rate(
                        result["routing"].get("rate_limit_rps") or RATE_LIMITER.rate,
                        result["routing"].get("rate_limit_burst") or RATE_LIMITER.burst)
                    result["rate_limit"] = {"rps": info["rate"], "burst": info["burst"]}
                self._audit("routes.policy", result.get("routing"))
            if path in ("/api/egress/channel-override", "/api/prompt-opt/mode",
                        "/api/rate-limit/config"):
                self._audit(path.strip("/").replace("/", "."), result)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except OSError as exc:
            self._json(500, {"error": {"message": "控制面落盘失败: %s" % exc}})
            return
        self._json(200, result)

    def _serve_task_lock_release(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Task lock release requires loopback same-origin UI"}})
            return
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {"message": "请求体必须是 JSON"}})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": {"message": "请求体必须是 JSON 对象"}})
            return
        task_id = str(data.get("task_id") or "").strip()
        group_id = str(data.get("group_id") or "").strip()
        if not task_id or not group_id:
            self._json(400, {"error": {"message": "task_id 与 group_id 均不可为空"}})
            return
        released = DISPATCHER.task_lock.release(task_id, group_id)
        self._json(200, {
            "ok": True,
            "released": released,
            "task_id": task_id,
            "group_id": group_id,
            "remaining": DISPATCHER.task_lock.size(),
        })

    def _serve_pricing_import(self):
        """价目表录入：回环 + 同源；校验后原子回写，支持 dry-run。

        只接收价目数据，不触达 /v1 或云端；结构与旧 HUB 的
        catalog/custom_prices.json 一致（unit=USD_per_mtok，按渠道分块）。
        """
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_REQUEST_BYTES:
            self._json(413, {"error": {"message": "价目表请求体超过上限"}})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Pricing import requires loopback same-origin UI"}})
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {"message": "请求体必须是 JSON"}})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": {"message": "请求体必须是 JSON 对象"}})
            return
        query = parse_qs(urlsplit(self.path).query)
        mode = str(payload.get("mode") or (query.get("mode") or ["merge"])[0]).lower()
        dry = payload.get("dry_run")
        if isinstance(dry, str):
            dry = dry.strip().lower() not in ("0", "false", "no")
        if dry is None:
            # 默认只预览；显式 dry_run=false 或 ?apply=1 才落盘。
            if query.get("apply"):
                dry = (query["apply"][0] or "").lower() not in ("1", "true", "yes")
            elif query.get("dry_run"):
                dry = (query["dry_run"][0] or "").lower() not in ("0", "false", "no")
            else:
                dry = True
        actor = (self.headers.get("x-hub-actor") or "local-ui").strip()[:64]
        try:
            result = LEDGER.import_pricing(payload, mode=mode, dry_run=bool(dry),
                                           actor=actor)
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": {"message": "价目表写入失败: %s" % exc}})
            return
        self._json(200, result)

    def _serve_groups_save(self):
        """校验后原子回写 model_groups.json，并立即热加载。

        防呆：规范全称必须 channel/model；能拿到上游目录时未知模型必须显式
        orphan:true（前端会为孤儿项自动打标），否则 400 拒绝写入。
        """
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Group save requires loopback same-origin UI"}})
            return
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {"message": "请求体必须是 JSON"}})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": {"message": "请求体必须是 JSON 对象"}})
            return
        group_id = str(data.get("group_id") or "").strip()
        if not group_id:
            self._json(400, {"error": {"message": "group_id 不可为空"}})
            return
        snapshot = model_groups_snapshot()
        existing = next((g for g in snapshot["groups"]
                         if g.get("group_id") == group_id), None)
        if existing is None:
            self._json(404, {"error": {"message": "分组不存在: %s" % group_id}})
            return
        policy = data.get("policy")
        if not isinstance(policy, dict):
            self._json(400, {"error": {"message": "policy 必须是对象"}})
            return
        members = data.get("members")
        if not isinstance(members, list) or not members:
            self._json(400, {"error": {"message": "members 必须是非空列表"}})
            return
        group_name = data.get("name")
        if not isinstance(group_name, str) or not group_name.strip():
            group_name = existing.get("name") or group_id
        payload = {"name": group_name, "policy": policy, "members": members}
        try:
            validated = validate_group_definition(
                group_id, payload, registered_model_names())
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        seen = set()
        for member in validated["members"]:
            if member["name"] in seen:
                self._json(400, {"error": {"message": "分组内模型重复: %s" % member["name"]}})
                return
            seen.add(member["name"])
        path = Path(MODEL_GROUPS_PATH)
        with MODEL_GROUPS_WRITE_LOCK:
            try:
                raw_config = path.read_text(encoding="utf-8")
                doc = json.loads(raw_config)
            except (OSError, ValueError) as exc:
                self._json(500, {"error": {"message": "无法读取模型分组配置: %s" % exc}})
                return
            if not isinstance(doc.get("groups"), dict):
                self._json(500, {"error": {"message": "模型分组配置缺少 groups 对象"}})
                return
            doc["groups"][group_id] = validated
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(str(tmp), str(path))
                result = reload_model_groups(force=True)
            except (OSError, ValueError) as exc:
                # 写盘或热加载失败：尽量回滚旧内容，绝不留半份配置。
                try:
                    path.write_text(raw_config, encoding="utf-8")
                    reload_model_groups(force=True)
                except (OSError, ValueError):
                    pass
                self._json(500, {"error": {"message": "保存分组失败: %s" % exc}})
                return
        result["saved"] = group_id
        result["members"] = len(validated["members"])
        self._json(200, result)

    def _serve_upstream_switch(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not self._switch_authorized():
            self._json(403, {"error": {"message": "Upstream switch requires loopback UI or admin token"}})
            return
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": {"message": "请求体必须是 JSON"}})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": {"message": "请求体必须是 JSON 对象"}})
            return
        try:
            result = switch_upstream(
                str(data.get("target") or ""), bool(data.get("force")))
        except UpstreamUnavailableError as exc:
            payload = upstream_state()
            payload.update({
                "error": {"message": str(exc)},
                "upstream_health": exc.probe,
            })
            self._json(409, payload)
            return
        except ValueError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        self._json(200, result)

    def _client_ip(self) -> str:
        ip = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if not ip:
            ip = self.client_address[0] if getattr(self, "client_address", None) else "127.0.0.1"
        return ip

    def _serve_admin_login(self):
        client_ip = self._client_ip()
        lock_secs = adminauth.locked_for(client_ip)
        if lock_secs > 0:
            self._json(429, {
                "error": {
                    "message": "Too many failed login attempts. IP locked for %d seconds." % lock_secs,
                    "retry_after": lock_secs,
                    "code": "auth_locked",
                }
            })
            return

        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}

        user = str(data.get("username") or data.get("user") or "").strip()
        pw = str(data.get("password") or data.get("pass") or "").strip()
        token = str(data.get("token") or "").strip()

        ok = False
        if token and (token == TOKEN or token == adminauth.ADMIN_TOKEN):
            ok = True
        elif user == adminauth.ADMIN_USER and adminauth.ADMIN_PASS_HASH:
            if adminauth.verify_password(pw, adminauth.ADMIN_PASS_HASH):
                ok = True
        elif not adminauth.ADMIN_PASS_HASH and not TOKEN and self._is_loopback():
            ok = True

        if ok:
            adminauth.note_success(client_ip)
            sid = adminauth.new_session()
            self._json(200, {
                "ok": True,
                "session_id": sid,
                "user": user or adminauth.ADMIN_USER,
                "message": "Login successful",
            })
        else:
            delay = adminauth.note_fail(client_ip)
            status = 429 if delay > 0 else 401
            self._json(status, {
                "error": {
                    "message": "Invalid credentials",
                    "code": "invalid_auth",
                    "locked_seconds": delay,
                }
            })

    def _serve_admin_auth_status(self):
        client_ip = self._client_ip()
        self._json(200, {
            "ok": True,
            "active_sessions": adminauth.session_count(),
            "ip_locked_for": adminauth.locked_for(client_ip),
            "password_configured": bool(adminauth.ADMIN_PASS_HASH),
            "token_configured": bool(TOKEN or adminauth.ADMIN_TOKEN),
        })

    def _serve_admin_unlock(self):
        if not self._is_loopback():
            self._json(403, {"error": {"message": "Unlock requires loopback access"}})
            return
        target = (parse_qs(urlsplit(self.path).query).get("ip") or [""])[0].strip()
        cnt = adminauth.reset_fails(target)
        self._json(200, {"ok": True, "reset_count": cnt, "ip": target or "*"})

    def _serve_accounts_import_text(self):
        if not self._local_control_authorized():
            self._json(403, {"error": {"message": "Account import requires loopback access and same-origin"}})
            return
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        channel = str(data.get("channel") or "").strip()
        raw_text = str(data.get("raw_text") or data.get("text") or "").strip()
        realm = str(data.get("realm") or "default").strip()
        name = str(data.get("name") or "").strip()
        res = accounts.import_text(LEDGER, channel, raw_text, realm=realm, name=name)
        status = 200 if res.get("ok") else 400
        self._json(status, res)

    def _check_rate_limit(self) -> bool:
        client_ip = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if not client_ip:
            client_ip = self.client_address[0] if getattr(self, "client_address", None) else "127.0.0.1"
        allowed, retry_after = RATE_LIMITER.check(client_ip, self.path, dict(self.headers))
        if not allowed:
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("retry-after", str(int(retry_after) or 1))
            payload = json.dumps({
                "error": {
                    "message": "Rate limit exceeded (%.0f req/s max)" % RATE_LIMITER.rate,
                    "retry_after": retry_after,
                }
            }).encode("utf-8")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return False
        return True

    def do_GET(self):
        if not self._check_rate_limit():
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._serve_health(probe_now="probe=1" in urlsplit(self.path).query.split("&"))
        elif path == "/api/models/groups":
            self._serve_model_groups()
        elif path == "/api/task_locks":
            self._serve_task_locks()
        elif path == "/api/dispatch/traces":
            self._serve_dispatch_traces()
        elif path == "/api/admin/auth-status":
            self._serve_admin_auth_status()
        elif path == "/api/admin/unlock":
            self._serve_admin_unlock()
        elif path == "/api/admin/locked-ips":
            self._serve_admin_locked_ips()
        elif path == "/api/custom":
            payload = CUSTOM_UPSTREAMS.payload()
            for item in payload.get("items") or []:
                item["enabled"] = CONTROL.channel_enabled(item.get("channel") or "")
            self._json(200, payload)
        elif path == "/api/keys":
            self._json(200, LOCAL_KEYS.list_public())
        elif path == "/api/routes/policy":
            self._json(200, {"ok": True, "routing": CONTROL.routing_policy(),
                             "rate_limit": {"rps": RATE_LIMITER.rate,
                                            "burst": RATE_LIMITER.burst}})
        elif path == "/api/reports/today":
            self._json(200, today_report_payload())
        elif path == "/api/oc-bridge/status":
            if not self._is_loopback():
                self._json(403, {"error": {"message": "OC bridge status requires loopback access"}})
            else:
                payload = OC_BRIDGE.public()
                payload["egress"] = CONTROL.egress_payload("opencode-free")
                payload["bridge_env_present"] = bool(os.environ.get("HUB_OC_BRIDGE_TOKEN"))
                self._json(200, payload)
        elif path == "/api/accounts/health":
            self._json(200, accounts.health_summary(LEDGER))
        elif path == "/api/channels/overview":
            self._json(200, channels_overview_payload())
        elif path == "/api/official/summary":
            self._json(200, official_summary_payload())
        elif path == "/api/accounts":
            channel_filter = parse_qs(urlsplit(self.path).query).get("channel", [""])[0]
            self._json(200, {"ok": True, "accounts": accounts.list_accounts(LEDGER, channel_filter)})
        elif path == "/api/pricing":
            self._json(200, {
                "ok": True,
                "summary": LEDGER.pricing_summary(),
                "rows": LEDGER.pricing_rows(),
                "doc": LEDGER.pricing_doc(),
                "generated_at": int(time.time()),
                "source": "local-pricing",
            })
        elif path == "/api/pricing/template":
            self._json(200, {
                "ok": True,
                "unit": "USD_per_mtok",
                "format": "upstreams[channel].models[model] = {in, cache, out, off_in?, off_cache?, off_out?}",
                "template": LEDGER.pricing_template(),
                "generated_at": int(time.time()),
            })
        elif path in LOCAL_READ_ROUTES:
            self._json(200, self._local_read(path, urlsplit(self.path).query))
        elif LEDGER_MODE == "local" and path in LEDGER_ROUTES:
            self._json(200, self._ledger_payload(path, urlsplit(self.path).query))
        elif LEDGER_MODE == "local" and path == "/api/models/mtok_prices":
            self._json(200, {
                "rows": LEDGER.pricing_rows(),
                "unit": LEDGER.pricing_summary().get("unit"),
                "usd_cny": LEDGER.pricing_summary().get("usd_cny"),
                "generated_at": int(time.time()), "source": "local-ledger"})
        elif path.startswith("/api/"):
            self._proxy_read("GET")
        elif path == "/v1/models":
            self._serve_models_list()
        else:
            super().do_GET()

    def do_HEAD(self):
        if not self._check_rate_limit():
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._serve_health(head_only=True)
        elif path == "/api/models/groups":
            self._serve_model_groups(head_only=True)
        elif path == "/api/dispatch/traces":
            self._serve_dispatch_traces(head_only=True)
        elif path == "/api/pricing":
            self._json(200, {
                "ok": True,
                "summary": LEDGER.pricing_summary(),
                "rows": LEDGER.pricing_rows(),
                "doc": LEDGER.pricing_doc(),
                "generated_at": int(time.time()),
                "source": "local-pricing",
            }, head_only=True)
        elif path == "/api/pricing/template":
            self._json(200, {
                "ok": True,
                "unit": "USD_per_mtok",
                "format": "upstreams[channel].models[model] = {in, cache, out, off_in?, off_cache?, off_out?}",
                "template": LEDGER.pricing_template(),
                "generated_at": int(time.time()),
            }, head_only=True)
        elif path == "/api/reports/today":
            self._json(200, today_report_payload(), head_only=True)
        elif path in LOCAL_READ_ROUTES:
            self._json(200, self._local_read(path, urlsplit(self.path).query),
                       head_only=True)
        elif LEDGER_MODE == "local" and path in LEDGER_ROUTES:
            self._json(200, self._ledger_payload(path, urlsplit(self.path).query),
                       head_only=True)
        elif path.startswith("/api/"):
            self._proxy_read("HEAD")
        elif path == "/v1/models":
            self._serve_models_list(head_only=True)
        else:
            super().do_HEAD()

    def _proxy_read(self, method: str):
        if is_read_blocked(urlsplit(self.path).path):
            self._json(403, {"error": {"message": "Sensitive endpoint blocked by HUB NEO; use host-side authorization"}})
            return
        self._proxy(method)

    def do_POST(self):
        if not self._check_rate_limit():
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._serve_health()
            return
        if path == "/api/models/groups/reload":
            self._serve_model_groups_reload()
            return
        if path == "/api/upstream/switch":
            self._serve_upstream_switch()
            return
        if path in ("/api/admin/login", "/admin/login"):
            self._serve_admin_login()
            return
        if path == "/api/accounts/import_text":
            self._serve_accounts_import_text()
            return
        if path == "/api/groups/save":
            self._serve_groups_save()
            return
        if path == "/api/pricing/import":
            self._serve_pricing_import()
            return
        if path == "/api/task_locks/release":
            self._serve_task_lock_release()
            return
        if path == "/api/custom":
            self._serve_custom_save()
            return
        if path == "/api/custom/probe":
            self._serve_custom_probe()
            return
        if path == "/api/pricing/sync":
            self._serve_pricing_sync()
            return
        if path == "/api/pricing/feeds":
            self._serve_pricing_feeds()
            return
        if path == "/api/keys":
            self._serve_keys_issue()
            return
        if path == "/api/admin/password":
            self._serve_admin_password()
            return
        if path == "/api/groups/create":
            self._serve_groups_create()
            return
        if path == "/api/egress/probe":
            self._serve_egress_probe()
            return
        if path == "/api/oc-bridge/token":
            self._serve_oc_bridge_token()
            return
        if path == "/api/pricing/single":
            self._serve_pricing_single()
            return
        if KEY_TOGGLE_RE.match(path):
            self._serve_keys_toggle(path)
            return
        if path in LOCAL_WRITE_ROUTES:
            self._serve_control_write(path)
            return
        if KEY_REALM_RE.match(path):
            self._serve_keys_realm(path)
            return
        if path.startswith("/v1/"):
            # 推理链路是网关本职:/v1/* 放行走反代(内含 auto/* 分组裁决),
            # 与 /api/* 管理写白名单互不影响。
            self._proxy("POST")
            return
        if is_write_allowed(path):
            self._proxy("POST")
        elif path.startswith("/api/"):
            self._json(403, {"error": {"message": "POST blocked: endpoint not in HUB NEO write whitelist"}})
        else:
            self.send_error(405)

    def do_DELETE(self):
        """本地删除动作（渠道 / KEY / 分组）：只对回环同源开放，逐条审计。"""
        if not self._check_rate_limit():
            return
        # 删除请求也可能带 body（部分客户端会发 JSON）：读干净，避免 keep-alive 串包。
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_REQUEST_BYTES:
            self._json(413, {"error": {"message": "删除请求体超过上限"}})
            return
        if length > 0:
            self.rfile.read(length)
        path = urlsplit(self.path).path
        if path.startswith("/api/custom/"):
            self._serve_custom_delete(unquote(path[len("/api/custom/"):]))
            return
        if path.startswith("/api/keys/"):
            self._serve_keys_revoke(unquote(path[len("/api/keys/"):]))
            return
        if path.startswith("/api/groups/"):
            self._serve_group_delete(unquote(path[len("/api/groups/"):]))
            return
        self._json(404, {"error": {"message": "未知的删除端点: %s" % path}})

    def _serve_models_list(self, head_only: bool = False):
        """OpenAI 兼容的模型清单：默认渠道 + 显式接入的额外渠道合表。"""
        payload = json.dumps({
            "object": "list",
            "data": self._model_catalog(),
            "generated_at": int(time.time()),
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _serve_health(self, head_only: bool = False, probe_now: bool = False):
        if probe_now:
            probe_upstream()
        data = {
            "status": "ok",
            "service": "hub-neo-gateway",
            "port": PORT,
            "version": VERSION,
            "upstream": UPSTREAM,
            "upstream_role": upstream_role(),
            "model_upstream": MODEL_UPSTREAM,
            "model_upstreams": MODEL_ROUTER.summary(),
            "model_token_present": bool(MODEL_TOKEN),
            "ledger_mode": LEDGER_MODE,
            "ledger_channel": LEDGER_CHANNEL if LEDGER_MODE == "local" else None,
            "ledger_backend": LEDGER.backend,
            "ledger_target": LEDGER.target,
            "upstream_cloud": UPSTREAM_CLOUD,
            "upstream_local": UPSTREAM_LOCAL,
            "upstream_health": upstream_health_snapshot(),
            "upstream_token_present": bool(upstream_token()),
            "local_token_present": bool(LOCAL_TOKEN),
            "token_present": bool(TOKEN),
            "ui": "/",
            "write_endpoints": len(POST_WHITELIST) + 1,
            "local_write_endpoints": sorted(LOCAL_WRITE_ROUTES),
            "control": {"path": str(CONTROL_PATH), "exists": CONTROL.exists(),
                        "channel_enabled": CONTROL.channel_enabled(LEDGER_CHANNEL),
                        "disabled_models": len(CONTROL.disabled_models(LEDGER_CHANNEL)),
                        "acked_errors": len(CONTROL.acked_errors()),
                        "egress": CONTROL.egress_payload(),
                        "budget": budget_health(LEDGER_CHANNEL),
                        "routing": CONTROL.routing_policy()},
            "probe_path": PROBE_PATH,
            "pricing": LEDGER.pricing_summary(),
            "model_groups": model_groups_health(),
            "prompt_opt": {
                "mode": PROMPT_OPTIMIZER.mode,
                "refiner_model": PROMPT_OPTIMIZER.refiner_model,
                "timeout_s": PROMPT_OPTIMIZER.timeout,
            },
            "stream_heal": {
                "sse": SSE_HEAL_MODE,
                "tool_guard": TOOL_GUARD_MODE,
                "stats": sse_heal.stats(),
            },
            "context_fold": {
                "mode": CONTEXT_FOLD_MODE,
                "trigger_tokens": context_fold.TRIGGER_TOKENS,
                "keep_head": context_fold.KEEP_HEAD,
                "keep_tail": context_fold.KEEP_TAIL,
                "stats": context_fold.stats(),
            },
            "rate_limit": {
                "enabled": RATE_LIMITER.enabled,
                "rps": RATE_LIMITER.rate,
                "burst": RATE_LIMITER.burst,
            },
            "conn_pool": {
                "max_per_host": GLOBAL_CONN_POOL.max_per_host,
                "idle_timeout_s": GLOBAL_CONN_POOL.max_idle_seconds,
            },
            "security_headers": [h[0] for h in hardening.SECURITY_HEADERS],
            "admin_auth": {
                "configured": bool(adminauth.ADMIN_PASS_HASH),
                "token_present": bool(TOKEN or adminauth.ADMIN_TOKEN),
                "sessions": adminauth.session_count(),
            },
        }
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _access_log(self, status, upstream_status, started, nbytes, req_id, client_ip):
        """P0-2(审计): JSON 结构化 Access Log(宪法 L2 契约字段)。"""
        try:
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "req_id": req_id or "",
                "client_ip": client_ip or "",
                "method": self.command,
                "path": urlsplit(self.path).path,
                "status": status,
                "upstream_status": upstream_status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "upstream_latency_ms": None,
                "bytes": nbytes,
            }
            sys.stderr.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:                                # noqa: BLE001
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[HUB NEO {PORT}] " + (fmt % args) + "\n")


def _create_server(port: int) -> SingleInstanceHTTPServer:
    return create_server(port, HubNeoHandler)


def _migrate_account_creds() -> None:
    """启动即把历史明文账号凭据重加密：一次、幂等，失败只告警不拦启动。"""
    try:
        result = accounts.migrate_plaintext_creds(LEDGER)
    except Exception as exc:                       # noqa: BLE001 迁移失败不该拦住网关
        print(f"[HUB NEO] 账号凭据迁移跳过: {exc}", file=sys.stderr, flush=True)
        return
    if result.get("migrated"):
        print("[HUB NEO] 账号凭据已重加密: %d 行" % len(result["migrated"]), flush=True)
    if result.get("failed"):
        print("[HUB NEO] 账号凭据重加密失败行: %s" % result["failed"],
              file=sys.stderr, flush=True)


def run(port: int = PORT, on_ready=None):
    """启动监听。``on_ready(server)`` 在 serve_forever 之前回调，
    供桌面窗口模式拿到 server 句柄做优雅停机。"""
    if not is_port_allowed(port):
        raise ValueError(f"Port {port} is not allowed by HUB NEO security policy")
    server = _create_server(port)
    _migrate_account_creds()
    print(f"[HUB NEO] Gateway listening on http://127.0.0.1:{port}/ "
          f"(Upstream: {UPSTREAM}, fallback: {UPSTREAM_LOCAL})", flush=True)
    server.daemon_threads = True
    start_upstream_probe()
    if on_ready:
        on_ready(server)
    # P0-4(审计): 优雅停机——SIGTERM/SIGINT 停探针、排空 in-flight 再关监听
    def _graceful(signum, frame):                  # noqa: ARG001
        threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except (ValueError, OSError):                # 非主线程(测试)场景降级
        pass
    try:
        server.serve_forever()
    finally:
        _PROBE_STOP.set()
        server.server_close()


if __name__ == "__main__":
    try:
        run(PORT)
    except GatewayAlreadyRunningError as exc:
        print(f"[HUB NEO] 启动中止: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
