"""本地读模型：把云端 kswa 的读接口换成账本 + 渠道配置派生。

原则：本地有事实就算真数（金额、构成、错误行、渠道、价目覆盖）；
本地确实没有的（云端密钥库、官方额度快照、账号池、订阅额度）给空集合 + note，
不补 0、不伪造成功，前端按空态显示。
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs

from . import clock
from .local_control import BUDGET_WARN_PCT

CHANNEL_LABELS = {"open-design": "OpenDesign"}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso(ts) -> str:
    """CST 时间字符串；错误台账「时间」列直接读它，缺了会退回只有时分秒。"""
    try:
        return clock.iso(ts)
    except (TypeError, ValueError, OSError):
        return ""


def _today_start(now: int) -> int:
    return clock.day_start(now)


class LocalReadModel:
    """一个实例对应一份账本 + 一个渠道快照，按需重新取值。"""

    def __init__(self, ledger, *, channel="", upstream="", models=None, token="",
                 control=None):
        self.ledger = ledger
        self.channel = (channel or "").strip()
        self.upstream = (upstream or "").rstrip("/")
        self.token = token or ""
        self._models = list(models or [])
        # 本地控制面（control.json）；缺失时按全默认作答，不伪造开关。
        self.control = control

    # ── 工具 ────────────────────────────────────────────────
    def _label(self, channel: str) -> str:
        return CHANNEL_LABELS.get(channel, channel)

    def _model_ids(self) -> list:
        out = []
        for m in self._models:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if mid:
                out.append(mid)
        return out

    def _priced(self) -> list:
        table = self.ledger.pricing(self.channel)
        return [mid for mid in self._model_ids() if mid in table]

    @staticmethod
    def _cost(rows) -> float:
        return round(sum(r["cost_cny"] for r in rows if r["cost_cny"] is not None), 6)

    # ── 各接口 ──────────────────────────────────────────────
    def billing_live(self, now: int = None) -> dict:
        now = int(now or time.time())
        today_rows = self.ledger.rows(since=_today_start(now))
        ok_rows = [r for r in today_rows if r["ok"]]
        cost = self._cost(today_rows)

        # 30 个 2 分钟桶，覆盖最近 60 分钟；空桶给 0（前端画零柱）。
        buckets = [0] * 30
        recent = self.ledger.rows(since=now - 3600)
        for r in recent:
            idx = 29 - int((now - _int(r["at"])) // 120)
            if 0 <= idx < 30:
                buckets[idx] += 1
        start = now - 3600
        series = [{"t": start + i * 120, "n": buckets[i]} for i in range(30)]

        by_channel = {}
        for r in today_rows:
            key = r["channel"] or self.channel or "—"
            slot = by_channel.setdefault(key, {"channel": key, "requests": 0,
                                               "cost_cny": 0.0, "tokens": 0})
            slot["requests"] += 1
            slot["tokens"] += _int(r["total_tokens"])
            if r["cost_cny"] is not None:
                slot["cost_cny"] = round(slot["cost_cny"] + r["cost_cny"], 6)
        total_cost = cost or 0.0
        channels = sorted(by_channel.values(), key=lambda x: -x["requests"])
        for c in channels:
            c["share"] = round(c["cost_cny"] / total_cost * 100, 2) if total_cost else None

        return {
            "ok": True,
            "at": now,
            "window_minutes": 60,
            "today": {
                "requests": len(today_rows),
                "ok": len(ok_rows),
                "cost_cny": cost if today_rows else None,
                "tokens": sum(_int(r["total_tokens"]) for r in today_rows),
            },
            "avg_per_call": {
                "cny": round(cost / len(ok_rows), 6) if ok_rows and cost else None,
                "n": len(ok_rows),
            },
            "series": series,
            "channels": channels,
            "budget": None,
            "projection": None,
            "source": "local-ledger",
        }

    def errors(self, query: str = "") -> dict:
        params = parse_qs(query)
        try:
            limit = max(1, min(100, int((params.get("limit") or ["20"])[0])))
            offset = max(0, int((params.get("offset") or ["0"])[0]))
        except (TypeError, ValueError):
            limit, offset = 20, 0
        rows = [r for r in self.ledger.rows(limit=2000) if not r["ok"]]
        rows.sort(key=lambda r: -(r["at"] or 0))
        acked = self.control.acked_errors() if self.control else set()
        kinds = {}
        for r in rows:
            kinds[self._error_kind(r)] = kinds.get(self._error_kind(r), 0) + 1
        page = rows[offset:offset + limit]
        return {
            "ok": True,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "kind_counts": kinds,
            "acked_total": len(acked),
            "rows": [{
                "id": r["id"], "at": r["at"], "iso": _iso(r["at"]),
                "channel": r["channel"],
                "model": r["model"], "status": r["status"], "error": r["error"],
                "kind": self._error_kind(r), "elapsed_ms": r["elapsed_ms"],
                "req_id": r["rid"],
                # 4xx 多为配额/鉴权（可自助处理），5xx 与无状态才是真故障；给前端一个可读分级。
                "level": ("error" if (not r["status"] or int(r["status"]) >= 500)
                          else ("warn" if int(r["status"]) in (429, 402, 401, 403) else "info")),
                "acked": (r["rid"] in acked) or (str(r["id"]) in acked),
            } for r in page],
            "source": "local-ledger",
        }

    @staticmethod
    def _error_kind(row) -> str:
        if row["status"]:
            return f"HTTP {row['status']}"
        text = (row["error"] or "").strip()
        return text[:40] if text else "unknown"

    def visibility(self) -> dict:
        entry = self.control.channel_entry(self.channel) if self.control else {}
        hidden = bool(entry.get("hidden"))
        channels = [{"channel": self.channel, "label": self._label(self.channel),
                     "kind": "builtin", "hidden": hidden,
                     "enabled": bool(entry.get("enabled", True)),
                     "note": entry.get("note") or ""}] if self.channel else []
        return {
            "ok": True,
            "channels": channels,
            "hidden_channels": [c["channel"] for c in channels if c["hidden"]],
            "path": str(self.control.path) if self.control else None,
            "exists": bool(self.control and self.control.exists()),
            "source": "local-control",
            "note": "渠道启停写 control.json（本地面板），不反代云端",
        }

    def channels(self, now: int = None) -> dict:
        now = int(now or time.time())
        today = [r for r in self.ledger.rows(since=_today_start(now))]
        priced = self._priced()
        models = self._model_ids()
        items = []
        if self.channel:
            rows = [r for r in today if (r["channel"] or self.channel) == self.channel]
            items.append({
                "channel": self.channel,
                "display": self._label(self.channel),
                "kind": "builtin",
                "enabled": bool(self.control.channel_entry(self.channel).get("enabled", True))
                           if self.control else True,
                "hidden": bool(self.control.channel_entry(self.channel).get("hidden"))
                          if self.control else False,
                "realms": ["intl"],
                "models": len(models),
                "priced": len(priced),
                "unpriced": [m for m in models if m not in priced],
                "accounts": 0,
                "pricing_mode": "mtok" if priced else "unpriced",
                "today": {
                    "requests": len(rows),
                    "cost_cny": self._cost(rows) if rows else None,
                    "tokens": sum(_int(r["total_tokens"]) for r in rows),
                },
            })
        return {
            "ok": True,
            "generated_at": now,
            "channels": items,
            "filters": {"realms": ["intl"], "kinds": ["builtin"]},
            "source": "local-config",
        }

    def custom(self) -> dict:
        model_ids = self._model_ids()
        data = []
        if self.channel:
            data.append({
                "name": self.channel,
                "label": self._label(self.channel),
                "base_url": self.upstream,
                "models": model_ids,
                "enabled": bool(self.control.channel_entry(self.channel).get("enabled", True))
                           if self.control else True,
                "has_key": bool(self.token),
                "key_masked": ("Bearer " + self.token[:6] + "…" + self.token[-4:]) if self.token else "",
            })
        return {"ok": True, "data": data, "items": data,
                "chat_url_hint": self.upstream, "source": "local-config"}

    def models_meta(self) -> dict:
        model_ids = self._model_ids()
        priced = self._priced()
        return {
            "ok": True,
            "channels": {self.channel: {"label": self._label(self.channel),
                                        "models": len(model_ids),
                                        "priced": len(priced)}} if self.channel else {},
            "custom": [],
            "source": "local-config",
        }

    def security_audit(self, now: int = None) -> dict:
        now = int(now or time.time())
        findings = []
        if not self.token:
            findings.append({"id": "upstream-token", "level": "high",
                             "title": "模型上游未配置 Bearer token",
                             "where": "HUB_MODEL_TOKEN"})
        unpriced = [r for r in self.ledger.rows(limit=2000)
                    if r["ok"] and r["cost_cny"] is None]
        if unpriced:
            findings.append({"id": "unpriced", "level": "medium",
                             "title": f"{len(unpriced)} 条成功行价目缺失",
                             "where": "运维区/configs/pricing.json"})
        score = max(0, 100 - sum(30 if f["level"] == "high" else 12 for f in findings))
        return {
            "ok": True,
            "at": now,
            "iso": clock.iso_t(now),
            "score": score,
            "level": "PASS" if score >= 90 else ("WARN" if score >= 70 else "FAIL"),
            "findings": findings,
            "stats": {"keys": 1 if self.token else 0, "crypto": "env",
                      "channels": 1 if self.channel else 0,
                      "ledger": self.ledger.backend},
            "source": "local-audit",
        }

    def cloud_sync(self, now: int = None) -> dict:
        now = int(now or time.time())
        return {"ok": True, "enabled": False, "base": "local-ledger",
                "at": now, "runs": [], "error": None, "source": "local-ledger"}

    def pool(self) -> dict:
        egress = self.control.egress_payload() if self.control else {
            "proxy_url": "", "proxy_enabled": False}
        return {"ok": True, "accounts": [], "channels": [], "egress": egress,
                "source": "local",
                "note": "本地直连上游，无账号池；egress 来自 control.json"}

    def official_summary(self, query: str = "") -> dict:
        params = parse_qs(query)
        try:
            days = max(1, min(30, int((params.get("days") or ["1"])[0])))
        except (TypeError, ValueError):
            days = 1
        return {"ok": True, "days": days, "channels": [], "fetched_at": int(time.time()),
                "cached": False, "source": "local",
                "note": "本地模式无 workbuddy 官方额度渠道"}

    def budgets(self, now: int = None) -> dict:
        """上限来自 control.json，spent_cny 是今日账本实际支出（不混两者）。"""
        now = int(now or time.time())
        if not self.control:
            return {"ok": True, "global": None, "items": [], "source": "local-ledger",
                    "note": "未挂本地控制面"}
        state = self.control.snapshot()
        total, by_channel = self.ledger.spend_since(_today_start(now))
        spend = {(key or self.channel): value for key, value in by_channel.items()}

        def used(amount, spent):
            return round(spent / amount * 100, 1) if amount else None

        def over(amount, spent):
            # 与派发闸门同一个判据：支出 ≥ 上限即已满（0 = 硬停，不是取消上限）。
            return bool(amount is not None and spent >= amount)

        def near(pct, full):
            # 预警线与闸门同源：到 80% 先报，到 100% 就是拦截，不重复报。
            return bool(pct is not None and pct >= BUDGET_WARN_PCT and not full)

        glob = dict(state["budgets"]["global"])
        glob["spent_cny"] = total
        glob["used_pct"] = used(glob.get("amount"), total)
        glob["over"] = over(glob.get("amount"), total)
        glob["warn"] = near(glob["used_pct"], glob["over"])
        items = []
        for item in state["budgets"]["items"]:
            row = dict(item)
            row["spent_cny"] = spend.get(item["key"], 0.0)
            row["used_pct"] = used(item.get("amount"), row["spent_cny"])
            row["over"] = over(item.get("amount"), row["spent_cny"])
            row["warn"] = near(row["used_pct"], row["over"])
            items.append(row)
        return {"ok": True, "global": glob, "items": items, "generated_at": now,
                "source": "local-control", "path": str(self.control.path),
                "enforced": True, "period": "day", "warn_pct": BUDGET_WARN_PCT,
                "note": ("上限由本地面板写入；spent_cny 为今日账本实际支出；"
                         "over=True 即 /v1 新请求已被拒（402 budget_exceeded）")}

    def subscription(self) -> dict:
        return {"ok": True, "upstream": self.upstream, "tier": None, "now": int(time.time()),
                "snapshot": None, "snapshot_path": None, "window": None,
                "models": len(self._model_ids()), "allowance_usd": None,
                "source": "local", "note": "上游订阅额度不由本网关持有"}

    def accounts_health(self) -> dict:
        return {"ok": True, "accounts": [], "generated_at": int(time.time()),
                "source": "local", "note": "本地模式无账号签到体系"}

    # ── 路由 ────────────────────────────────────────────────
    def payload(self, path: str, query: str = ""):
        if path == "/api/billing/live":
            return self.billing_live()
        if path == "/api/errors":
            return self.errors(query)
        if path == "/api/visibility":
            return self.visibility()
        if path == "/api/readmodel/channels":
            return self.channels()
        if path == "/api/custom":
            return self.custom()
        if path == "/api/models/meta":
            return self.models_meta()
        if path == "/api/security/audit":
            return self.security_audit()
        if path == "/api/cloud/sync":
            return self.cloud_sync()
        if path == "/api/pool":
            return self.pool()
        if path == "/api/official/summary":
            return self.official_summary(query)
        if path == "/api/budgets":
            return self.budgets()
        if path == "/api/th/subscription":
            return self.subscription()
        if path == "/api/accounts/health":
            return self.accounts_health()
        if path == "/api/channels/overview":
            return self.channels_overview()
        if path == "/api/keys":
            return []
        return None



def channels_overview(*, ledger, control=None, router=None, accounts_mod=None,
                      creds=None, quota_map=None, reconcile=None, now=None) -> dict:
    """06 渠道管理单入口读模型：渠道开关 + 路由/自定义头 + 账号池 + 双轨额度。

    分界铁律：`official` 是**官方源**额度（带 source 印章），`gateway` 是**本机 8888**
    的今日预算闸门，两者并列但绝不合流成一个百分比。
    """
    now = int(now or time.time())
    since = _today_start(now)
    try:
        total_spent, spend_by_channel = ledger.spend_since(since)
    except Exception:                       # noqa: BLE001 账本不可读时不编支出
        total_spent, spend_by_channel = 0.0, {}
    quota_map = quota_map if isinstance(quota_map, dict) else {}
    reconcile = reconcile if isinstance(reconcile, dict) else {}
    router_channels = {}
    if router is not None:
        try:
            router_channels = {row.get("channel"): row for row in router.summary().get("channels") or []}
        except Exception:                   # noqa: BLE001 路由表坏了不影响其它块
            router_channels = {}
    creds_rows = {}
    if creds is not None:
        try:
            creds_rows = {row["channel"]: row for row in creds.channels()}
        except Exception:                   # noqa: BLE001 保险仓不可读时按未登录呈现
            creds_rows = {}

    names = []
    for src in (router_channels, quota_map, creds_rows):
        for name in src:
            if name and name not in names:
                names.append(name)

    items = []
    for name in names:
        entry = (control.channel_entry(name) if control else {}) or {}
        route = router_channels.get(name) or {}
        quota = quota_map.get(name) or {}
        acc_rows = []
        if accounts_mod is not None:
            try:
                acc_rows = accounts_mod.list_accounts(ledger, name)
            except Exception:               # noqa: BLE001 账号表不可读就空着，不误报成功
                acc_rows = []
        checked_today = sum(1 for a in acc_rows if (a.get("last_checkin") or 0) >= since)
        spent_channel = float((spend_by_channel or {}).get(name) or 0.0)
        gate = (control.budget_gate(name, total_spent, spent_channel) if control
                else {"key": "", "cap_cny": None, "spent_cny": spent_channel,
                      "used_pct": None, "warn": False, "blocked": False})
        items.append({
            "channel": name,
            "label": CHANNEL_LABELS.get(name, name),
            "enabled": bool(entry.get("enabled", True)),
            "hidden": bool(entry.get("hidden")),
            "note": entry.get("note") or "",
            "base_url": route.get("base_url") or "",
            "endpoint": route.get("endpoint") or "/v1/chat/completions",
            "decision": bool(route.get("decision")),
            "models": list(route.get("models") or []),
            "headers": dict(route.get("headers") or {}),
            "header_names": list(route.get("header_names") or []),
            "egress": (control.egress_payload(name) if control else {}),
            "official": quota,
            "official_source": quota.get("source") or "none",
            "gateway": gate,
            "accounts": {
                "total": len(acc_rows),
                "active": sum(1 for a in acc_rows if a.get("status") == "active"),
                "need_refresh": sum(1 for a in acc_rows if a.get("status") == "need_refresh"),
                "checked_in_today": checked_today,
                "credits": round(sum(float(a.get("credits") or 0.0) for a in acc_rows), 4),
                "rows": acc_rows,
            },
            "creds": creds_rows.get(name) or {},
            "reconcile": reconcile.get(name) or {},
        })

    mix = {}
    for item in items:
        mix[item["official_source"]] = mix.get(item["official_source"], 0) + 1
    return {
        "ok": True,
        "generated_at": now,
        "channels": items,
        "summary": {
            "channels": len(items),
            "enabled": sum(1 for i in items if i["enabled"] and not i["hidden"]),
            "official_sources": mix,
            "accounts": sum(i["accounts"]["total"] for i in items),
            "checked_in_today": sum(i["accounts"]["checked_in_today"] for i in items),
            "local_spent_cny": round(float(total_spent or 0.0), 6),
            "headers": sum(len(i["header_names"]) for i in items),
        },
        "source": "local-readmodel",
        "note": "官方源额度与本地 8888 预算分属两条轴，不合流；无数据如实呈 none",
    }


READ_ROUTES = frozenset({
    "/api/billing/live", "/api/errors", "/api/visibility",
    "/api/readmodel/channels", "/api/custom", "/api/models/meta",
    "/api/security/audit", "/api/cloud/sync", "/api/pool",
    "/api/official/summary", "/api/budgets", "/api/th/subscription",
    "/api/accounts/health", "/api/keys", "/api/channels/overview",
    "/api/reports/today",
})
