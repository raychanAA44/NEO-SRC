# -*- coding: utf-8 -*-
"""KWSA lite 账本：SQLite 单表、峰谷价目计算与今日聚合。

口径与 NEO 对齐：
- 成功行才计价；价目缺失记 ``cost_cny=NULL`` 并计入“未计价”，不拿 0 充数。
- 失败行不计费。
- 价目支持扁平价与峰谷两档；每行落库应用的峰/谷档与实际单价，流水可复核。
- 金额统一人民币；令牌始终拆为输入 / 缓存命中 / 输出三段。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from core import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    rid TEXT,
    requested_model TEXT,
    model TEXT,
    channel TEXT,
    upstream TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    status INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    error TEXT,
    elapsed_ms REAL,
    ttft_ms REAL,
    cost_cny REAL,
    cost_reason TEXT,
    price_period TEXT,
    price_in REAL,
    price_cache REAL,
    price_out REAL,
    price_unit TEXT,
    price_note TEXT,
    client_ip TEXT,
    stream INTEGER DEFAULT 0,
    group_id TEXT,
    locked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);
CREATE INDEX IF NOT EXISTS idx_usage_channel ON usage(channel);
"""


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def day_start(ts=None) -> float:
    """当日 00:00（CST），与 HUB NEO 主账本同一口径。"""
    return float(clock.day_start(ts))


def day_key(ts=None) -> str:
    return clock.date(ts)


