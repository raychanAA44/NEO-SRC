#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/quota.py —— 官方渠道额度三级降级引擎。

层级：`live`（官方端点现取）→ `manual`（面板手工快照）→ `doc`（文档口径）→ `none`（如实没有）。
规矩：
- 任何一层都拿不到时回 `none`，**绝不用猜测/默认值冒充额度**；
- 每一层都带 `source`，前端据此打 `LIVE` / `MANUAL` / `DOC` 印章；
- 只用标准库 `urllib`，超时短（默认 6s），失败立刻降级，不拖慢面板。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE_LIVE = "live"
SOURCE_MANUAL = "manual"
SOURCE_DOC = "doc"
SOURCE_NONE = "none"
SOURCES = (SOURCE_LIVE, SOURCE_MANUAL, SOURCE_DOC, SOURCE_NONE)

# 官方额度不一定有统一 API：按常见路径挨个试，命中即用。
PROBE_PATHS = ("/api/usage", "/v1/usage", "/api/v1/usage",
               "/api/quota", "/user", "/auth/v1/user")
# 官方可能把额度按窗口分开报（日 / 5 小时 / 周 / 月）。
WINDOW_ALIASES = {
    "today": ("today", "daily", "day", "today_usage"),
    "h5": ("5h", "5_hour", "five_hour", "five_hours", "session", "rolling_5h"),
    "week": ("week", "weekly", "week_usage"),
    "month": ("month", "monthly", "month_usage"),
}
WINDOW_LABELS = {"today": "今日", "h5": "近 5 小时", "week": "本周", "month": "本月"}

USED_KEYS = ("used", "used_points", "used_credits", "spent", "consumed", "usage")
LIMIT_KEYS = ("limit", "total", "quota", "total_points", "credits_total", "monthly_limit")
LEFT_KEYS = ("remaining", "remain", "balance", "left", "available", "credits")
UNIT_KEYS = ("unit", "currency", "credits_unit")

from core.paths import DATA_DIR
SNAPSHOT_PATH = DATA_DIR / "quota_snapshots.json"


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_num(doc: dict, keys) -> float | None:
    for key in keys:
        if key in doc:
            got = _num(doc.get(key))
            if got is not None:
                return got
    return None


def normalize(channel: str, *, source: str, used=None, limit=None, remaining=None,
              unit: str = "", reset_at=None, expires_at=None, note: str = "",
              raw: dict | None = None) -> dict:
    """统一额度形状：所有来源都必须走这里，前端只认这一份字段。"""
    used_v, limit_v, left_v = _num(used), _num(limit), _num(remaining)
    if left_v is None and limit_v is not None and used_v is not None:
        left_v = max(0.0, limit_v - used_v)
    if used_v is None and limit_v is not None and left_v is not None:
        used_v = max(0.0, limit_v - left_v)
    pct = None
    if limit_v and limit_v > 0 and used_v is not None:
        pct = round(max(0.0, min(100.0, used_v / limit_v * 100.0)), 1)
    elif limit_v and limit_v > 0 and left_v is not None:
        pct = round(max(0.0, min(100.0, (limit_v - left_v) / limit_v * 100.0)), 1)
    return {
        "channel": str(channel or ""),
        "source": source if source in SOURCES else SOURCE_NONE,
        "used": used_v, "limit": limit_v, "remaining": left_v,
        "used_pct": pct,
        "unit": str(unit or ""),
        "reset_at": str(reset_at or ""),
        "expires_at": str(expires_at or ""),
        "note": str(note or ""),
        "raw": raw if isinstance(raw, dict) else {},
        "windows": {},
        "at": int(time.time()),
    }


def _extract_windows(channel: str, body: dict) -> dict:
    """从官方应答里抽分窗额度；没报的窗口不造，保持空。"""
    out = {}
    if not isinstance(body, dict):
        return out
    for label, aliases in WINDOW_ALIASES.items():
        for name in aliases:
            node = body.get(name)
            if not isinstance(node, dict):
                continue
            used = _first_num(node, USED_KEYS)
            limit = _first_num(node, LIMIT_KEYS)
            left = _first_num(node, LEFT_KEYS)
            if used is None and limit is None and left is None:
                continue
            row = normalize(channel, source=SOURCE_LIVE, used=used, limit=limit, remaining=left,
                            unit=str(node.get("unit") or ""),
                            reset_at=node.get("reset_at") or node.get("resets_at") or "")
            out[label] = {k: row[k] for k in ("used", "limit", "remaining", "used_pct",
                                             "unit", "reset_at", "source")}
            out[label]["window"] = label
            break
    return out


