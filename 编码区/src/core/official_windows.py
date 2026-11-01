#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/official_windows.py —— 官方分窗用量（今日 / 近 5 小时 / 本周 / 本月）。

两家官方各有一套接口，这里只做一件事：把它们的「已用 / 上限 / 重置时间」拆成四个
窗口，并向上一层层交代**缺了哪个字段**。

| 渠道 | 认证 | 端点 | 拿到的窗口 |
|---|---|---|---|
| open-design | 登录态会话 cookie（`__Secure-better-auth.session_token`） | `GET {api}/api/v1/workspaces/current` → workspaceId<br>`GET {api}/api/v1/workspaces/{ws}/billing/coding-plan-usage` → `windows[]`（`durationSeconds` / `usedCredits` / `limitCredits` / `resetsAt`）<br>`GET {api}/api/v1/billing/summary` → `todayConsumedUsd` / 余额 / 周期 | 5h、7d（官方直接给上限）→ 今日（summary 的今日消费）→ 本月（官方只给余额，不给发放额 ⇒ 如实缺） |
| ollama | API key（`Authorization: Bearer`） | `GET https://ollama.com/api/balance` → `included.allowance_usd/balance_usd/period`；legacy plan 给 `session/weekly.remaining_percent`<br>`GET https://ollama.com/api/usage?range=24h / 7d / 30d` → 时间桶 | 今日 / 5小时 / 本周 / 本月都从桶里聚合出消费；月上限用 `allowance_usd`，legacy 的 5h/周用官方 `remaining_percent` |

三条纪律：

1. **只读**：全部是 GET；不 refresh 会话、不改上游。
2. **不猜**：官方没给的字段留 `None`，并写进 `missing[]`（带端点与状态码），界面据此
   说清「缺哪个字段」，不用 0 冒充。
3. **不拖慢面板**：默认 stale-while-revalidate（先回上次结果，后台刷新）；只有显式
   `blocking=True`（登录即同步、手动刷新）才同步等结果。

ollama 官方限流 10 次/分钟（跨 KEY 与设备共享）⇒ 缓存 60s，且一次取数最多 4 个请求。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core import clock

TTL = 60.0                     # 与 /api/official/summary 的缓存同节奏
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OD_API = "https://amr-api.open-design.ai/api"
OD_ORIGIN = "https://open-design.ai"
OD_COOKIE = "__Secure-better-auth.session_token"
OLLAMA_BASE = "https://ollama.com"

WINDOWS = ("today", "h5", "week", "month")
LABELS = {"today": "今日", "h5": "近 5 小时", "week": "本周", "month": "本月"}

# 官方窗口时长（秒）→ 本仓窗口键。18000=5h，86400=日，604800=7d，2592000=30d。
OD_DURATIONS = {86400: "today", 18000: "h5", 604800: "week", 2592000: "month"}

CACHE: dict = {}
_LOCK = threading.Lock()
_INFLIGHT: dict = {}          # channel → 线程开工时间；用来识别卡死的取数线程
STALE_SECS = 45.0             # 超过这么久还没回来就认定卡住，允许重开一个线程


# ── 窗口行 ──────────────────────────────────────────────────────────────
def _row(label, *, used=None, limit=None, used_pct=None, unit="USD",
         reset_at="", basis="", requests=None, tokens=None, note="") -> dict:
    """一个窗口的统一形状；算不出百分比就留 None（不反推、不补 0）。"""
    if used_pct is None and isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        used_pct = round(max(0.0, min(100.0, used / limit * 100.0)), 1)
    remaining = None
    if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
        remaining = round(max(0.0, limit - used), 6)
    return {"label": label, "source": "official", "basis": basis,
            "used": used, "limit": limit, "remaining": remaining,
            "used_pct": used_pct, "unit": unit, "reset_at": reset_at,
            "requests": requests, "tokens": tokens, "note": note}


def _empty_missing(window: str, why: str) -> dict:
    label = LABELS.get(window) or ("全部窗口" if window == "*" else window)
    return {"window": window, "label": label, "why": why}