class PriceBook:
    """一份 pricing 配置的人民币价目投影。"""

    def __init__(self, pricing: dict):
        pricing = pricing or {}
        self.unit = str(pricing.get("unit") or "USD_per_mtok")
        self.usd_cny = float(pricing.get("usd_cny") or 7.2)
        self.channels = pricing.get("channels") if isinstance(pricing.get("channels"), dict) else {}

    @staticmethod
    def _candidates(model: str) -> list:
        raw = str(model or "").strip()
        bare = raw.split("/", 1)[-1] if "/" in raw else raw
        tagless = raw.split(":", 1)[0]
        return [raw, bare, tagless, "custom/" + bare]

    @staticmethod
    def _channel_block(raw) -> tuple:
        """兼容旧扁平表与带 peak_rule 的渠道块。"""
        if not isinstance(raw, dict):
            return {}, {}
        models = raw.get("models")
        if isinstance(models, dict):
            return models, raw
        return ({k: v for k, v in raw.items()
                 if isinstance(v, dict) and any(x in v for x in ("in", "cache", "out"))},
                {})

    def _lookup(self, model: str, channel: str = "") -> tuple:
        wanted = str(channel or "")
        order = ([wanted] if wanted else []) + [k for k in self.channels if k != wanted]
        for name in order:
            models, block = self._channel_block(self.channels.get(name))
            for cand in self._candidates(model):
                hit = models.get(cand)
                if isinstance(hit, dict):
                    return hit, block, name
        return {}, {}, ""

    @staticmethod
    def _is_peak(block: dict, ts=None) -> bool:
        rule = block.get("peak_rule")
        if not isinstance(rule, dict):
            return False
        windows = []
        kind = str(rule.get("kind") or "")
        if kind == "utc_weekday_window":
            windows = [(int(rule.get("start_hour", 12)), int(rule.get("end_hour", 18)))]
        elif kind == "utc_weekday_windows":
            windows = [(int(a), int(b)) for a, b in (rule.get("windows") or [])]
        if not windows:
            return False
        lt = time.gmtime(time.time() if ts is None else float(ts))
        weekday = (lt.tm_wday + 1) % 7  # 周日=0，周六=6
        if weekday in (0, 6):
            return False
        return any(start <= lt.tm_hour < end for start, end in windows)

    @staticmethod
    def _rule_label(block: dict) -> str:
        rule = block.get("peak_rule")
        if not isinstance(rule, dict):
            return "FLAT / 无峰谷"
        kind = str(rule.get("kind") or "")
        if kind == "utc_weekday_window":
            return "UTC 周一-周五 %02d:00-%02d:00 峰" % (
                int(rule.get("start_hour", 12)), int(rule.get("end_hour", 18)))
        if kind == "utc_weekday_windows":
            spans = " / ".join("%02d-%02d" % (int(a), int(b))
                               for a, b in (rule.get("windows") or []))
            return "UTC 周一-周五 %s 峰" % spans
        return "峰谷规则未识别"

    @staticmethod
    def _picked(row: dict, key: str, peak: bool, has_rule: bool) -> float:
        if has_rule and not peak:
            value = row.get("off_" + key)
            if value is not None:
                return float(value)
        return float(row.get(key) or 0.0)

    def rate(self, model: str, channel: str = "") -> dict:
        row, _, _ = self._lookup(model, channel)
        return row

    def describe(self, model: str, channel: str = "", ts=None) -> dict:
        row, block, hit_channel = self._lookup(model, channel)
        if not row:
            return {"configured": False, "period": None, "note": "价目未配置"}
        has_rule = isinstance(block.get("peak_rule"), dict)
        peak = self._is_peak(block, ts) if has_rule else False
        off_keys = ("off_in", "off_cache", "off_out")
        off_configured = any(row.get(k) is not None for k in off_keys)
        period = "flat" if not has_rule else ("peak" if peak else "offpeak")
        if not has_rule:
            note = "单一价"
        elif peak:
            note = "峰价"
        elif off_configured:
            note = "谷价"
        else:
            note = "谷价未配置，按峰价"
        return {
            "configured": True,
            "channel": hit_channel,
            "model": model,
            "unit": self.unit,
            "usd_cny": self.usd_cny,
            "period": period,
            "note": note,
            "peak": {k: row.get(k) for k in ("in", "cache", "out")},
            "offpeak": ({k: row.get("off_" + k) for k in ("in", "cache", "out")}
                        if off_configured else None),
            "rule": block.get("peak_rule"),
            "rule_label": self._rule_label(block),
        }

    def quote(self, model: str, prompt: int, cached: int, completion: int,
              channel: str = "", ts=None) -> dict:
        row, block, _ = self._lookup(model, channel)
        if not row:
            return {"cost_cny": None, "cost_reason": "价目未配置",
                    "price_period": None, "price_in": None, "price_cache": None,
                    "price_out": None, "price_unit": self.unit, "price_note": ""}
        has_rule = isinstance(block.get("peak_rule"), dict)
        peak = self._is_peak(block, ts) if has_rule else False
        in_rate = self._picked(row, "in", peak, has_rule)
        cache_rate = self._picked(row, "cache", peak, has_rule)
        out_rate = self._picked(row, "out", peak, has_rule)
        # 与 NEO 同口径：prompt 含缓存命中量，命中部分只按 cache 价计一次。
        billed_in = max(0, int(prompt or 0) - int(cached or 0))
        amount = (billed_in * in_rate + int(cached or 0) * cache_rate
                  + int(completion or 0) * out_rate) / 1_000_000
        if self.unit.startswith("USD"):
            amount *= self.usd_cny
        info = self.describe(model, channel, ts=ts)
        return {
            "cost_cny": round(amount, 6),
            "cost_reason": "",
            "price_period": info["period"],
            "price_in": in_rate,
            "price_cache": cache_rate,
            "price_out": out_rate,
            "price_unit": self.unit,
            "price_note": info["note"],
        }

    def compute(self, model: str, prompt: int, cached: int, completion: int,
                channel: str = "") -> tuple:
        quote = self.quote(model, prompt, cached, completion, channel=channel)
        return quote["cost_cny"], quote["cost_reason"]

    def rows(self) -> list:
        rows = []
        for channel, raw in self.channels.items():
            models, block = self._channel_block(raw)
            for model, rate in models.items():
                if not isinstance(rate, dict):
                    continue
                off = {k: rate.get("off_" + k) for k in ("in", "cache", "out")}
                off = off if any(v is not None for v in off.values()) else None
                rows.append({
                    "channel": channel,
                    "model": model,
                    "mode": "peak_offpeak" if isinstance(block.get("peak_rule"), dict) else "flat",
                    "peak": {k: rate.get(k) for k in ("in", "cache", "out")},
                    "offpeak": off,
                    "rule": block.get("peak_rule"),
                    "rule_label": self._rule_label(block),
                    "unit": self.unit,
                    "usd_cny": self.usd_cny,
                    "note": str(rate.get("note") or ""),
                })
        return rows

    def summary(self) -> dict:
        rows = self.rows()
        return {
            "unit": self.unit,
            "usd_cny": self.usd_cny,
            "channels": len(self.channels),
            "models": len(rows),
            "peak_offpeak": sum(1 for r in rows if r["mode"] == "peak_offpeak"),
            "flat": sum(1 for r in rows if r["mode"] == "flat"),
        }