def _extract(channel: str, doc: dict) -> dict | None:
    """从官方应答里抽额度；抽不到返回 None（不编数字）。"""
    if not isinstance(doc, dict):
        return None
    body = doc
    for key in ("data", "result", "usage", "quota", "billing"):
        if isinstance(doc.get(key), dict):
            body = doc[key]
            break
    used = _first_num(body, USED_KEYS)
    limit = _first_num(body, LIMIT_KEYS)
    left = _first_num(body, LEFT_KEYS)
    if used is None and limit is None and left is None:
        return None
    unit = str(body.get("unit") or body.get("currency") or "")
    for key in UNIT_KEYS:
        if body.get(key):
            unit = str(body.get(key))
            break
    row = normalize(channel, source=SOURCE_LIVE, used=used, limit=limit, remaining=left,
                    unit=unit, reset_at=body.get("reset_at") or body.get("resets_at") or "",
                    expires_at=body.get("expires_at") or body.get("expire_at") or "",
                    note=str(body.get("plan") or body.get("tier") or ""), raw=body)
    row["windows"] = _extract_windows(channel, body)
    return row


def probe_live(channel: str, base_url: str, token: str = "", headers: dict | None = None,
               timeout: float = 6.0, opener=None) -> dict | None:
    """官方端点现取：逐个候选路径试，命中即回；全失败回 None（交给下一级）。"""
    base = str(base_url or "").rstrip("/")
    if not base:
        return None
    send = opener or urllib.request.urlopen
    for path in PROBE_PATHS:
        req = urllib.request.Request(base + path, method="GET")
        req.add_header("accept", "application/json")
        if token:
            req.add_header("authorization", "Bearer " + str(token))
        for name, value in (headers or {}).items():
            req.add_header(str(name), str(value))
        try:
            with send(req, timeout=timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            continue
        got = _extract(channel, doc)
        if got:
            got["probe_path"] = path
            return got
    return None


def fetch_channel_quota(channel: str, *, base_url: str = "", token: str = "",
                        headers: dict | None = None, manual: dict | None = None,
                        doc: dict | None = None, timeout: float = 6.0,
                        fetcher=None) -> dict:
    """三级降级：live → manual → doc → none。`fetcher` 供单测注入假官方应答。"""
    live = None
    try:
        if fetcher is not None:
            live = fetcher(channel, base_url, token, headers or {})
            if isinstance(live, dict) and live.get("source") is None:
                live = normalize(channel, source=SOURCE_LIVE, **live)
        elif base_url:
            live = probe_live(channel, base_url, token, headers or {}, timeout=timeout)
    except Exception:                     # noqa: BLE001 官方不可达不该让面板 500
        live = None
    if live:
        return live
    if isinstance(manual, dict) and any(manual.get(k) is not None
                                       for k in ("used", "limit", "remaining")):
        return normalize(channel, source=SOURCE_MANUAL,
                         note=manual.get("note") or "面板手工快照", **{
                             k: manual.get(k) for k in
                             ("used", "limit", "remaining", "unit", "reset_at", "expires_at")})
    if isinstance(doc, dict) and any(doc.get(k) is not None
                                    for k in ("used", "limit", "remaining")):
        return normalize(channel, source=SOURCE_DOC,
                         note=doc.get("note") or "文档口径兜底", **{
                             k: doc.get(k) for k in
                             ("used", "limit", "remaining", "unit", "reset_at", "expires_at")})
    return normalize(channel, source=SOURCE_NONE, note="官方额度不可达且无手工快照")


# ── 手工快照存储（manual 层的数据源） ─────────────────────────
def load_snapshots(path=None) -> dict:
    target = Path(path or os.environ.get("HUB_QUOTA_SNAPSHOTS") or SNAPSHOT_PATH)
    if not target.is_file():
        return {}
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    channels = doc.get("channels") if isinstance(doc, dict) else None
    return channels if isinstance(channels, dict) else {}


def save_snapshot(channel: str, payload: dict, path=None) -> dict:
    """手工录入/登录抓取后的快照落盘（manual 层用；原子写）。"""
    name = str(channel or "").strip()
    if not name:
        raise ValueError("channel 必填")
    target = Path(path or os.environ.get("HUB_QUOTA_SNAPSHOTS") or SNAPSHOT_PATH)
    doc = {"schema": 1, "channels": load_snapshots(target)}
    row = {k: payload.get(k) for k in ("used", "limit", "remaining", "unit",
                                       "reset_at", "expires_at", "note")}
    row["at"] = int(time.time())
    doc["channels"][name] = row
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return {"channel": name, **row}


def source_label(source: str) -> str:
    return {SOURCE_LIVE: "LIVE", SOURCE_MANUAL: "MANUAL",
            SOURCE_DOC: "DOC", SOURCE_NONE: "NONE"}.get(str(source or ""), "NONE")


__all__ = ["SOURCE_LIVE", "SOURCE_MANUAL", "SOURCE_DOC", "SOURCE_NONE", "SOURCES",
           "normalize", "probe_live", "fetch_channel_quota", "load_snapshots",
           "save_snapshot", "source_label", "SNAPSHOT_PATH"]