def _missing_field(channel: str) -> dict:
    """缺字段时说清「缺哪一个、去哪补」，而不是笼统一句「取不到」。"""
    if channel == "open-design":
        return _empty_missing("*", "缺字段 session（%s）：官方账单只认登录态 cookie，不认 sk- 开头的 "
                                  "API Key。抽屉里填一次用户名+密码（网关会现换会话），或勾选「从本机"
                                  "浏览器取会话」；两条都没有就拿不到今日/5h/周/月。" % OD_COOKIE)
    if channel == "ollama":
        return _empty_missing("*", "缺字段 api_key：官方用量端点只认 Authorization: Bearer。在抽屉的 "
                                  "TOKEN 框粘贴 ollama key（或先把它收录进自定义上游），登录一次即出四窗。")
    return _empty_missing("*", "该渠道未接入官方分窗用量")


# ── 工具 ────────────────────────────────────────────────────────────────
def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _get_json(url: str, headers: dict, timeout: float, fetch=None) -> tuple:
    """→ `(status, doc|text, error)`；HTTPError 也把响应体读回来看原因。"""
    hdrs = {"accept": "application/json", "user-agent": UA}
    hdrs.update({k: v for k, v in (headers or {}).items() if v})
    if fetch is not None:
        return fetch(url, hdrs, timeout)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(2 * 1024 * 1024)
            return int(getattr(res, "status", None) or 200), _parse(raw), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except OSError:
            body = b""
        return int(exc.code or 0), _parse(body), "HTTP %d" % int(exc.code or 0)
    except Exception as exc:                       # noqa: BLE001 探测失败按状态回
        return 0, None, str(exc) or exc.__class__.__name__