class Ledger:
    """线程安全的 SQLite 账本门面。"""

    def __init__(self, path, pricing: dict, keep_days: int = 7):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pricebook = PriceBook(pricing)
        self.keep_days = max(1, int(keep_days))
        self._lock = threading.Lock()
        self._last_prune = 0.0
        with self._lock:
            con = self._connect()
            try:
                con.executescript(SCHEMA)
                self._migrate(con)
                con.commit()
            finally:
                con.close()
        self.prune(force=True)

    @staticmethod
    def _migrate(con) -> None:
        """旧库补列：只加不改，保留历史行。"""
        existing = {row[1] for row in con.execute("PRAGMA table_info(usage)").fetchall()}
        for column, ddl in (
            ("price_period", "TEXT"), ("price_in", "REAL"),
            ("price_cache", "REAL"), ("price_out", "REAL"),
            ("price_unit", "TEXT"), ("price_note", "TEXT"),
        ):
            if column not in existing:
                con.execute("ALTER TABLE usage ADD COLUMN %s %s" % (column, ddl))

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    # ── 写入 ────────────────────────────────────────────────
    def record(self, *, model="", requested_model="", prompt=0, cached=0, completion=0,
               total=None, status=0, ok=False, error="", elapsed_ms=None, ttft_ms=None,
               rid="", channel="", upstream="", client_ip="", stream=False,
               group_id="", locked=False, at=None, cost_reason=None) -> dict:
        prompt, cached, completion = _int(prompt), _int(cached), _int(completion)
        total = _int(total) if total is not None else prompt + completion
        success = bool(ok)
        ts = float(at) if at is not None else time.time()
        if success:
            quote = self.pricebook.quote(model, prompt, cached, completion,
                                         channel=channel, ts=ts)
            cost = quote["cost_cny"]
            reason = quote["cost_reason"]
        else:
            quote = {"price_period": None, "price_in": None, "price_cache": None,
                     "price_out": None, "price_unit": self.pricebook.unit,
                     "price_note": ""}
            cost, reason = None, "失败不计费"
        if cost_reason is not None:
            reason = cost_reason
        row = {
            "ts": ts, "day": day_key(ts), "rid": str(rid or ""),
            "requested_model": str(requested_model or ""), "model": str(model or ""),
            "channel": str(channel or ""), "upstream": str(upstream or ""),
            "prompt_tokens": prompt, "cached_tokens": cached,
            "completion_tokens": completion, "total_tokens": total,
            "status": int(status or 0), "ok": 1 if success else 0,
            "error": str(error or ""), "elapsed_ms": None if elapsed_ms is None else round(float(elapsed_ms), 1),
            "ttft_ms": None if ttft_ms is None else round(float(ttft_ms), 1),
            "cost_cny": cost, "cost_reason": reason,
            "price_period": quote.get("price_period"),
            "price_in": quote.get("price_in"),
            "price_cache": quote.get("price_cache"),
            "price_out": quote.get("price_out"),
            "price_unit": quote.get("price_unit"),
            "price_note": quote.get("price_note"),
            "client_ip": str(client_ip or ""),
            "stream": 1 if stream else 0, "group_id": str(group_id or ""),
            "locked": 1 if locked else 0,
        }
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    """INSERT INTO usage (ts, day, rid, requested_model, model, channel, upstream,
                       prompt_tokens, cached_tokens, completion_tokens, total_tokens, status, ok,
                       error, elapsed_ms, ttft_ms, cost_cny, cost_reason,
                       price_period, price_in, price_cache, price_out, price_unit, price_note,
                       client_ip, stream, group_id, locked)
                       VALUES (:ts, :day, :rid, :requested_model, :model, :channel, :upstream,
                       :prompt_tokens, :cached_tokens, :completion_tokens, :total_tokens,
                       :status, :ok, :error, :elapsed_ms, :ttft_ms, :cost_cny, :cost_reason,
                       :price_period, :price_in, :price_cache, :price_out, :price_unit, :price_note,
                       :client_ip, :stream, :group_id, :locked)""",
                    row,
                )
                row["id"] = cur.lastrowid
                con.commit()
            finally:
                con.close()
        self._maybe_prune()
        return row

    def record_from_payload(self, payload: bytes, *, request_model="", status=0, ok=None,
                            error="", elapsed_ms=None, ttft_ms=None, rid="", channel="",
                            upstream="", client_ip="", group_id="", locked=False) -> dict:
        """从 JSON 或 SSE 尾块提取 usage；取不到也记一行，保留请求事实。"""
        blob = _json_blob(payload)
        usage = (blob or {}).get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}

        def pick(*names, default=0):
            for name in names:
                if usage.get(name) is not None:
                    return usage.get(name)
                if details.get(name) is not None:
                    return details.get(name)
            return default

        prompt = pick("prompt_tokens", "input_tokens")
        completion = pick("completion_tokens", "output_tokens")
        cached = pick("cached_tokens", "cached_input_tokens")
        model = str(blob.get("model") or "") if isinstance(blob, dict) else ""
        success = (200 <= int(status or 0) < 300) if ok is None else bool(ok)
        return self.record(
            model=model or request_model, requested_model=request_model,
            prompt=prompt, cached=cached, completion=completion,
            total=usage.get("total_tokens"), status=status, ok=success,
            error=error, elapsed_ms=elapsed_ms, ttft_ms=ttft_ms, rid=rid,
            channel=channel, upstream=upstream, client_ip=client_ip,
            stream=_looks_stream(payload), group_id=group_id, locked=locked,
        )

    # ── 读取 ────────────────────────────────────────────────
    def spend_today(self, channel: str = "") -> float:
        return self.spend_since(day_start(), channel=channel)

    def spend_since(self, since: float, channel: str = "") -> float:
        sql = "SELECT COALESCE(SUM(cost_cny), 0) FROM usage WHERE ts >= ? AND cost_cny IS NOT NULL"
        params = [float(since)]
        if channel:
            sql += " AND channel = ?"
            params.append(channel)
        with self._lock:
            con = self._connect()
            try:
                return round(float(con.execute(sql, params).fetchone()[0] or 0.0), 6)
            finally:
                con.close()

    def summary_today(self, top_models: int = 8) -> dict:
        since = day_start()
        with self._lock:
            con = self._connect()
            try:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM usage WHERE ts >= ? ORDER BY ts DESC", (since,)
                ).fetchall()]
            finally:
                con.close()
        requests = len(rows)
        ok_rows = [r for r in rows if r["ok"]]
        priced = [r for r in ok_rows if r["cost_cny"] is not None]
        total_cost = round(sum(r["cost_cny"] for r in priced), 6)
        channels, models = {}, {}
        periods = {"peak": 0, "offpeak": 0, "flat": 0, "none": 0}
        for row in rows:
            period = row.get("price_period") or "none"
            periods[period] = periods.get(period, 0) + 1
            ch = channels.setdefault(row["channel"] or "", {
                "channel": row["channel"] or "", "requests": 0, "errors": 0,
                "tokens": 0, "cost_cny": 0.0, "priced": 0,
            })
            ch["requests"] += 1
            ch["errors"] += 0 if row["ok"] else 1
            ch["tokens"] += int(row["total_tokens"] or 0)
            if row["cost_cny"] is not None:
                ch["cost_cny"] = round(ch["cost_cny"] + row["cost_cny"], 6)
                ch["priced"] += 1
            key = (row["channel"] or "", row["model"] or "")
            md = models.setdefault(key, {
                "channel": row["channel"] or "", "model": row["model"] or "",
                "requests": 0, "tokens": 0, "cost_cny": 0.0,
            })
            md["requests"] += 1
            md["tokens"] += int(row["total_tokens"] or 0)
            if row["cost_cny"] is not None:
                md["cost_cny"] = round(md["cost_cny"] + row["cost_cny"], 6)
        by_channel = sorted(channels.values(), key=lambda r: r["requests"], reverse=True)
        by_model = sorted(models.values(), key=lambda r: (r["tokens"], r["requests"]), reverse=True)
        return {
            "day": day_key(),
            "generated_at": int(time.time()),
            "requests": requests,
            "ok": len(ok_rows),
            "errors": requests - len(ok_rows),
            "prompt_tokens": sum(int(r["prompt_tokens"] or 0) for r in rows),
            "cached_tokens": sum(int(r["cached_tokens"] or 0) for r in rows),
            "completion_tokens": sum(int(r["completion_tokens"] or 0) for r in rows),
            "total_tokens": sum(int(r["total_tokens"] or 0) for r in rows),
            "cost_cny": total_cost,
            "priced": len(priced),
            "unpriced": len(ok_rows) - len(priced),
            "periods": periods,
            "by_channel": by_channel,
            "by_model": by_model[:top_models],
        }

    def recent(self, limit: int = 20, channel: str = "", status: int | None = None,
               only_errors: bool = False) -> list:
        limit = max(1, min(int(limit or 20), 200))
        sql = "SELECT * FROM usage WHERE 1=1"
        params = []
        if channel:
            sql += " AND channel = ?"
            params.append(channel)
        if status is not None:
            sql += " AND status = ?"
            params.append(int(status))
        if only_errors:
            sql += " AND ok = 0"
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            con = self._connect()
            try:
                rows = [dict(r) for r in con.execute(sql, params).fetchall()]
            finally:
                con.close()
        for row in rows:
            row["at"] = clock.iso_t(row["ts"])
            row["time"] = clock.iso(row["ts"])[11:]
            row["tz_hour"] = None
        return rows

    def window(self, start: float, end: float, tz_offset_s: int = 8 * 3600) -> dict:
        """按时间窗聚合：渠道/模型/日/小时四维，供用量查账与峰谷对照。

        ``tz_offset_s`` 只影响小时与日的切桶，不改库里的 UTC 时间戳。
        """
        start, end = float(start), float(end)
        offset = int(tz_offset_s)
        with self._lock:
            con = self._connect()
            try:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM usage WHERE ts >= ? AND ts < ? ORDER BY ts",
                    (start, end)).fetchall()]
            finally:
                con.close()
        totals = {"requests": 0, "errors": 0, "unpriced": 0, "prompt_tokens": 0,
                  "cached_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "cost_cny": 0.0, "priced": 0}
        by_channel, by_model, by_day = {}, {}, {}
        by_hour = [0] * 24
        for row in rows:
            ok = bool(row.get("ok"))
            cost = row.get("cost_cny")
            totals["requests"] += 1
            if not ok:
                totals["errors"] += 1
            for key in ("prompt_tokens", "cached_tokens", "completion_tokens",
                        "total_tokens"):
                totals[key] += int(row.get(key) or 0)
            if cost is None:
                if ok:
                    totals["unpriced"] += 1
            else:
                totals["cost_cny"] += float(cost)
                totals["priced"] += 1
            for bucket, key in ((by_channel, row.get("channel") or "—"),
                                (by_model, row.get("model") or "—")):
                slot = bucket.setdefault(key, {
                    "requests": 0, "errors": 0, "tokens": 0, "cost_cny": 0.0,
                    "unpriced": 0})
                slot["requests"] += 1
                if not ok:
                    slot["errors"] += 1
                slot["tokens"] += int(row.get("total_tokens") or 0)
                if cost is None:
                    if ok:
                        slot["unpriced"] += 1
                else:
                    slot["cost_cny"] = round(slot["cost_cny"] + float(cost), 6)
            shifted = float(row.get("ts") or 0) + offset
            day = time.strftime("%Y-%m-%d", time.gmtime(shifted))
            by_day[day] = by_day.get(day, 0) + 1
            by_hour[int((shifted % 86400) // 3600)] += 1
        totals["cost_cny"] = round(totals["cost_cny"], 6)
        return {
            "start": int(start), "end": int(end), "tz_offset_s": offset,
            "totals": totals,
            "by_channel": [dict(row, channel=name) for name, row
                           in sorted(by_channel.items(),
                                     key=lambda kv: -kv[1]["requests"])],
            "by_model": [dict(row, model=name) for name, row
                         in sorted(by_model.items(),
                                   key=lambda kv: -kv[1]["requests"])[:12]],
            "by_day": [{"day": day, "requests": count}
                       for day, count in sorted(by_day.items())],
            "by_hour": [{"hour": hour, "requests": count}
                        for hour, count in enumerate(by_hour)],
        }

    def health(self) -> dict:
        with self._lock:
            con = self._connect()
            try:
                total = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
                oldest = con.execute("SELECT MIN(ts) FROM usage").fetchone()[0]
            finally:
                con.close()
        return {
            "backend": "sqlite",
            "path": str(self.path).replace("\\", "/"),
            "rows": int(total or 0),
            "oldest": None if oldest is None else clock.iso_t(oldest),
            "keep_days": self.keep_days,
        }

    # ── 维护 ────────────────────────────────────────────────
    def prune(self, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_prune < 3600:
            return 0
        cutoff = now - self.keep_days * 86400
        with self._lock:
            con = self._connect()
            try:
                removed = con.execute("DELETE FROM usage WHERE ts < ?", (cutoff,)).rowcount
                con.commit()
            finally:
                con.close()
        self._last_prune = now
        return int(removed or 0)

    def _maybe_prune(self) -> None:
        if time.time() - self._last_prune > 3600:
            try:
                self.prune()
            except Exception:
                pass

    def pricing(self) -> dict:
        return self.pricebook.summary()

    def pricing_rows(self) -> list:
        return self.pricebook.rows()


def _looks_stream(payload) -> bool:
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    return "data:" in text


def _json_blob(payload):
    """返回 JSON 对象；SSE 则从后往前找最后一个带 usage 的 data 帧。"""
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            blob = json.loads(stripped)
            if isinstance(blob, dict):
                return blob
        except ValueError:
            pass
    if "data:" in text:
        for line in reversed([l.strip() for l in text.splitlines() if l.strip().startswith("data:")]):
            chunk = line[5:].strip()
            if chunk in ("", "[DONE]"):
                continue
            try:
                cand = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("usage"):
                return cand
    return {}


def price_of(model: str, pricing: dict, channel: str = "") -> dict:
    return PriceBook(pricing).rate(model, channel=channel)
