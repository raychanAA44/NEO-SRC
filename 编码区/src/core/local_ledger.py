#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_ledger.py —— 8888 自带账本（本地记账，不依赖云端与 8795）。

背景：HUB NEO 的 8888 原先只是反代，`/api/*` 的账本数据全部来自上游
（云端 kswa 或本地 hub-ng 8795）。要让网关在“不连云、不起 8795”的前提下
仍有账可查，就必须自己记账：

  /v1/*  推理请求 → 直连模型上游（amr-link），响应里的 `usage` 原地解析
  记账   → 一行一请求写入本地 SQLite（令牌 / 状态 / 时延 / 花费）
  /api/* 账本读接口 → 由本模块直接作答（usage / status / dashboard）

三条不越界的纪律：
1. 算不出来就给 `None`，绝不补 0——价目缺失的行 `_cost.cny` 为 null，
   `_cost.bill_reason` 写明原因，前端如实显示“待价目”。
2. 只记可核对的事实：令牌数、状态码、耗时来自上游回执；模型名优先取
   上游回执里的 `model`（网关可能改写），取不到才回落到请求体。
3. 失败行不计费（`billable=False`），与 P2 契约 `cost_cny > 0 ⇒ billable`
   同一条铁律。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from core import clock
from core import pricing as pricebook

from core.paths import CONFIG_DIR, DATA_DIR

DEFAULT_DB = DATA_DIR / "ledger.db"
DEFAULT_PRICING = CONFIG_DIR / "pricing.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  rid TEXT,
  model TEXT,
  channel TEXT,
  upstream TEXT,
  prompt_tokens INTEGER,
  cached_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  status INTEGER,
  ok INTEGER NOT NULL DEFAULT 0,
  ttft_ms REAL,
  error TEXT,
  elapsed_ms REAL,
  cost_cny REAL,
  cost_reason TEXT,
  client_ip TEXT,
  stream INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
"""


def _load_pricing(path: Path) -> dict:
    """兼容旧调用：返回“模型 → 人民币三档价”的平表。

    真正的渠道/峰谷/折扣逻辑在 `core.pricing`；这里只做展示与旧测试的
    平表投影，避免 UI 目录一次显示多个渠道的同名模型。
    """
    return pricebook.flat_rates(path)


def _pct(sorted_values: list, q: float):
    """最近秩法分位数：不插值，返回真实观测到的耗时，避免编出没出现过的数。"""
    if not sorted_values:
        return None
    rank = max(1, int(round(q / 100 * len(sorted_values) + 0.5)))
    return round(sorted_values[min(len(sorted_values), rank) - 1], 1)


def _iso(ts) -> str:
    """账本时间字符串（CST，%Y-%m-%d %H:%M:%S）。前端「数据时标」直接读它。"""
    try:
        return clock.iso(ts)
    except (TypeError, ValueError, OSError):
        return ""


def _health(today: dict, priced_models: int) -> dict:
    """健康分只由今日真实事实推导，不读外部常量：

    100 起扣：失败率 ×2（上限 40）、成功但缺价目行 −15、价目表空 −25、今日无入站 −5。
    恒带 `health_reasons`，页面能解释为什么不是 100，避免出现无法追溯的「假分」。
    """
    score = 100.0
    reasons = []
    reqs = _int(today.get("requests"))
    rate = today.get("success_rate")
    if reqs and rate is not None:
        loss = max(0.0, 100.0 - float(rate))
        if loss > 0:
            score -= min(40.0, loss * 2)
            reasons.append("失败率 %.2f%%" % loss)
    else:
        score -= 5
        reasons.append("今日无入站")
    unpriced = _int(today.get("unpriced_rows"))
    if unpriced:
        score -= 15
        reasons.append("%d 行缺价目" % unpriced)
    if not priced_models:
        score -= 25
        reasons.append("价目表为空")
    score = int(round(max(0.0, min(100.0, score))))
    text = "正常" if score >= 90 else ("注意" if score >= 70 else "降级")
    return {"health_score": score, "health_text": text, "health_reasons": reasons}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _SqliteStore:
    """单文件账本（测试与无库环境用）：连接按调用创建，避免 HTTP 线程共享句柄。"""

    kind = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.target = self.path.as_posix()
        with closing(self.connect()) as con:
            con.executescript(SCHEMA)
            self._migrate(con)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """老库没有 ttft_ms 列：建表语句只对新库生效，存量就地补列。"""
        cols = {row[1] for row in con.execute("PRAGMA table_info(usage)")}
        if "ttft_ms" not in cols:
            con.execute("ALTER TABLE usage ADD COLUMN ttft_ms REAL")
            con.commit()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def insert(self, row: dict) -> None:
        with closing(self.connect()) as con:
            con.execute(
                "INSERT INTO usage (at, rid, model, channel, upstream, prompt_tokens,"
                " cached_tokens, completion_tokens, total_tokens, status, ok, error,"
                " elapsed_ms, ttft_ms, cost_cny, cost_reason, client_ip, stream)"
                " VALUES (:at,:rid,:model,:channel,:upstream,:prompt_tokens,"
                ":cached_tokens,:completion_tokens,:total_tokens,:status,:ok,:error,"
                ":elapsed_ms,:ttft_ms,:cost_cny,:cost_reason,:client_ip,:stream)", row)
            con.commit()

    def fetch(self, since=None, limit=None) -> list:
        sql = "SELECT * FROM usage"
        args = []
        if since is not None:
            sql += " WHERE at >= ?"
            args.append(float(since))
        sql += " ORDER BY at DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with closing(self.connect()) as con:
            return [dict(r) for r in con.execute(sql, args)]

    def count(self) -> int:
        with closing(self.connect()) as con:
            return int(con.execute("SELECT COUNT(*) FROM usage").fetchone()[0])

    def update_cost(self, row_id: int, cost: float, reason: str) -> int:
        """只补空金额：已经有 cost_cny 的行绝不被覆盖（记账即定稿）。"""
        with closing(self.connect()) as con:
            cur = con.execute("UPDATE usage SET cost_cny = ?, cost_reason = ?"
                              " WHERE id = ? AND cost_cny IS NULL", (cost, reason, row_id))
            con.commit()
            return cur.rowcount


MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS `{table}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  at DOUBLE NOT NULL,
  rid VARCHAR(64),
  model VARCHAR(191),
  channel VARCHAR(64),
  upstream VARCHAR(255),
  prompt_tokens INT NOT NULL DEFAULT 0,
  cached_tokens INT NOT NULL DEFAULT 0,
  completion_tokens INT NOT NULL DEFAULT 0,
  total_tokens INT NOT NULL DEFAULT 0,
  status INT NOT NULL DEFAULT 0,
  ok TINYINT NOT NULL DEFAULT 0,
  ttft_ms DOUBLE NULL,
  error VARCHAR(255),
  elapsed_ms DOUBLE NULL,
  cost_cny DECIMAL(20,8) NULL,
  cost_reason VARCHAR(64),
  client_ip VARCHAR(64),
  stream TINYINT NOT NULL DEFAULT 0,
  KEY idx_at (at),
  KEY idx_model (model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class _MySQLStore:
    """MySQL 账本：独立表，不碰旧账本（hub_dw.usage_archive 之类）任何一行。

    每次操作新建连接：8888 是多线程 HTTPServer，连接不能跨线程共享。
    驱动用已装好的 pymysql，不引入新依赖。
    """

    kind = "mysql"

    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self.table = self.cfg.pop("table", "usage")
        self.target = "mysql://%s/%s.%s" % (
            self.cfg.get("host") or "127.0.0.1", self.cfg.get("database"), self.table)
        with self.connect() as con, con.cursor() as cur:
            cur.execute(MYSQL_SCHEMA.format(table=self.table))
            con.commit()

    def connect(self):
        import pymysql  # 仅 MySQL 模式才需要；SQLite 路径不碰它
        return pymysql.connect(charset="utf8mb4", autocommit=False,
                               cursorclass=pymysql.cursors.DictCursor, **self.cfg)

    def insert(self, row: dict) -> None:
        cols = ("at, rid, model, channel, upstream, prompt_tokens, cached_tokens,"
                " completion_tokens, total_tokens, status, ok, error, elapsed_ms,"
                " ttft_ms, cost_cny, cost_reason, client_ip, stream").split(", ")
        sql = "INSERT INTO `%s` (%s) VALUES (%s)" % (
            self.table, ", ".join("`%s`" % c for c in cols),
            ", ".join(["%s"] * len(cols)))
        with self.connect() as con, con.cursor() as cur:
            cur.execute(sql, [row.get(c) for c in cols])
            con.commit()

    def fetch(self, since=None, limit=None) -> list:
        sql = "SELECT * FROM `%s`" % self.table
        args = []
        if since is not None:
            sql += " WHERE at >= %s"
            args.append(float(since))
        sql += " ORDER BY at DESC"
        if limit:
            sql += " LIMIT %s"
            args.append(int(limit))
        with self.connect() as con, con.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        out = []
        for r in rows:
            if r.get("cost_cny") is not None:
                r["cost_cny"] = float(r["cost_cny"])  # DECIMAL -> float，与 SQLite 同类型
            out.append(dict(r))
        return out

    def count(self) -> int:
        with self.connect() as con, con.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM `%s`" % self.table)
            return int((cur.fetchone() or {}).get("n") or 0)

    def update_cost(self, row_id: int, cost: float, reason: str) -> int:
        """只补空金额：已经有 cost_cny 的行绝不被覆盖（记账即定稿）。"""
        with self.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE `%s` SET cost_cny = %%s, cost_reason = %%s"
                        " WHERE id = %%s AND cost_cny IS NULL" % self.table,
                        (cost, reason, row_id))
            con.commit()
            return cur.rowcount


class LocalLedger:
    """线程安全的账本门面：存储后端可换（MySQL / SQLite），读模型共享一套。"""

    def __init__(self, db_path=None, pricing_path=None, store=None):
        self.pricing_path = Path(pricing_path or os.environ.get("HUB_PRICING") or DEFAULT_PRICING)
        self._lock = threading.Lock()
        self._store = store or self._build_store(db_path)

    @staticmethod
    def _build_store(db_path=None):
        # 显式给了 db_path 就用那个文件（测试与本地核对）；env 只决定“不给路径时”用哪个后端。
        if db_path is None and (os.environ.get("HUB_LEDGER_BACKEND") or "sqlite").strip().lower() == "mysql":
            return _MySQLStore({
                "host": os.environ.get("HUB_LEDGER_MYSQL_HOST") or "127.0.0.1",
                "port": int(os.environ.get("HUB_LEDGER_MYSQL_PORT") or 3306),
                "user": os.environ.get("HUB_LEDGER_MYSQL_USER") or "root",
                "password": os.environ.get("HUB_LEDGER_MYSQL_PASSWORD") or "",
                "database": os.environ.get("HUB_LEDGER_MYSQL_DB") or "hub_neo",
                "table": os.environ.get("HUB_LEDGER_MYSQL_TABLE") or "usage",
            })
        return _SqliteStore(Path(db_path or os.environ.get("HUB_LEDGER_DB") or DEFAULT_DB))

    # ── 基础设施 ─────────────────────────────────────────────
    @property
    def backend(self) -> str:
        return self._store.kind

    def connect(self):
        """后端原始连接（sqlite3.Connection / pymysql.Connection）。

        `accounts` 这类同库旁表共用它，不再猜后端；**用完必须 close**：
        Windows 上不关会占住临时库文件，tearDown 的 unlink 就会 PermissionError。
        """
        return self._store.connect()

    @property
    def db_path(self):
        """SQLite 库文件路径；MySQL 后端返回 None（没有本地文件可指）。"""
        return getattr(self._store, "path", None)

    @property
    def target(self) -> str:
        return self._store.target

    def pricing(self, channel: str = "") -> dict:
        """平表投影（人民币）；渠道为空时合并所有渠道，供旧 UI/测试读取。"""
        return pricebook.flat_rates(self.pricing_path, channel=channel)

    def pricing_rows(self, channel: str = "") -> list:
        return pricebook.rows(self.pricing_path, channel=channel)

    def pricing_doc(self) -> dict:
        return pricebook.load_doc(self.pricing_path)

    def pricing_summary(self) -> dict:
        return pricebook.summary(self.pricing_path)

    def pricing_template(self) -> dict:
        return pricebook.template()

    def import_pricing(self, payload: dict, *, mode: str = "merge",
                       dry_run: bool = False, actor: str = "") -> dict:
        return pricebook.import_payload(payload, path=self.pricing_path, mode=mode,
                                        dry_run=dry_run, actor=actor)

    def price_of(self, model: str, channel: str = ""):
        """按 (渠道, 模型) 取人民币有效价；不支持渠道时退回全局旧平表。

        客户端常带 `vendor/` 前缀调用，而价目是按渠道模型 id 给的；
        匹配顺序：精确 → 去 vendor/ → 去 :tag → 全局兼容表。
        """
        doc = self.pricing_doc()
        return pricebook.rate_of(doc, channel, model)

    def compute_cost(self, model: str, prompt: int, cached: int, completion: int,
                     channel: str = "", ts=None):
        """返回 (cost_cny | None, reason)。单位价按“元 / 百万 token”。"""
        doc = self.pricing_doc()
        parts = pricebook.cost_parts(doc, channel, model, prompt, cached,
                                     completion, ts=ts)
        if parts is None:
            return None, "价目未配置"
        return round(parts["in"] + parts["cache"] + parts["out"], 6), ""

    def saved_of(self, model: str, cached: int, channel: str = ""):
        """缓存节省：命中 token 按输入价应付 − 按缓存价实付；价目缺失给 None。"""
        price = self.price_of(model, channel=channel)
        if not price or not _int(cached):
            return None
        return round((price["in"] - price["cache"]) * _int(cached) / 1_000_000, 6)

    # ── 写入 ─────────────────────────────────────────────────
    def record(self, *, model="", prompt=0, cached=0, completion=0, total=None,
               status=0, ok=False, error="", elapsed_ms=None, ttft_ms=None, rid="",
               channel="", upstream="", client_ip="", stream=False, at=None) -> dict:
        prompt, cached, completion = _int(prompt), _int(cached), _int(completion)
        total = _int(total) if total is not None else prompt + completion
        cost, reason = (None, "失败不计费") if not ok else self.compute_cost(
            model, prompt, cached, completion, channel=channel)
        row = dict(
            at=float(at) if at is not None else time.time(),
            rid=str(rid or ""), model=str(model or ""),
            channel=str(channel or ""), upstream=str(upstream or ""),
            prompt_tokens=prompt, cached_tokens=cached,
            completion_tokens=completion, total_tokens=total,
            status=int(status or 0), ok=1 if ok else 0, error=str(error or ""),
            elapsed_ms=None if elapsed_ms is None else round(float(elapsed_ms), 1),
            ttft_ms=None if ttft_ms is None else round(float(ttft_ms), 1),
            cost_cny=cost, cost_reason=reason, client_ip=str(client_ip or ""),
            stream=1 if stream else 0,
        )
        with self._lock:
            self._store.insert(row)
        return row

    def backfill_unpriced(self, reason: str = "补算：价目前缀匹配修正", apply: bool = False) -> dict:
        """给“成功但没金额”的行按现行价目补算。只动 cost_cny 为空的行，不改已有金额。

        用途：修正口存在缺陷（如模型名带 vendor/ 前缀）导致的历史漏账；
        不是“重算历史”，已计价的行一行不碰。默认 dry-run，`apply=True` 才落库。
        """
        candidates = [r for r in self._rows() if r["ok"] and r["cost_cny"] is None]
        plan, updated = [], 0
        for r in candidates:
            cost, why = self.compute_cost(r["model"], r["prompt_tokens"],
                                          r["cached_tokens"], r["completion_tokens"],
                                          channel=r["channel"])
            if cost is None:
                continue
            plan.append({"id": r["id"], "model": r["model"], "cost_cny": cost})
            if apply:
                updated += self._store.update_cost(r["id"], cost, reason)
        return {"candidates": len(candidates), "priced": len(plan),
                "updated": updated, "applied": bool(apply), "plan": plan}

    def record_from_payload(self, payload: bytes, *, request_model="",
                            model_override="", **meta) -> dict:
        """从上游回执里取 usage（兼容 JSON 与 SSE 尾块）。取不到就记一条无令牌行。

        兼容两种口径：
          · OpenAI chat：prompt_tokens / completion_tokens / total_tokens
          · Ollama systemone：input_tokens / output_tokens（output 可为 0）
        """
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
        blob = None
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                blob = json.loads(stripped)
            except Exception:
                blob = None
        if blob is None and "data:" in text:
            for line in reversed([l.strip() for l in text.splitlines() if l.strip().startswith("data:")]):
                chunk = line[5:].strip()
                if chunk in ("", "[DONE]"):
                    continue
                try:
                    cand = json.loads(chunk)
                except Exception:
                    continue
                if isinstance(cand, dict) and cand.get("usage"):
                    blob = cand
                    break
        usage = (blob or {}).get("usage") or {}
        model = str(model_override or (blob or {}).get("model") or request_model or "")
        details = usage.get("prompt_tokens_details") or {}

        def pick(*names, default=0):
            for name in names:
                if name in usage and usage.get(name) is not None:
                    return usage.get(name)
                if name in details and details.get(name) is not None:
                    return details.get(name)
            return default

        prompt = pick("prompt_tokens", "input_tokens")
        completion = pick("completion_tokens", "output_tokens")
        cached = pick("cached_tokens", "cached_input_tokens")
        meta.setdefault("ok", True)
        return self.record(
            model=model,
            prompt=prompt,
            cached=cached or 0,
            completion=completion,
            total=usage.get("total_tokens"),
            stream=bool(meta.pop("stream", False)) or ("data:" in text),
            **meta,
        )

    # ── 读取 ─────────────────────────────────────────────────
    def _rows(self, since=None, limit=None) -> list:
        return self._store.fetch(since=since, limit=limit)

    def rows(self, since=None, limit=None) -> list:
        """读侧统一入口（读模型用）；不把底层 store 暴露给调用方。"""
        return self._rows(since=since, limit=limit)

    def spend_since(self, since) -> tuple:
        """自 since 起的实际支出：(总额, 按渠道)。

        与预算读侧同口径：没价目的行（cost_cny is None）不计——不拿 0 充数。
        渠道为空的行记在 "" 键下，由调用方决定归到哪个渠道。
        """
        total, by_channel = 0.0, {}
        for row in self._rows(since=since):
            cost = row["cost_cny"]
            if cost is None:
                continue
            total += cost
            key = row["channel"] or ""
            by_channel[key] = by_channel.get(key, 0.0) + cost
        return round(total, 6), {k: round(v, 6) for k, v in by_channel.items()}

    def window_stats(self, channel: str = "", now=None) -> dict:
        """本地实测用量分窗：今日 / 近 5 小时 / 本周 / 本月。

        官方没公布分窗额度时，界面用这组数字回答「这段用了多少」，
        字段名带 local 前缀，不冒充官方剩余额度。
        """
        now = float(now or time.time())
        starts = {
            "today": clock.day_start(now),
            "h5": now - 5 * 3600,
            "week": clock.week_start(now),
            "month": clock.month_start(now),
        }
        all_rows = self._rows()
        out = {}
        for key, since in starts.items():
            rows = [r for r in all_rows
                    if r["at"] >= since and (not channel or (r["channel"] or "") == channel)]
            cost = sum(r["cost_cny"] for r in rows if r["cost_cny"] is not None)
            out[key] = {
                "requests": len(rows),
                "ok": sum(1 for r in rows if r["ok"]),
                "errors": sum(1 for r in rows if not r["ok"]),
                "tokens": sum(_int(r["total_tokens"]) for r in rows),
                "cost_cny": round(cost, 6) if any(r["cost_cny"] is not None for r in rows) else None,
                "since": int(since),
                "until": int(now),
                "source": "local",
            }
        return out

    def daily_summary(self, day_start=None, top_models: int = 8) -> dict:
        """今日全量聚合（纪事信笺/报表专用）：一次读全量行，按渠道/模型/租户/状态分组。

        与 `/api/usage` 的 50 条样本无关：这里直接对当日全部行做统计，
        算不出来就给 None，绝不补 0、绝不前端截断。
        """
        now = time.time()
        if day_start is None:
            day_start = clock.day_start(now)
        since = float(day_start)
        rows = self._rows(since=since)
        executive = self._summary(rows)
        priced = [r for r in rows if r["cost_cny"] is not None]
        cost_total = executive["cost_cny"] or 0.0
        executive["calls"] = executive["requests"]
        executive["avg_cost_cny"] = (round(cost_total / len(priced), 6)
                                     if priced else None)
        executive["cost_rows"] = len(priced)
        executive["tokens_total"] = executive["tokens"]

        def blank(calls=0):
            return {"calls": calls, "ok": 0, "errors": 0,
                    "prompt": 0, "cached": 0, "completion": 0, "total": 0,
                    "cost": 0.0, "priced": 0,
                    "lat": [], "ttft": [], "last_at": 0}

        def add(bucket, row):
            bucket["calls"] += 1
            if row["ok"]:
                bucket["ok"] += 1
            else:
                bucket["errors"] += 1
            bucket["prompt"] += _int(row["prompt_tokens"])
            bucket["cached"] += _int(row["cached_tokens"])
            bucket["completion"] += _int(row["completion_tokens"])
            bucket["total"] += _int(row["total_tokens"])
            if row["cost_cny"] is not None:
                bucket["cost"] += row["cost_cny"]
                bucket["priced"] += 1
            if row["elapsed_ms"] is not None:
                bucket["lat"].append(row["elapsed_ms"])
            if row["ttft_ms"] is not None:
                bucket["ttft"].append(row["ttft_ms"])
            bucket["last_at"] = max(bucket["last_at"], int(row["at"] or 0))

        channels, models, tenants = {}, {}, {}
        statuses, error_rows = {}, []
        for row in rows:
            channel = row["channel"] or "—"
            add(channels.setdefault(channel, dict(blank(), channel=channel)), row)
            model_key = "%s/%s" % (channel, row["model"] or "—")
            model = models.setdefault(model_key, dict(blank(), channel=channel,
                                                      model=row["model"] or "—"))
            add(model, row)
            tenant_id = row.get("key_name") or row.get("client_ip") or "—"
            tenant = tenants.setdefault(tenant_id, dict(blank(), tenant=tenant_id))
            add(tenant, row)
            status = int(row["status"] or 0)
            if status >= 400:
                statuses[status] = statuses.get(status, 0) + 1
            if not row["ok"]:
                error_rows.append({
                    "at": int(row["at"] or 0), "model": row["model"] or "—",
                    "channel": channel, "status": status,
                    "error": (row["error"] or "")[:120],
                })

        def finish(bucket):
            latency = sorted(bucket.pop("lat"))
            ttfts = sorted(bucket.pop("ttft"))
            priced_rows = bucket.pop("priced")
            bucket["cost_cny"] = round(bucket.pop("cost"), 6) if priced_rows else None
            bucket["avg_latency_ms"] = (round(sum(latency) / len(latency), 1)
                                        if latency else None)
            bucket["p95_latency_ms"] = _pct(latency, 95)
            bucket["ttft_avg_ms"] = (round(sum(ttfts) / len(ttfts), 1)
                                     if ttfts else None)
            bucket["cache_hit_pct"] = (
                round(bucket["cached"] / (bucket["prompt"] + bucket["cached"]) * 100, 2)
                if (bucket["prompt"] + bucket["cached"]) else None)
            return bucket

        channel_rows = sorted((finish(v) for v in channels.values()),
                              key=lambda r: (-r["calls"], r["channel"]))
        model_rows = sorted((finish(v) for v in models.values()),
                            key=lambda r: (-r["calls"], -r["total"], r["model"]))
        tenant_rows = sorted((finish(v) for v in tenants.values()),
                             key=lambda r: (-r["calls"], r["tenant"]))
        error_rows.sort(key=lambda r: -r["at"])
        return {
            "ok": True,
            "date": clock.date(since),
            "day_start": int(since),
            "generated_at": int(now),
            "source": "local-ledger",
            "executive": executive,
            "channels": channel_rows,
            "models": model_rows[:max(1, int(top_models))],
            "tenants": {
                "kind": "key" if any(row.get("key_name") for row in rows) else "client_ip",
                "rows": tenant_rows,
                "note": ("按 API KEY 归属" if any(row.get("key_name") for row in rows)
                         else "本地 /v1 未绑定 KEY 身份，按客户端 IP 归属"),
            },
            "incidents": {
                "error_rows": len(error_rows),
                "status_counts": [{"status": s, "count": c}
                                  for s, c in sorted(statuses.items())],
                "cooldown_signals": sum(c for s, c in statuses.items()
                                        if s in (429, 503)),
                "recent_errors": error_rows[:5],
            },
        }

    def _summary(self, rows: list) -> dict:
        reqs = len(rows)
        ok = sum(1 for r in rows if r["ok"])
        priced = [r for r in rows if r["cost_cny"] is not None]
        prompt = sum(_int(r["prompt_tokens"]) for r in rows)
        cached = sum(_int(r["cached_tokens"]) for r in rows)
        completion = sum(_int(r["completion_tokens"]) for r in rows)
        seen = prompt + cached
        latency = sorted(r["elapsed_ms"] for r in rows if r["elapsed_ms"] is not None)
        ttfts = sorted(r["ttft_ms"] for r in rows if r["ttft_ms"] is not None)
        return {
            "requests": reqs,
            "ok": ok,
            "errors": reqs - ok,
            # 前端 F.pct 直接把数字当百分数（57.1 而非 0.571），这里统一按百分比给。
            "success_rate": round(ok / reqs * 100, 2) if reqs else None,
            "tokens": sum(_int(r["total_tokens"]) for r in rows),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": completion,
            "cache_hit_pct": round(cached / seen * 100, 2) if seen else None,
            "cost_cny": round(sum(r["cost_cny"] for r in priced), 6) if priced else None,
            # 只把“成功但没价目”的算未计价；失败行本来就不该有金额，不混进去。
            "unpriced_rows": sum(1 for r in rows if r["ok"] and r["cost_cny"] is None),
            "mix": {"prompt": prompt, "cached": cached, "completion": completion},
            # 概览页读的是 st.elapsed_ms_avg（不是 latency.avg_ms），两个都留。
            "elapsed_ms_avg": round(sum(latency) / len(latency), 1) if latency else None,
            "latency": {
                "count": len(latency),
                "avg_ms": round(sum(latency) / len(latency), 1) if latency else None,
                "p50_ms": _pct(latency, 50),
                "p95_ms": _pct(latency, 95),
                "ttft_count": len(ttfts),
                "ttft_avg_ms": round(sum(ttfts) / len(ttfts), 1) if ttfts else None,
                "ttft_p50_ms": _pct(ttfts, 50),
            },
        }

    def usage_rows(self, limit=50, offset=0) -> list:
        rows = self._rows(limit=int(limit) + int(offset))
        rows = rows[int(offset):]
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "at": r["at"],
                "rid": r["rid"],
                "model": r["model"],
                "channel": r["channel"],
                "channel_label": r["channel"],
                "upstream": r["upstream"],
                "prompt_tokens": r["prompt_tokens"],
                "input_tokens": r["prompt_tokens"],
                "cached_tokens": r["cached_tokens"],
                "completion_tokens": r["completion_tokens"],
                "output_tokens": r["completion_tokens"],
                "total_tokens": r["total_tokens"],
                "cache_hit_pct": (round(r["cached_tokens"] / (r["prompt_tokens"] + r["cached_tokens"]) * 100, 2)
                                  if (r["prompt_tokens"] or 0) + (r["cached_tokens"] or 0) else None),
                "status": r["status"],
                "outcome": "ok" if r["ok"] else "error",
                "error": r["error"],
                "elapsed_ms": r["elapsed_ms"],
                "ttft_ms": r["ttft_ms"],
                "cost_cny": r["cost_cny"],
                "_cost": {
                    "cny": r["cost_cny"],
                    "usd": None,
                    "official": None,
                    "list": None,
                    "saved_cny": self.saved_of(r["model"], r["cached_tokens"],
                                                channel=r["channel"]) if r["ok"] else None,
                    "billable": bool(r["ok"] and (r["cost_cny"] or 0) > 0),
                    "bill_reason": r["cost_reason"] or ("失败不计费" if not r["ok"] else ""),
                },
                "client_ip": r["client_ip"],
                "stream": bool(r["stream"]),
            })
        # 计费口径随行回传：input-only 模型能直接看到 out=0 的来源，
        # 不靠前端猜“为什么 completion 是 0 却仍有金额”。
        for item, r in zip(out, rows):
            parts = pricebook.cost_parts(
                self.pricing_doc(), r["channel"], r["model"],
                r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"],
                ts=r["at"]) if r["ok"] else None
            rate = (parts or {}).get("rate") or {}
            item["billing"] = {
                "basis": rate.get("billing_basis") or "token",
                "out_source": rate.get("out_source") or "",
                "cache_source": rate.get("cache_source") or "",
                "rate_in_cny": rate.get("in"),
                "rate_cache_cny": rate.get("cache"),
                "rate_out_cny": rate.get("out"),
                "input_cost_cny": None if parts is None else round(parts["in"], 9),
                "cache_cost_cny": None if parts is None else round(parts["cache"], 9),
                "output_cost_cny": None if parts is None else round(parts["out"], 9),
            }
        return out

    def usage_payload(self, limit=50, offset=0) -> dict:
        rows = self.usage_rows(limit=limit, offset=offset)
        total = self._store.count()
        return {
            "rows": rows,
            "total": total,
            "limit": int(limit),
            "offset": int(offset),
            "generated_at": int(time.time()),
            "source": "local-ledger",
        }

    def status_payload(self, channel="") -> dict:
        now = time.time()
        midnight = clock.day_start(now)
        all_rows = self._rows()
        today = self._summary([r for r in all_rows if r["at"] >= midnight])
        hour = self._summary([r for r in all_rows if r["at"] >= now - 3600])
        overall = self._summary(all_rows)
        priced = len(self.pricing(channel))
        payload = {
            "generated_at": int(now),
            "at": int(now),
            "iso": _iso(now),
            "source": "local-ledger",
            "traffic": {"today": today, "all": overall, "last_hour": hour},
            # 旧前端读 status.today.mix；保留同一份数据，避免两处各算一次。
            "today": {"mix": today["mix"], "cost_cny": today["cost_cny"],
                      "requests": today["requests"], "tokens": today["tokens"]},
            "routing": {"breaker": {"active": 0}, "switches_1h": 0,
                        "cooling": 0, "cooling_next_secs": None,
                        "account_concurrency": 0, "channel_sticky": []},
            # 前端按 name 在 status.pool.channels 里查条目（数组，不是计数）。
            "pool": {"channels": ([{"name": channel, "label": channel, "enabled": True}]
                                  if channel else []), "egress": []},
            "accounts": {"total": 0, "ok": 0, "active": 0},
            "channels": {"ready": 1 if channel else 0, "total": 1 if channel else 0},
            "ledger": {
                # 用正斜杠上报路径：Windows 反斜杠在 JSON/前端里多次转义很脆。
                "db": self.target,
                "backend": self.backend,
                "rows": len(all_rows),
                "priced_models": priced,
                "unpriced_today": today["unpriced_rows"],
            },
        }
        payload.update(_health(today, priced))
        return payload

    def dashboard_payload(self, channel="") -> dict:
        now = time.time()
        midnight = clock.day_start(now)
        rows = [r for r in self._rows() if r["at"] >= midnight]
        today = self._summary(rows)

        def group(key_name: str) -> list:
            bucket: dict = {}
            for r in rows:
                key = r[key_name] or "—"
                item = bucket.setdefault(key, {"key": key, "requests": 0, "tokens": 0,
                                               "cost_cny": 0.0, "errors": 0, "priced": 0,
                                               "ok": 0, "prompt": 0, "cached": 0,
                                               "latency_ms": 0.0, "latency_n": 0})
                item["requests"] += 1
                item["tokens"] += _int(r["total_tokens"])
                item["errors"] += 0 if r["ok"] else 1
                item["ok"] += 1 if r["ok"] else 0
                item["prompt"] += _int(r["prompt_tokens"])
                item["cached"] += _int(r["cached_tokens"])
                if r["elapsed_ms"] is not None:
                    item["latency_ms"] += float(r["elapsed_ms"])
                    item["latency_n"] += 1
                if r["cost_cny"] is not None:
                    item["cost_cny"] += r["cost_cny"]
                    item["priced"] += 1
            total_cost = sum(i["cost_cny"] for i in bucket.values()) or 0
            out = []
            for item in sorted(bucket.values(), key=lambda i: -i["requests"]):
                cost = round(item["cost_cny"], 6) if item["priced"] else None
                # 未计价的行占比无意义：给 None 而不是 0，否则“零费用”会变成“占比 0%”。
                share = (round(item["cost_cny"] / total_cost, 4)
                         if total_cost and item["priced"] else None)
                # 每档都带自己的成功率/缓存命中/均单：账页「按渠道」不再只画一根费用条。
                out.append({
                    "key": item["key"], "requests": item["requests"],
                    "ok": item["ok"], "errors": item["errors"],
                    "success_rate": (round(item["ok"] / item["requests"] * 100, 2)
                                     if item["requests"] else None),
                    "tokens": item["tokens"],
                    "prompt_tokens": item["prompt"],
                    "cached_tokens": item["cached"],
                    "cache_hit_pct": (round(item["cached"] / item["prompt"] * 100, 2)
                                      if item["prompt"] else None),
                    "avg_ms": (round(item["latency_ms"] / item["latency_n"], 1)
                               if item["latency_n"] else None),
                    "avg_cny": (round(cost / item["priced"], 6)
                                if cost and item["priced"] else None),
                    "cost_cny": cost, "share": share,
                })
            return out

        ready = 1 if channel else 0
        priced = len(self.pricing(channel))
        return {
            "generated_at": int(now),
            "iso": _iso(now),
            "source": "local-ledger",
            "today": {
                "requests": today["requests"],
                "errors": today["errors"],
                "success_rate": today["success_rate"],
                "cost_cny": today["cost_cny"],
                "credit": None,
                "latency": today["latency"],
                "tokens": today["tokens"],
                # 令牌口径与 status.traffic.today 同源，概览的 TOKEN 表与缓存命中直接读这四个字段。
                "prompt_tokens": today["prompt_tokens"],
                "cached_tokens": today["cached_tokens"],
                "completion_tokens": today["completion_tokens"],
                "cache_hit_pct": today["cache_hit_pct"],
                "unpriced_rows": today["unpriced_rows"],
            },
            "health_score": _health(today, priced)["health_score"],
            "health": {
                "channels_ready": ready,
                "channels_total": ready,
                "accounts_active": 0,
                "accounts_total": 0,
                "score": _health(today, priced)["health_score"],
            },
            "models": group("model"),
            "channels": group("channel"),
        }

    def routes_payload(self, channel="", model_upstream="", proxy_url="",
                       model_upstreams=None) -> dict:
        egress = {"upstream": model_upstream} if model_upstream else {}
        egress.update({"proxy_url": proxy_url or "", "proxy_enabled": bool(proxy_url)})
        return {"data": {
            "generated_at": int(time.time()),
            "source": "local-ledger",
            "account_concurrency": 0,
            "channel_sticky": ([{"channel": channel, "sticky": True}] if channel else []),
            "switches": [],
            "bindings": 0,
            "egress": egress,
            "model_upstreams": model_upstreams or {},
        }}

    def catalog_payload(self, models: list, channel="", disabled=None,
                        channel_enabled=True, disabled_map=None,
                        enabled_map=None) -> dict:
        """目录取自各模型上游；价目缺失的行 `rate` 为 null，不猜。

        disabled = 默认渠道明确停用的模型 id；disabled_map/enabled_map 给
        额外渠道（如 ollama）各自的开关状态。
        """
        table = self.pricing(channel)
        tables = {channel: table}
        off = set(disabled or ())
        items = []
        for m in models or []:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if not mid:
                continue
            meta = (m.get("metadata") or {}) if isinstance(m, dict) else {}
            model_channel = str((m.get("channel") if isinstance(m, dict) else "") or channel)
            if model_channel not in tables:
                tables[model_channel] = self.pricing(model_channel)
            price = tables[model_channel].get(mid)
            model_off = set((disabled_map or {}).get(model_channel, ())) \
                if model_channel != channel else off
            model_on = (enabled_map or {}).get(model_channel,
                                               channel_enabled if model_channel == channel else True)
            items.append({
                "id": mid,
                "name": mid,
                "vendor": (m.get("owned_by") if isinstance(m, dict) else "") or "—",
                "channel": model_channel,
                "context": meta.get("context_limit"),
                "efforts": [],
                "reasoning": bool(meta.get("capability") == "standard"),
                "vision": "image" in ((m.get("architecture") or {}).get("input_modalities") or [])
                          if isinstance(m, dict) else False,
                "tools": False,
                "enabled": (bool(m.get("enabled", True)) if isinstance(m, dict) else True)
                           and mid not in model_off and bool(model_on),
                "free": False,
                "decision": bool(m.get("decision")) if isinstance(m, dict) else False,
                "endpoint": (m.get("endpoint") if isinstance(m, dict) else "") or "",
                "billing_basis": (m.get("billing_basis") if isinstance(m, dict) else "") or "",
                "rate": price,
                # 价目行的抓取/录入时间：面板只展示这一个时间，不展示来源长篇。
                "price_checked_at": (price or {}).get("checked_at") or "",
                "pricing_mode": "mtok" if price else "unpriced",
            })
        return {"models": items, "total": len(items),
                "generated_at": int(time.time()), "source": "local-ledger"}