def _parse(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None


def _iso_ts(text) -> float | None:
    """官方时间串 → epoch；不带时区按 UTC（两家都是 UTC）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


# ── open-design ─────────────────────────────────────────────────────────
def _od_collect(session: str, timeout: float, fetch=None) -> dict:
    api, windows, missing, tried = OD_API, {}, [], []
    headers = {"cookie": "%s=%s" % (OD_COOKIE, session),
               "origin": OD_ORIGIN,
               "referer": OD_ORIGIN + "/cloud/dashboard"}
    st, cur, err = _get_json(api + "/v1/workspaces/current", headers, timeout, fetch)
    tried.append({"url": api + "/v1/workspaces/current", "status": st, "error": err})
    ws = str((cur or {}).get("workspaceId") or "") if isinstance(cur, dict) else ""
    if st != 200 or not ws:
        missing.append(_empty_missing("h5", "GET /v1/workspaces/current → HTTP %s%s（拿不到 workspaceId，"
                                           "5h/7d 窗口打不开）" % (st, (" " + err) if err else "")))
    else:
        hdr = dict(headers, **{"x-vela-workspace-id": ws})
        url = api + "/v1/workspaces/%s/billing/coding-plan-usage" % ws
        st2, plan, err2 = _get_json(url, hdr, timeout, fetch)
        tried.append({"url": url, "status": st2, "error": err2})
        got = {}
        # 只有 200 才算数：401/403 时响应体可能是错误壳，按它解析就是编数据。
        rows_plan = (plan.get("windows") if (st2 == 200 and isinstance(plan, dict)) else None) or []
        for row in rows_plan:
            if not isinstance(row, dict):
                continue
            key = OD_DURATIONS.get(int(_num(row.get("durationSeconds")) or 0))
            if not key:
                continue
            used = _num(row.get("usedCredits"))
            limit = _num(row.get("limitCredits"))
            if used is None and limit is None:
                continue
            got[key] = _row(LABELS[key], used=used, limit=limit,
                            unit=str(row.get("unit") or "credits"),
                            reset_at=str(row.get("resetsAt") or ""),
                            basis="coding-plan-usage")
            windows[key] = got[key]
        for key in ("h5", "week"):
            if key not in got:
                missing.append(_empty_missing(key, "coding-plan-usage 未返回该窗口"
                                                   "（HTTP %s%s）" % (st2, (" " + err2) if err2 else "")))
    st3, summ, err3 = _get_json(api + "/v1/billing/summary", headers, timeout, fetch)
    tried.append({"url": api + "/v1/billing/summary", "status": st3, "error": err3})
    summ = summ if (st3 == 200 and isinstance(summ, dict)) else {}
    today_used = _num(summ.get("todayConsumedUsd"))
    if "today" not in windows:
        if today_used is not None:
            # 官方只给「今日已花」，不给日上限 ⇒ 有数没分母，如实出用数不画百分比。
            windows["today"] = _row("今日", used=today_used, limit=None, unit="USD",
                                    basis="billing/summary.todayConsumedUsd",
                                    note="官方只给今日消费，未给日上限")
        else:
            missing.append(_empty_missing("today", "billing/summary 未给 todayConsumedUsd"
                                                   "（HTTP %s%s）" % (st3, (" " + err3) if err3 else "")))
    if "month" not in windows:
        missing.append(_empty_missing("month", "官方未公布月度发放额（billing/summary 只有 "
                                               "subscriptionCreditsUsd / rechargeCreditsUsd 余额）"))
    extra = {"balance_usd": _num(summ.get("totalAvailableCreditsUsd")),
             "subscription_usd": _num(summ.get("subscriptionCreditsUsd")),
             "recharge_usd": _num(summ.get("rechargeCreditsUsd")),
             "period_start": str(summ.get("subscriptionCurrentPeriodStart") or ""),
             "period_end": str(summ.get("subscriptionCurrentPeriodEnd") or ""),
             "tier": str((plan or {}).get("tier") if isinstance(plan, dict) else "") or ""}
    return {"ok": bool(windows), "windows": windows, "missing": missing, "tried": tried,
            "extra": extra, "auth": "session"}


# ── ollama ──────────────────────────────────────────────────────────────
def _bucket_sum(buckets, since: float, until: float | None = None) -> dict:
    """把官方时间桶按 [since, until) 聚合；缺字段留 None（legacy plan 只给请求数）。"""
    used = reqs = tokens = 0
    seen_usd = seen_req = seen_tok = False
    for row in buckets or []:
        if not isinstance(row, dict):
            continue
        start = _iso_ts(row.get("from"))
        if start is None or start < since or (until is not None and start >= until):
            continue
        usd = _num(row.get("usage_usd"))
        if usd is not None:
            used += usd
            seen_usd = True
        count = _num(row.get("request_count"))
        if count is not None:
            reqs += count
            seen_req = True
        inp = _num(row.get("input_tokens")) or 0
        out = _num(row.get("output_tokens")) or 0
        if inp or out:
            tokens += inp + out
            seen_tok = True
    return {"used": round(used, 6) if seen_usd else None,
            "requests": int(reqs) if seen_req else None,
            "tokens": int(tokens) if seen_tok else None}


def _ollama_collect(api_key: str, timeout: float, fetch=None) -> dict:
    now = time.time()
    day, week, month = clock.day_start(now), clock.week_start(now), clock.month_start(now)
    headers = {"authorization": "Bearer " + api_key}
    windows, missing, tried = {}, [], []
    st, bal, err = _get_json(OLLAMA_BASE + "/api/balance", headers, timeout, fetch)
    tried.append({"url": OLLAMA_BASE + "/api/balance", "status": st, "error": err})
    bal = bal if isinstance(bal, dict) else {}
    inc = bal.get("included") if isinstance(bal.get("included"), dict) else {}
    allow, left = _num(inc.get("allowance_usd")), _num(inc.get("balance_usd"))
    period = inc.get("period") if isinstance(inc.get("period"), dict) else {}
    if allow is not None and left is not None:
        windows["month"] = _row("本月", used=round(max(0.0, allow - left), 6), limit=allow,
                                unit="USD", reset_at=str(period.get("until") or ""),
                                basis="balance.included.allowance_usd - balance_usd")
    for key, node in (("h5", inc.get("session")), ("week", inc.get("weekly"))):
        if not isinstance(node, dict):
            continue
        left_pct = _num(node.get("remaining_percent"))
        if left_pct is None:
            continue
        windows[key] = _row(LABELS[key], used=None, limit=None,
                            used_pct=round(max(0.0, min(100.0, 100.0 - left_pct)), 1),
                            unit="%", reset_at=str(node.get("resets_at") or ""),
                            basis="balance.included.%s.remaining_percent" % ("session" if key == "h5" else "weekly"))
    ranges = {}
    for rng, key in (("24h", None), ("7d", "week"), ("30d", "month")):
        url_u = OLLAMA_BASE + "/api/usage?range=%s&scope=self" % rng
        st_u, doc, err_u = _get_json(url_u, headers, timeout, fetch)
        tried.append({"url": url_u, "status": st_u, "error": err_u})
        if st_u == 200 and isinstance(doc, dict):
            ranges[rng] = doc.get("buckets") or []
        else:
            missing.append(_empty_missing(key or "today", "/api/usage?range=%s → HTTP %s%s"
                                          % (rng, st_u, (" " + err_u) if err_u else "")))
    if "24h" in ranges:
        today_sum = _bucket_sum(ranges["24h"], day)
        h5_sum = _bucket_sum(ranges["24h"], now - 5 * 3600)
        for key, agg in (("today", today_sum), ("h5", h5_sum)):
            if agg["used"] is None and agg["requests"] is None:
                if key not in windows:
                    missing.append(_empty_missing(key, "/api/usage?range=24h 的桶里没有 usage_usd / request_count"))
                continue
            base = windows.get(key) or _row(LABELS[key], unit="USD", basis="usage.24h buckets")
            base.update({"used": agg["used"] if agg["used"] is not None else base.get("used"),
                         "requests": agg["requests"], "tokens": agg["tokens"]})
            windows[key] = base
    if "7d" in ranges and "week" not in windows:
        agg = _bucket_sum(ranges["7d"], week)
        if agg["used"] is not None or agg["requests"] is not None:
            windows["week"] = _row("本周", used=agg["used"], unit="USD",
                                   requests=agg["requests"], tokens=agg["tokens"],
                                   basis="usage.7d buckets")
    if "30d" in ranges:
        agg = _bucket_sum(ranges["30d"], month)
        base = windows.get("month")
        if base and agg["used"] is not None:
            base.update({"requests": agg["requests"], "tokens": agg["tokens"]})
        elif agg["used"] is not None or agg["requests"] is not None:
            windows["month"] = _row("本月", used=agg["used"], unit="USD",
                                    requests=agg["requests"], tokens=agg["tokens"],
                                    basis="usage.30d buckets")
    for key in WINDOWS:
        if key not in windows:
            missing.append(_empty_missing(key, "官方未给该窗口的已用/上限字段（balance 与 /api/usage 都没有）"))
    extra = {"balance_usd": left, "allowance_usd": allow,
             "purchased_usd": _num((bal.get("purchased") or {}).get("balance_usd"))
             if isinstance(bal.get("purchased"), dict) else None,
             "period_start": str(period.get("from") or ""), "period_end": str(period.get("until") or "")}
    return {"ok": bool(windows), "windows": windows, "missing": missing, "tried": tried,
            "extra": extra, "auth": "api_key"}


# ── 对外 ────────────────────────────────────────────────────────────────
def _credentials(channel: str) -> dict:
    """按渠道找官方认证：open-design 要会话 cookie，ollama 用 API key。

    会话来源顺序：凭据仓 → 本机浏览器只读探针（拿不到就如实空着，不伪造）。
    """
    out = {"session": "", "api_key": "", "why": ""}
    if channel == "open-design":
        try:
            from core import price_sync
            from core.vendor_creds import VendorCreds
            out["session"] = price_sync.session_from_creds(VendorCreds().get(channel) or {})
        except Exception:                          # noqa: BLE001
            out["session"] = ""
        if not out["session"]:
            try:
                from core import vendor_session
                out["session"] = vendor_session.session_token(
                    "amr-api.open-design.ai", (OD_COOKIE,))
                out["why"] = "会话来自本机浏览器（只读）"
            except Exception as exc:               # noqa: BLE001
                out["why"] = "没有会话：凭据仓无 session，浏览器也没取到（%s）" % str(exc)[:120]
        if not out["session"] and not out["why"]:
            out["why"] = "没有会话：在 06 渠道抽屉里登录一次（或粘贴 session cookie）"
        return out
    if channel == "ollama":
        try:
            from core.custom_upstreams import CustomUpstreamStore
            from core.vendor_creds import VendorCreds
            out["api_key"] = str((VendorCreds().get(channel) or {}).get("api_key")
                                 or (VendorCreds().get(channel) or {}).get("token") or "")
        except Exception:                          # noqa: BLE001
            out["api_key"] = ""
        # 自定义上游里的 key 由调用方（server）解封后传进来；这里不反向 import 主模块。
        if not out["api_key"]:
            out["why"] = "没有 API key：在 06 渠道抽屉里登录一次（填 token）"
        return out
    out["why"] = "该渠道官方没有分窗用量端点（或尚未接入）"
    return out


SUPPORTED = ("open-design", "ollama")


def collect(channel: str, *, blocking: bool = False, timeout: float = 4.0,
            creds: dict | None = None, fetch=None) -> dict:
    """取官方分窗用量。`blocking=False` 时先回上次结果并后台刷新。"""
    name = str(channel or "")
    if name not in SUPPORTED:
        return {"ok": False, "channel": name, "windows": {}, "pending": False,
                "missing": [_empty_missing("*", "该渠道未接入官方分窗用量")], "tried": []}
    now = time.time()
    with _LOCK:
        hit = CACHE.get(name)
    if hit and now - hit["at"] < TTL and not blocking:
        return hit["data"]
    if not blocking:
        if hit:
            # 过期先给旧值，后台补新值：面板不等网络。
            _spawn(name, timeout, creds)
            out = dict(hit["data"])
            out["stale"] = True
            return out
        _spawn(name, timeout, creds)
        return {"ok": False, "channel": name, "windows": {}, "pending": True,
                "missing": [{"window": "*", "why": "首次取数进行中"}], "tried": []}
    data = _collect_sync(name, timeout, creds, fetch)
    with _LOCK:
        CACHE[name] = {"at": time.time(), "data": data}
    return data


def _spawn(channel: str, timeout: float, creds) -> None:
    now = time.time()
    with _LOCK:
        started = _INFLIGHT.get(channel)
        if started is not None and now - started < STALE_SECS:
            return
        _INFLIGHT[channel] = now

    def run():
        data = None
        try:
            data = _collect_sync(channel, timeout, creds)
        except Exception as exc:                   # noqa: BLE001 线程里的异常必须落成读数
            data = {"ok": False, "channel": channel, "windows": {},
                    "missing": [_empty_missing("*", "取数线程异常：%s: %s"
                                               % (exc.__class__.__name__, str(exc)[:160]))],
                    "tried": [], "checked_at": int(time.time())}
        finally:
            # 无论成败都写缓存：不写就会让面板永远停在「首次取数进行中」。
            if data is None:
                data = {"ok": False, "channel": channel, "windows": {},
                        "missing": [_empty_missing("*", "取数线程没返回结果")],
                        "tried": [], "checked_at": int(time.time())}
            with _LOCK:
                CACHE[channel] = {"at": time.time(), "data": data}
                _INFLIGHT.pop(channel, None)

    threading.Thread(target=run, name="official-windows-%s" % channel, daemon=True).start()


def _collect_sync(channel: str, timeout: float, creds, fetch=None) -> dict:
    got = dict(creds or {}) or _credentials(channel)
    out = {"ok": False, "channel": channel, "windows": {}, "missing": [], "tried": [],
           "checked_at": int(time.time())}
    try:
        if channel == "open-design":
            if not got.get("session"):
                out["missing"] = [_missing_field(channel)]
                return out
            body = _od_collect(got["session"], timeout, fetch)
        else:
            if not got.get("api_key"):
                out["missing"] = [_missing_field(channel)]
                return out
            body = _ollama_collect(got["api_key"], timeout, fetch)
    except Exception as exc:                       # noqa: BLE001 官方不可达不拖垮面板
        out["missing"] = [{"window": "*", "why": "取数异常：%s" % (str(exc)[:160])}]
        return out
    out.update({"ok": body["ok"], "windows": body["windows"], "missing": body["missing"],
                "tried": body["tried"], "extra": body.get("extra") or {},
                "auth": body.get("auth") or "", "checked_at": int(time.time())})
    return out


def invalidate(channel: str = "") -> None:
    with _LOCK:
        if channel:
            CACHE.pop(str(channel), None)
        else:
            CACHE.clear()


__all__ = ["collect", "invalidate", "SUPPORTED", "WINDOWS", "LABELS", "TTL"]
