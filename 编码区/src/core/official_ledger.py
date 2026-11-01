#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/official_ledger.py —— 官方额度/官方用量 vs 本地账本的差额对账。

原则：**只对比同口径的数**。
- 官方给的是「请求数/调用量」→ 可以跟本地账本的请求数直接算差额；
- 官方给的是「点数/积分/余额」→ 与本地的人民币账本不同单位，就只并列呈现、
  `comparable=false` 并写明原因，绝不硬算一个假差额。
- 结果 60s 内存缓存，面板刷新不打爆官方端点。
"""

from __future__ import annotations

import threading
import time

from core import clock

CACHE_TTL = 60
# 官方用量能直接和本地「请求数」对上的单位
REQUEST_UNITS = frozenset({"", "requests", "calls", "count", "times"})


def today_start(now=None) -> float:
    """对账「今日」起点：与账本同源，按 CST 00:00 切（不用宿主机时区）。"""
    return float(clock.day_start(now))


def local_metrics(ledger, channel: str = "", since=None) -> dict:
    """本地账本口径：今日（或指定起点）请求数 / 花费 / token。"""
    since = today_start() if since is None else float(since)
    rows = []
    try:
        rows = ledger.rows(since=since) or []
    except Exception:                      # noqa: BLE001 账本不可读时如实回零，不伪造
        rows = []
    channel = str(channel or "").strip()
    requests = spent = tokens = 0
    for row in rows:
        if channel and str(row.get("channel") or "") != channel:
            continue
        requests += 1
        spent += float(row.get("cost_cny") or 0.0)
        tokens += int(row.get("total_tokens") or 0)
    return {"requests": requests, "spent_cny": round(spent, 6), "tokens": tokens,
            "since": int(since)}


def compare(channel: str, official: dict, local: dict) -> dict:
    """官方 vs 本地：同口径才出差额，否则只并列并写明不可比原因。"""
    official = official if isinstance(official, dict) else {}
    local = local if isinstance(local, dict) else {}
    used = official.get("used")
    unit = str(official.get("unit") or "").strip().lower()
    local_requests = int(local.get("requests") or 0)
    comparable = isinstance(used, (int, float)) and unit in REQUEST_UNITS
    delta = int(used - local_requests) if comparable else None
    if used is None:
        # 一层都没取到官方数（source=none/manual 空）时无对价可算：如实说没有，
        # 不能套「单位不同」——那等于暗示手里有一份官方点数，属于编数。
        verdict, note = "no_official", "未取到官方额度（无 live/manual 来源），没有可对比的官方数"
    elif not comparable:
        verdict, note = "units_differ", "官方口径为点数/余额，与本地请求数不同单位，不做差额运算"
    elif delta == 0:
        verdict, note = "match", "官方与本地调用量一致"
    elif delta > 0:
        verdict, note = "official_ahead", "官方计数高于本地账本：可能有别处调用同一账号"
    else:
        verdict, note = "local_ahead", "本地计数高于官方：官方计数有延迟或未计费请求"
    return {
        "channel": str(channel or ""),
        "comparable": comparable,
        "official_used": used,
        "official_unit": official.get("unit") or "",
        "official_source": official.get("source") or "none",
        "local_requests": local_requests,
        "local_spent_cny": local.get("spent_cny") or 0.0,
        "delta_requests": delta,
        "verdict": verdict,
        "note": note,
        "at": int(time.time()),
    }


class OfficialLedger:
    """对账门面：按渠道缓存「官方额度 + 差额」结果 60 秒。"""

    def __init__(self, ledger, ttl: float = CACHE_TTL):
        self.ledger = ledger
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def invalidate(self, channel: str = "") -> None:
        with self._lock:
            if channel:
                self._cache.pop(str(channel), None)
            else:
                self._cache.clear()

    def entry(self, channel: str, quota: dict) -> dict:
        """单个渠道：官方额度 + 本地口径 + 差额（带缓存）。"""
        name = str(channel or "")
        now = time.time()
        # 分窗还在后台取数时不能缓存：否则「首次取数进行中」会被冻结 60 秒，
        # 而官方数字其实一两秒就到了。已缓存的 pending 行也一律当未命中。
        pending = bool((quota or {}).get("windows_pending"))
        with self._lock:
            hit = self._cache.get(name)
            if (hit and not pending and now - hit[0] < self.ttl
                    and not (hit[1].get("official") or {}).get("windows_pending")):
                return hit[1]
        local = local_metrics(self.ledger, name)
        payload = {
            "channel": name,
            "official": quota,
            "local": local,
            "reconcile": compare(name, quota, local),
        }
        with self._lock:
            self._cache[name] = (now, payload)
        return payload

    def summary(self, quotas: dict) -> dict:
        """一批渠道的对账汇总（`quotas`: {channel: quota dict}）。"""
        rows = [self.entry(ch, q) for ch, q in (quotas or {}).items()]
        ahead = [r["channel"] for r in rows if r["reconcile"]["verdict"] == "official_ahead"]
        return {
            "channels": rows,
            "total": len(rows),
            "official_ahead": ahead,
            "comparable": sum(1 for r in rows if r["reconcile"]["comparable"]),
            "source_mix": _mix(rows),
            "generated_at": int(time.time()),
            "note": "差值为空表示官方与本地口径不同（点数/余额 vs 请求数），不硬算",
        }


def _mix(rows: list) -> dict:
    mix = {}
    for row in rows:
        source = str((row.get("official") or {}).get("source") or "none")
        mix[source] = mix.get(source, 0) + 1
    return mix


__all__ = ["OfficialLedger", "compare", "local_metrics", "today_start",
           "CACHE_TTL", "REQUEST_UNITS"]
