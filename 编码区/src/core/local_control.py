"""本地控制面：渠道启停、模型开关、预算上限落本地配置文件。

原则：写操作只改 8888 自己的 `control.json`（临时文件 + os.replace 原子替换），
读侧（visibility / channels / catalog / budgets）与派发闸门同源读取，
不再反代云端 kswa。文件缺失 = 全默认（渠道开、模型跟随上游、预算无硬顶）。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

BUDGET_UNIT = "CNY"
BUDGET_PERIOD = "day"
# 占用率预警线：到这个比例先报，而不是等撞上限才知道。
BUDGET_WARN_PCT = 80
MAX_ACKED = 500
PROXY_SCHEMES = ("http://", "https://")
# 路由策略缺省值：超时沿用代理链既有 180s（流式长任务），重试 0 次不改变现网行为，
# 粘性锁沿用 TaskLockStore 的 12h，限流沿用 RateLimiter 的 100 req/s。
ROUTING_DEFAULTS = {
    "timeout_s": 180,
    "max_retries": 0,
    "sticky_ttl_s": 43200,
    "rate_limit_rps": 100,
    "rate_limit_burst": 150,
}
ROUTING_LIMITS = {
    "timeout_s": (1, 600),
    "max_retries": (0, 3),
    "sticky_ttl_s": (30, 86400),
    "rate_limit_rps": (1, 10000),
    "rate_limit_burst": (1, 100000),
}
PROMPT_MODES = ("off", "shadow", "enforce")


def _amount(value):
    """预算金额：None / "" = 无硬顶；负数或非数字一律拒。"""
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("预算金额必须是数字或 null") from exc
    if num < 0:
        raise ValueError("预算金额不能为负")
    # 与账本 `cost_cny` 同精度（6 位）：4 位在分级卢布的花费量级上会把上限归零或跳过预警线。
    return round(num, 6)


class LocalControl:
    """一份 control.json：渠道开关 + 模型开关 + 预算上限，按 mtime 自动重读。"""

    def __init__(self, path, *, default_channel="", clock=time.time):
        self.path = Path(path)
        self.default_channel = (default_channel or "").strip()
        self._clock = clock
        self._lock = threading.RLock()
        self._state = None
        self._stamp = None

    # ── 存储 ────────────────────────────────────────────────
    def _defaults(self) -> dict:
        state = {
            "version": 1,
            "updated_at": 0,
            "channels": {},
            "models": {},
            "suspended": {},
            "errors": {"acked": []},
            "egress": {"proxy_url": "", "channel_overrides": {}, "updated_at": 0},
            "budgets": {"global": {"amount": None, "unit": BUDGET_UNIT,
                                   "period": BUDGET_PERIOD}, "items": []},
            "routing": dict(ROUTING_DEFAULTS),
            "prompt_opt": {"mode": "", "updated_at": 0},
        }
        if self.default_channel:
            state["channels"][self.default_channel] = {
                "enabled": True, "hidden": False, "note": "默认渠道（HUB_MODEL_CHANNEL）"}
            state["budgets"]["items"].append({
                "key": self.default_channel, "label": self.default_channel,
                "amount": None, "unit": BUDGET_UNIT, "period": BUDGET_PERIOD})
        return state

    def _stamp_of(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _normalize(self, raw) -> dict:
        """手改容错：结构补齐，值原样；未知键丢弃，不给前端留脏字段。"""
        state = self._defaults()
        if not isinstance(raw, dict):
            return state
        state["version"] = int(raw.get("version") or 1)
        state["updated_at"] = int(raw.get("updated_at") or 0)
        for name, entry in (raw.get("channels") or {}).items():
            entry = entry if isinstance(entry, dict) else {}
            state["channels"][str(name)] = {
                "enabled": entry.get("enabled", True) is not False,
                "hidden": bool(entry.get("hidden")),
                "note": str(entry.get("note") or ""),
            }
        for name, models in (raw.get("models") or {}).items():
            if isinstance(models, dict):
                state["models"][str(name)] = {
                    str(k): v is not False for k, v in models.items()}
        for name, models in (raw.get("suspended") or {}).items():
            if isinstance(models, dict):
                state["suspended"][str(name)] = {
                    str(k): (None if v is None else v is not False)
                    for k, v in models.items()}
        acked = raw.get("errors", {}).get("acked") if isinstance(raw.get("errors"), dict) else []
        state["errors"] = {"acked": [str(x) for x in (acked or [])
                                    if str(x).strip()][-MAX_ACKED:]}
        egress = raw.get("egress") if isinstance(raw.get("egress"), dict) else {}
        overrides = egress.get("channel_overrides") if isinstance(
            egress.get("channel_overrides"), dict) else {}
        clean_overrides = {}
        for name, target in overrides.items():
            name = str(name or "").strip()
            target = str(target or "").strip()
            if not name or not target:
                continue
            if target.upper() == "DIRECT" or target.lower().startswith(PROXY_SCHEMES):
                clean_overrides[name] = "DIRECT" if target.upper() == "DIRECT" else target
        state["egress"] = {"proxy_url": str(egress.get("proxy_url") or "").strip(),
                           "channel_overrides": clean_overrides,
                           "updated_at": int(egress.get("updated_at") or 0)}
        budgets = raw.get("budgets") if isinstance(raw.get("budgets"), dict) else {}
        glob = budgets.get("global") if isinstance(budgets.get("global"), dict) else {}
        state["budgets"]["global"] = {
            "amount": _amount(glob.get("amount")),
            "unit": str(glob.get("unit") or BUDGET_UNIT),
            "period": str(glob.get("period") or BUDGET_PERIOD),
        }
        items = []
        for item in budgets.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("label") or "").strip()
            if not key:
                continue
            items.append({
                "key": key, "label": str(item.get("label") or key),
                "amount": _amount(item.get("amount")),
                "unit": str(item.get("unit") or BUDGET_UNIT),
                "period": str(item.get("period") or BUDGET_PERIOD),
            })
        if items:
            state["budgets"]["items"] = items
        routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
        for key, default in ROUTING_DEFAULTS.items():
            value = routing.get(key, default)
            low, high = ROUTING_LIMITS[key]
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(default)
            if key == "timeout_s":
                state["routing"][key] = round(min(high, max(low, value)), 1)
            else:
                state["routing"][key] = int(min(high, max(low, value)))
        prompt_opt = raw.get("prompt_opt") if isinstance(raw.get("prompt_opt"), dict) else {}
        mode = str(prompt_opt.get("mode") or "").strip().lower()
        state["prompt_opt"] = {"mode": mode if mode in PROMPT_MODES else "",
                               "updated_at": int(prompt_opt.get("updated_at") or 0)}
        return state

    def _load(self, force: bool = False) -> dict:
        with self._lock:
            stamp = self._stamp_of()
            if self._state is not None and not force and stamp == self._stamp:
                return self._state
            if stamp is None:
                state = self._defaults()
            else:
                try:
                    state = self._normalize(
                        json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    state = self._defaults()
            self._state, self._stamp = state, stamp
            return state

    def _save(self, state: dict) -> None:
        state["updated_at"] = int(self._clock())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(self.path))
        with self._lock:
            self._state, self._stamp = state, self._stamp_of()

    # ── 读 ──────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load())

    def channel_entry(self, channel: str) -> dict:
        channel = (channel or "").strip()
        state = self._load()
        entry = state["channels"].get(channel)
        if not isinstance(entry, dict):
            return {"enabled": True, "hidden": False, "note": ""}
        return dict(entry)

    def channel_enabled(self, channel: str) -> bool:
        return bool(self.channel_entry(channel).get("enabled", True))

    def model_enabled(self, channel: str, model: str):
        """None = 未表态（跟随上游目录）；True/False = 本地面板明确开关。"""
        models = self._load()["models"].get((channel or "").strip()) or {}
        if model not in models:
            return None
        return bool(models[model])

    def disabled_models(self, channel: str) -> set:
        models = self._load()["models"].get((channel or "").strip()) or {}
        return {mid for mid, on in models.items() if not on}

    def exists(self) -> bool:
        return self.path.exists()

    # ── 写 ──────────────────────────────────────────────────
    def toggle_model(self, channel: str, model: str, enabled) -> dict:
        channel, model = (channel or "").strip(), (model or "").strip()
        if not channel or not model:
            raise ValueError("channel 与 id 均不可为空")
        with self._lock:
            state = self._load()
            bucket = state["models"].setdefault(channel, {})
            before = bucket.get(model)
            want = bool(enabled)
            changed = before != want
            if changed:
                bucket[model] = want
                self._save(state)
            return {"ok": True, "channel": channel, "id": model, "enabled": want,
                    "changed": 1 if changed else 0, "saved": 1 if changed else 0,
                    "before": before, "config": str(self.path)}

    def toggle_models(self, channel: str, ids, enabled) -> dict:
        channel = (channel or "").strip()
        wanted = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if not channel:
            raise ValueError("channel 不可为空")
        if not wanted:
            raise ValueError("ids 必须是非空列表")
        want = bool(enabled)
        with self._lock:
            state = self._load()
            bucket = state["models"].setdefault(channel, {})
            changed = 0
            for mid in wanted:
                if bucket.get(mid) != want:
                    bucket[mid] = want
                    changed += 1
            if changed:
                self._save(state)
            return {"ok": True, "channel": channel, "enabled": want,
                    "total": len(wanted), "changed": changed, "saved": changed,
                    "config": str(self.path)}

    def toggle_visibility(self, channel: str, hidden, reason: str = "",
                          known_models=()) -> dict:
        """渠道启停：隐藏时按快照停用该渠道全部已知模型，恢复时按快照还原。"""
        channel = (channel or "").strip()
        if not channel:
            raise ValueError("channel 不可为空")
        hide = bool(hidden)
        models = [str(m).strip() for m in (known_models or ()) if str(m).strip()]
        with self._lock:
            state = self._load()
            entry = state["channels"].setdefault(
                channel, {"enabled": True, "hidden": False, "note": ""})
            was_hidden = bool(entry.get("hidden"))
            bucket = state["models"].setdefault(channel, {})
            if hide:
                snapshot = {mid: bucket.get(mid) for mid in models}
                for mid, value in (state["models"].get(channel) or {}).items():
                    snapshot.setdefault(mid, value)
                state["suspended"][channel] = snapshot
                for mid in snapshot:
                    bucket[mid] = False
            else:
                snapshot = state["suspended"].pop(channel, {})
                for mid, value in snapshot.items():
                    if value is None:
                        bucket.pop(mid, None)
                    else:
                        bucket[mid] = bool(value)
            entry.update({"enabled": not hide, "hidden": hide,
                          "note": str(reason or "")[:120]})
            self._save(state)
            return {"ok": True, "channel": channel, "hidden": hide,
                    "changed": 1 if hide != was_hidden else 0,
                    "models": len(models), "reason": entry["note"],
                    "config": str(self.path)}

    def acked_errors(self) -> set:
        return set(self._load()["errors"]["acked"])

    def ack_errors(self, ids) -> dict:
        """本地确认错误行：只记 rid/id，不删账本行。"""
        wanted = []
        for raw in ids or ():
            value = str(raw).strip()
            if value and value not in wanted:
                wanted.append(value)
        with self._lock:
            state = self._load()
            current = state["errors"]["acked"]
            done = 0
            for value in wanted:
                if value not in current:
                    current.append(value)
                    done += 1
            state["errors"]["acked"] = current[-MAX_ACKED:]
            self._save(state)
            return {"ok": True, "total": len(wanted), "done": done,
                    "acked_total": len(state["errors"]["acked"]),
                    "config": str(self.path)}

    def egress_proxy(self, channel: str = "") -> str:
        """渠道出口：有覆盖用覆盖（DIRECT=空串），否则回退全局默认。"""
        egress = self._load()["egress"]
        channel = (channel or "").strip()
        if channel:
            override = (egress.get("channel_overrides") or {}).get(channel)
            if override == "DIRECT":
                return ""
            if override:
                return override
        return egress["proxy_url"]

    def egress_payload(self, channel: str = "") -> dict:
        egress = self._load()["egress"]
        payload = {"proxy_url": egress["proxy_url"],
                   "proxy_enabled": bool(egress["proxy_url"])}
        channel = (channel or "").strip()
        if not channel:
            return payload
        override = (egress.get("channel_overrides") or {}).get(channel, "")
        effective = self.egress_proxy(channel)
        payload.update({"channel": channel,
                        "channel_override": override or "INHERIT",
                        "channel_proxy_url": effective,
                        "channel_proxy_enabled": bool(effective),
                        "channel_overrides": dict(egress.get("channel_overrides") or {}),
                        "updated_at": int(egress.get("updated_at") or 0)})
        return payload

    def set_egress(self, url) -> dict:
        """出站代理：只收 http(s)；空串=归位环境默认。socks5 本地不支持，直接拒。"""
        value = str(url or "").strip()
        if value and not value.lower().startswith(PROXY_SCHEMES):
            raise ValueError("本地网关只支持 http:// 或 https:// 代理（空串=归位默认）")
        with self._lock:
            state = self._load()
            before = state["egress"]["proxy_url"]
            state["egress"].update({"proxy_url": value,
                                    "updated_at": int(self._clock())})
            self._save(state)
            return {"ok": True, "proxy_url": value, "proxy_enabled": bool(value),
                    "changed": 1 if value != before else 0,
                    "config": str(self.path)}

    def set_channel_egress(self, channel: str, mode: str = "inherit",
                           url: str = "") -> dict:
        """单渠道出口三态：inherit（跟随全局）/ direct（强制直连）/ proxy（独立 URL）。"""
        channel = (channel or "").strip()
        if not channel:
            raise ValueError("channel 必填")
        mode = (mode or "inherit").strip().lower()
        with self._lock:
            state = self._load()
            overrides = state["egress"].setdefault("channel_overrides", {})
            before = overrides.get(channel, "INHERIT")
            if mode in ("inherit", "default"):
                overrides.pop(channel, None)
                after = "INHERIT"
            elif mode in ("direct", "none"):
                overrides[channel] = "DIRECT"
                after = "DIRECT"
            elif mode in ("proxy", "url"):
                value = str(url or "").strip()
                if not value.lower().startswith(PROXY_SCHEMES):
                    raise ValueError("独立代理必须 http:// 或 https://")
                overrides[channel] = value
                after = value
            else:
                raise ValueError("mode 必须是 inherit/direct/proxy")
            state["egress"]["updated_at"] = int(self._clock())
            self._save(state)
        return {"ok": True, "channel": channel, "mode": after,
                "effective": self.egress_proxy(channel),
                "changed": 1 if after != before else 0,
                "config": str(self.path)}

    # ── 预算硬闸门 ──────────────────────────────────────────
    def budget_caps(self, channel: str = "") -> list:
        """已设上限的档 [(key, amount, period)]：global 在前，本渠道档在后。

        amount 为 None 的档不进列表——未设上限就是放行，不是 0。
        """
        state = self._load()
        channel = (channel or "").strip()
        caps = []
        glob = state["budgets"]["global"]
        if glob.get("amount") is not None:
            caps.append(("global", float(glob["amount"]),
                         glob.get("period") or BUDGET_PERIOD))
        if channel:
            row = next((it for it in state["budgets"]["items"] if it["key"] == channel), None)
            if row and row.get("amount") is not None:
                caps.append((channel, float(row["amount"]),
                             row.get("period") or BUDGET_PERIOD))
        return caps

    def budget_gate(self, channel: str, spent_total, spent_channel) -> dict:
        """硬闸门判定：支出 ≥ 上限即拒；≥ 80% 只报预警。只看已设上限的档，不猜。

        支出由调用方给（今日账本实际数）；本方法只做判定，不打库、不写盘。
        时间粒度是自然日，与 `period=day` 一致；跨日由调用方按当天起点重算。
        """
        channel = (channel or "").strip()
        spend = {"global": float(spent_total or 0.0), channel: float(spent_channel or 0.0)}
        caps = self.budget_caps(channel)
        worst = None                      # (占用率, 档位, 上限, 已花)
        for key, cap, period in caps:
            value = spend.get(key, 0.0)
            if value >= cap:
                pct = 100.0 if not cap else round(value / cap * 100, 1)
                return {"blocked": True, "key": key, "cap_cny": cap,
                        "spent_cny": round(value, 6), "period": period,
                        "cap_count": len(caps), "used_pct": pct,
                        "warn": True, "warn_pct": BUDGET_WARN_PCT}
            if cap:
                pct = round(value / cap * 100, 1)
                if worst is None or pct > worst[0]:
                    worst = (pct, key, cap, value)
        gate = {"blocked": False, "cap_count": len(caps),
                "caps": [key for key, _, _ in caps], "warn_pct": BUDGET_WARN_PCT}
        if worst is None:
            gate.update({"used_pct": None, "warn": False})
        else:
            pct, key, cap, value = worst
            gate.update({"used_pct": pct, "warn": pct >= BUDGET_WARN_PCT,
                         "key": key, "cap_cny": cap, "spent_cny": round(value, 6)})
        return gate

    def set_budget(self, key: str, amount=None, unit: str = BUDGET_UNIT,
                   label: str = "", period: str = BUDGET_PERIOD) -> dict:
        key = (key or "global").strip() or "global"
        value = _amount(amount)
        with self._lock:
            state = self._load()
            budgets = state["budgets"]
            if key == "global":
                budgets["global"].update({"amount": value,
                                          "unit": str(unit or BUDGET_UNIT),
                                          "period": str(period or BUDGET_PERIOD)})
            else:
                row = next((it for it in budgets["items"] if it["key"] == key), None)
                if row is None:
                    row = {"key": key, "label": label or key,
                           "amount": None, "unit": BUDGET_UNIT,
                           "period": BUDGET_PERIOD}
                    budgets["items"].append(row)
                row.update({"amount": value, "unit": str(unit or BUDGET_UNIT),
                            "label": label or row["label"],
                            "period": str(period or BUDGET_PERIOD)})
            self._save(state)
            return {"ok": True, "key": key, "amount": value,
                    "unit": str(unit or BUDGET_UNIT), "saved": 1,
                    "config": str(self.path)}


    # ── 提示词优化模式（off / shadow / enforce）──────────────
    def prompt_opt_mode(self) -> str:
        return str(self._load()["prompt_opt"].get("mode") or "")

    def set_prompt_opt_mode(self, mode: str) -> dict:
        value = str(mode or "").strip().lower()
        if value not in PROMPT_MODES:
            raise ValueError("mode 必须是 off / shadow / enforce")
        with self._lock:
            state = self._load()
            before = state["prompt_opt"].get("mode") or ""
            state["prompt_opt"] = {"mode": value, "updated_at": int(self._clock())}
            self._save(state)
        return {"ok": True, "mode": value,
                "changed": 1 if value != before else 0,
                "config": str(self.path)}

    # ── 路由策略（超时 / 重试 / 粘性 TTL / 入站限流）──────────
    def routing_policy(self) -> dict:
        return dict(self._load()["routing"])

    def set_routing_policy(self, payload: dict) -> dict:
        """只接受白名单键；越界值直接拒，不做静默截断（前端好定位）。"""
        if not isinstance(payload, dict):
            raise ValueError("策略必须是对象")
        wanted = {key: payload[key] for key in ROUTING_DEFAULTS if key in payload}
        if not wanted:
            raise ValueError("没有可写入的策略键（%s）" % "/".join(ROUTING_DEFAULTS))
        clean = {}
        for key, value in wanted.items():
            low, high = ROUTING_LIMITS[key]
            try:
                num = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("%s 必须是数字" % key) from exc
            if num < low or num > high:
                raise ValueError("%s 超出范围 %s-%s" % (key, low, high))
            clean[key] = round(num, 1) if key == "timeout_s" else int(num)
        with self._lock:
            state = self._load()
            before = dict(state["routing"])
            state["routing"].update(clean)
            self._save(state)
        return {"ok": True, "routing": dict(state["routing"]),
                "changed": sum(1 for key, value in clean.items() if before.get(key) != value),
                "config": str(self.path)}


__all__ = ["LocalControl", "BUDGET_UNIT", "BUDGET_PERIOD", "BUDGET_WARN_PCT",
           "MAX_ACKED", "ROUTING_DEFAULTS", "ROUTING_LIMITS"]
