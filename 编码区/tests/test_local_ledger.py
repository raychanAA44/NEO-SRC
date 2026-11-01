#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_ledger.py - 8888 自带账本的记账口径与读模型回归。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core import pricing  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402


class TestLocalLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "ledger.db"
        self.pricing = self.dir / "pricing.json"
        self.pricing.write_text(json.dumps({"models": {
            "glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")
        self.ledger = LocalLedger(self.db, self.pricing)

    # ── 分窗用量 ──────────────────────────────────────────
    def test_window_stats_split_by_cst_windows(self):
        """今日 / 近 5 小时 / 本周 / 本月 各自独立计数，不拿今日冒充全窗。"""
        now = time.time()
        self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10, ok=True,
                           channel="demo", at=now - 60)
        self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10, ok=True,
                           channel="demo", at=now - 6 * 3600)
        self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10, ok=True,
                           channel="other", at=now - 6 * 3600)
        self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10, ok=True,
                           channel="demo", at=now - 40 * 86400)
        got = self.ledger.window_stats("demo", now=now)
        self.assertEqual(sorted(got), ["h5", "month", "today", "week"])
        self.assertEqual(got["h5"]["requests"], 1, "6 小时前那笔不在近 5 小时内")
        # 6 小时前可能还在今天（早于 06:00 CST 就不是）；只断言它比 5 小时窗口多。
        self.assertGreaterEqual(got["today"]["requests"], got["h5"]["requests"])
        self.assertGreater(got["today"]["cost_cny"], 0)
        self.assertGreaterEqual(got["month"]["requests"], got["week"]["requests"])
        self.assertEqual(got["today"]["source"], "local")
        self.assertEqual(self.ledger.window_stats("nobody", now=now)["today"]["requests"], 0)

    def _payload(self, **usage):
        return json.dumps({"model": "glm-5.3-flash", "usage": usage}).encode("utf-8")

    # ── 计价 ────────────────────────────────────────────────
    def test_cost_uses_three_tiers(self):
        # 输入 1000 里 400 是缓存：600×0.7 + 400×0.07 + 500×2.8 = 1848 元/百万
        row = self.ledger.record_from_payload(
            self._payload(prompt_tokens=1000, prompt_tokens_details={"cached_tokens": 400},
                          completion_tokens=500, total_tokens=1500),
            request_model="auto/code", status=200, ok=True, elapsed_ms=12.5,
            rid="r1", channel="open-design")
        self.assertAlmostEqual(row["cost_cny"], 0.001848, places=9)

    def test_cache_saved_matches_price_gap(self):
        self.ledger.record_from_payload(
            self._payload(prompt_tokens=1000, prompt_tokens_details={"cached_tokens": 400},
                          completion_tokens=0, total_tokens=1000), status=200, ok=True)
        row = self.ledger.usage_payload(limit=5)["rows"][0]
        self.assertAlmostEqual(row["_cost"]["saved_cny"], 0.000252, places=9)

    def test_systemone_input_only_usage_is_billed_without_output(self):
        pricing_path = self.dir / "pricing-input-only.json"
        pricing_path.write_text(json.dumps({
            "schema": 1, "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {"ollama": {"models": {"clef:27b": {
                "in": 0.24, "cache": 0.24, "out": 0.0,
                "cache_source": "input_fallback",
                "out_source": "official_input_only",
                "billing_basis": "input_only",
            }}}},
        }), encoding="utf-8")
        ledger = LocalLedger(self.dir / "input-only.db", pricing_path)
        payload = json.dumps({
            "model": "clef:27b",
            "answers": {"urgent": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }).encode("utf-8")
        row = ledger.record_from_payload(
            payload, request_model="clef", model_override="clef:27b",
            status=200, ok=True, rid="clef-r1", channel="ollama")
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["completion_tokens"], 0)
        self.assertEqual(row["total_tokens"], 100)
        self.assertAlmostEqual(row["cost_cny"], 0.000161, places=6)
        item = ledger.usage_payload(limit=1)["rows"][0]
        self.assertEqual(item["input_tokens"], 100)
        self.assertEqual(item["output_tokens"], 0)
        self.assertEqual(item["billing"]["basis"], "input_only")
        self.assertEqual(item["billing"]["out_source"], "official_input_only")
        self.assertEqual(item["billing"]["rate_out_cny"], 0.0)
        self.assertEqual(item["billing"]["output_cost_cny"], 0.0)

    def test_unpriced_model_gives_none_not_zero(self):
        self.ledger.record_from_payload(
            json.dumps({"model": "mystery", "usage": {"prompt_tokens": 10,
                                                      "completion_tokens": 5}}).encode(),
            status=200, ok=True)
        row = self.ledger.usage_payload(limit=5)["rows"][0]
        self.assertIsNone(row["_cost"]["cny"])
        self.assertEqual(row["_cost"]["bill_reason"], "价目未配置")
        self.assertFalse(row["_cost"]["billable"])
        self.assertIsNone(row["_cost"]["saved_cny"])

    def test_backfill_only_fills_empty_cost(self):
        """补算只动空金额：已计价的行绝不能披改写。"""
        priced = self.ledger.record(model="glm-5.3-flash", prompt=1000, completion=1000,
                                    status=200, ok=True)
        # 模拟“价目/前缀修正前入库”的空金额行
        orphan = self.ledger.record(model="anthropic/glm-5.3-flash", prompt=1000,
                                    completion=1000, status=200, ok=True)
        self.assertIsNotNone(orphan["cost_cny"])
        from contextlib import closing
        with closing(self.ledger._store.connect()) as con:
            con.execute("UPDATE usage SET cost_cny = NULL, cost_reason = '价目未配置'"
                        " WHERE id = ?", (2,))
            con.execute("UPDATE usage SET cost_cny = 9.99 WHERE id = ?", (1,))
            con.commit()
        dry = self.ledger.backfill_unpriced()
        self.assertFalse(dry["applied"])
        self.assertEqual(dry["priced"], 1)
        self.assertEqual(dry["updated"], 0)
        out = self.ledger.backfill_unpriced(apply=True)
        self.assertEqual(out["updated"], 1)
        rows = {r["id"]: r for r in self.ledger.usage_payload(10)["rows"]}
        # 1000×0.7 + 1000×2.8 = 3500 微元
        self.assertAlmostEqual(rows[2]["_cost"]["cny"], 0.0035, places=9)
        self.assertEqual(rows[1]["_cost"]["cny"], 9.99)  # 原金额未被动

    def test_vendor_prefixed_model_still_priced(self):
        """`anthropic/claude-haiku-5.5` 与 `claude-haiku-5.5` 同一条模型，不能因写法不同变成未计价。"""
        price = {"in": 0.5, "cache": 0.05, "out": 1.5}
        self.pricing.write_text(json.dumps({"models": {"claude-haiku-5.5": price}}), encoding="utf-8")
        row = self.ledger.record(model="anthropic/claude-haiku-5.5", prompt=1000,
                                 completion=100, status=200, ok=True)
        self.assertAlmostEqual(row["cost_cny"], 0.00065, places=9)
        self.assertEqual(row["cost_reason"], "")

    def test_channel_specific_cost_uses_selected_channel(self):
        """同一模型不同渠道不同价：记账必须用本笔的 channel，不能只按模型名。"""
        self.pricing.write_text(json.dumps({
            "schema": 1, "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {
                "open-design": {"models": {"deepseek-v4-pro": {
                    "in": 1.32, "cache": 0.044, "out": 3.96}}},
                "other": {"models": {"deepseek-v4-pro": {
                    "in": 2.0, "cache": 0.2, "out": 8.0}}},
            }}), encoding="utf-8")
        row = self.ledger.record(model="deepseek-v4-pro", prompt=1000,
                                 cached=0, completion=500, status=200, ok=True,
                                 channel="open-design")
        # (1000×1.32 + 500×3.96) × 6.72 / 1e6
        self.assertAlmostEqual(row["cost_cny"], 0.022176, places=9)

    def test_custom_channel_alias_is_priced(self):
        """custom: 前缀是 UI 叫法，价目落盘后去前缀，账本按同一渠道计价。"""
        self.pricing.write_text(json.dumps({
            "schema": 1, "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {"custom:acme": {"models": {
                "acme-1": {"in": 1.0, "cache": 0.1, "out": 2.0}}}},
        }), encoding="utf-8")
        self.assertIn("acme", self.ledger.pricing_doc()["upstreams"])
        row = self.ledger.record(model="acme-1", prompt=1000, completion=500,
                                 status=200, ok=True, channel="custom:acme")
        self.assertAlmostEqual(row["cost_cny"], 0.01344, places=9)

    def test_pricing_import_dry_run_then_apply(self):
        """录入接口：默认可预览；apply 后原子落盘并立即参与计价；坏行不写。"""
        self.pricing.write_text(json.dumps({
            "schema": 1, "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {"open-design": {"models": {
                "old-model": {"in": 0.5, "cache": 0.05, "out": 1.5}}}},
        }), encoding="utf-8")
        payload = {"channel": "open-design", "rows": [
            {"model": "new-model", "in": 1.0, "cache": 0.1, "out": 2.0}]}
        dry = self.ledger.import_pricing(payload, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["diff"]["added_count"], 1)
        self.assertNotIn("new-model", self.ledger.pricing())
        applied = self.ledger.import_pricing(payload, actor="test")
        self.assertFalse(applied["dry_run"])
        self.assertEqual(applied["diff"]["added_count"], 1)
        table = self.ledger.pricing()
        self.assertIn("old-model", table)  # merge 不删旧模型
        self.assertIn("new-model", table)
        row = self.ledger.record(model="new-model", prompt=1000, completion=500,
                                 status=200, ok=True, channel="open-design")
        self.assertAlmostEqual(row["cost_cny"], 0.01344, places=9)
        with self.assertRaises(ValueError):
            self.ledger.import_pricing({"channel": "open-design", "rows": [
                {"model": "bad-model", "in": 1.0}]})
        self.assertNotIn("bad-model", self.ledger.pricing())

    def test_failure_not_billed(self):
        row = self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10,
                                 status=500, ok=False, error="boom")
        self.assertIsNone(row["cost_cny"])
        self.assertEqual(row["cost_reason"], "失败不计费")
        self.assertFalse(self.ledger.usage_payload(5)["rows"][0]["_cost"]["billable"])

    # ── 回执解析 ────────────────────────────────────────────
    def test_stream_tail_chunk_carries_usage(self):
        sse = (
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            'data: {"model":"glm-5.3-flash","choices":[],"usage":{"prompt_tokens":7,'
            '"completion_tokens":3,"total_tokens":10}}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        row = self.ledger.record_from_payload(sse, request_model="glm-5.3-flash",
                                              status=200, ok=True, stream=True)
        self.assertEqual(row["prompt_tokens"], 7)
        self.assertEqual(row["total_tokens"], 10)
        self.assertEqual(row["stream"], 1)

    def test_model_falls_back_to_request_name(self):
        row = self.ledger.record_from_payload(b"", request_model="auto/code",
                                              status=200, ok=True)
        self.assertEqual(row["model"], "auto/code")
        self.assertEqual(row["total_tokens"], 0)

    # ── 读模型 ──────────────────────────────────────────────
    def test_usage_payload_paging(self):
        for i in range(3):
            self.ledger.record(model="glm-5.3-flash", prompt=10, completion=1,
                               status=200, ok=True, rid="r%d" % i)
        page = self.ledger.usage_payload(limit=2, offset=0)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(page["source"], "local-ledger")
        second = self.ledger.usage_payload(limit=2, offset=2)
        self.assertEqual(len(second["rows"]), 1)

    def test_status_today_and_mix(self):
        self.ledger.record(model="glm-5.3-flash", prompt=100, cached=40, completion=10,
                           status=200, ok=True)
        status = self.ledger.status_payload("open-design")
        today = status["traffic"]["today"]
        self.assertEqual(today["requests"], 1)
        self.assertEqual(today["ok"], 1)
        self.assertEqual(today["mix"], {"prompt": 100, "cached": 40, "completion": 10})
        # 成功率按百分数给（前端 F.pct 不把 0~1 当比例）
        self.assertEqual(today["success_rate"], 100.0)
        self.assertEqual(status["today"]["mix"], today["mix"])
        self.assertEqual(status["ledger"]["priced_models"], 1)
        # 渠道池必须是数组：前端按 name 查条目，给了计数会直接 .find 报错。
        self.assertIsInstance(status["pool"]["channels"], list)
        self.assertEqual(status["pool"]["channels"][0]["name"], "open-design")

    def test_unpriced_rows_ignores_failures(self):
        """失败行本来就没有金额，不能算进“未计价”里报警。"""
        self.ledger.record(model="glm-5.3-flash", prompt=100, completion=10,
                           status=200, ok=True)
        self.ledger.record(model="glm-5.3-flash", prompt=10, completion=1,
                           status=500, ok=False)
        today = self.ledger.status_payload("open-design")["traffic"]["today"]
        self.assertEqual(today["requests"], 2)
        self.assertEqual(today["unpriced_rows"], 0)

    def test_dashboard_groups_and_share(self):
        self.ledger.record(model="glm-5.3-flash", prompt=1000, completion=1000,
                           status=200, ok=True)
        self.ledger.record(model="mystery", prompt=10, completion=1, status=200, ok=True)
        dash = self.ledger.dashboard_payload("open-design")
        by_key = {m["key"]: m for m in dash["models"]}
        self.assertAlmostEqual(by_key["glm-5.3-flash"]["share"], 1.0)
        self.assertIsNone(by_key["mystery"]["cost_cny"])
        self.assertIsNone(by_key["mystery"]["share"])
        self.assertEqual(dash["health"]["channels_ready"], 1)

    def test_catalog_carries_rate_only_when_priced(self):
        models = [
            {"id": "glm-5.3-flash", "owned_by": "vela", "metadata": {"context_limit": 1000000},
             "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "gpt-6-luna", "owned_by": "vela"},
        ]
        cat = self.ledger.catalog_payload(models, "open-design")
        by_id = {m["id"]: m for m in cat["models"]}
        self.assertEqual(cat["total"], 2)
        self.assertEqual(by_id["glm-5.3-flash"]["pricing_mode"], "mtok")
        self.assertTrue(by_id["glm-5.3-flash"]["vision"])
        self.assertEqual(by_id["gpt-6-luna"]["pricing_mode"], "unpriced")
        self.assertIsNone(by_id["gpt-6-luna"]["rate"])

    def test_last_hour_window(self):
        self.ledger.record(model="glm-5.3-flash", prompt=1, completion=1, status=200, ok=True)
        status = self.ledger.status_payload("open-design")
        self.assertEqual(status["traffic"]["last_hour"]["requests"], 1)
        self.assertIsNotNone(status["traffic"]["today"]["cost_cny"])

    # ── TTFT 与延迟分位 ─────────────────────────────────────
    def test_ttft_recorded_and_percentiles(self):
        for ttft, elapsed in ((120.0, 900.0), (300.0, 1400.0), (60.0, 700.0)):
            self.ledger.record(model="glm-5.3-flash", prompt=10, completion=1, status=200,
                               ok=True, ttft_ms=ttft, elapsed_ms=elapsed)
        rows = self.ledger.usage_payload(5)["rows"]
        self.assertEqual(sorted(r["ttft_ms"] for r in rows), [60.0, 120.0, 300.0])
        lat = self.ledger.status_payload("open-design")["traffic"]["today"]["latency"]
        self.assertEqual(lat["count"], 3)
        # 分位算的是总耗时（700/900/1400）；TTFT 另有一组字段
        self.assertEqual(lat["avg_ms"], 1000.0)
        self.assertEqual(lat["p50_ms"], 900.0)
        self.assertEqual(lat["p95_ms"], 1400.0)
        self.assertEqual(lat["ttft_count"], 3)
        self.assertEqual(lat["ttft_avg_ms"], 160.0)
        self.assertEqual(lat["ttft_p50_ms"], 120.0)
        # 概览页的 AVG 读 traffic.today.elapsed_ms_avg
        today = self.ledger.status_payload("open-design")["traffic"]["today"]
        self.assertEqual(today["elapsed_ms_avg"], 1000.0)

    def test_latency_empty_gives_none(self):
        lat = self.ledger.status_payload("open-design")["traffic"]["today"]["latency"]
        self.assertEqual(lat["count"], 0)
        self.assertIsNone(lat["p50_ms"])
        self.assertIsNone(lat["p95_ms"])
        self.assertIsNone(lat["ttft_avg_ms"])

    def test_legacy_db_gets_ttft_column(self):
        """存量账本建表时没有 ttft_ms：打开后就地补列，不能让记账直接报错。"""
        import sqlite3
        legacy = self.dir / "legacy.db"
        con = sqlite3.connect(legacy)
        con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL,"
                    " rid TEXT, model TEXT, channel TEXT, upstream TEXT, prompt_tokens INTEGER,"
                    " cached_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,"
                    " status INTEGER, ok INTEGER, error TEXT, elapsed_ms REAL, cost_cny REAL,"
                    " cost_reason TEXT, client_ip TEXT, stream INTEGER)")
        con.commit()
        con.close()
        led = LocalLedger(legacy, self.pricing)
        row = led.record(model="glm-5.3-flash", prompt=1, completion=1, status=200,
                         ok=True, ttft_ms=88.0)
        self.assertEqual(row["ttft_ms"], 88.0)
        self.assertEqual(led.usage_payload(1)["rows"][0]["ttft_ms"], 88.0)

    def test_shipped_pricing_covers_channels_models(self):
        """仓内价目表必须可解析，且 open-design 行带官方来源、三档价全正。

        用 channel-aware 的 rows()，避免平表按模型名去重后把同名模型互相覆盖。
        行数不写死：官方价目端点增删模型时，这里断言的是“带来源且价为正”，
        而不是把某一时刻的行数固化成不变量。
        """
        path = ROOT_DIR / "运维区" / "configs" / "pricing.json"
        doc = pricing.load_doc(path)
        self.assertEqual(list(doc["upstreams"]), [
            "open-design", "ollama", "ollama-v2", "tokenharbor", "opencode"])
        rows = pricing.rows(path)
        self.assertGreater(len(rows), 100)
        own = [row for row in rows if row["channel"] == "open-design"]
        self.assertGreaterEqual(len(own), 13)
        for row in own:
            for key in ("in", "cache", "out"):
                self.assertGreater(row[key], 0, "%s/%s.%s 价必须为正" % (
                    row["channel"], row["model"], key))
        # 价目必须可追溯到官方端点，而不是手填或按倍率推算。
        for model, entry in doc["upstreams"]["open-design"]["models"].items():
            self.assertTrue(entry.get("source_url"), "%s 缺官方来源" % model)
            self.assertTrue(entry.get("checked_at"), "%s 缺核验日期" % model)
        for row in rows:
            self.assertLessEqual(row["cache"], row["in"],
                                 "%s/%s 缓存价不应高于输入价" % (row["channel"], row["model"]))

    def test_sqlite_store_is_default_backend(self):
        self.assertEqual(self.ledger.backend, "sqlite")
        self.assertTrue(self.ledger.target.endswith("ledger.db"))

    @unittest.skipUnless(os.environ.get("HUB_TEST_MYSQL_LEDGER") == "1",
                         "需要真实 MySQL：HUB_TEST_MYSQL_LEDGER=1")
    def test_mysql_backend_roundtrip(self):
        """真实 MySQL 上的记账与读回（不碰旧账本：每次用临时表）。"""
        from core.local_ledger import _MySQLStore
        cfg = {
            "host": os.environ.get("HUB_LEDGER_MYSQL_HOST", "127.0.0.1"),
            "port": int(os.environ.get("HUB_LEDGER_MYSQL_PORT", "3306")),
            "user": os.environ.get("HUB_LEDGER_MYSQL_USER", "neo_ledger"),
            "password": os.environ.get("HUB_LEDGER_MYSQL_PASSWORD", ""),
            "database": os.environ.get("HUB_LEDGER_MYSQL_DB", "hub_neo"),
            "table": "usage_selftest",
        }
        store = _MySQLStore(cfg)
        led = LocalLedger(pricing_path=self.pricing, store=store)
        led.record(model="glm-5.3-flash", prompt=1000, completion=500, status=200,
                   ok=True, ttft_ms=321.0, elapsed_ms=900.0, stream=True)
        rows = led.usage_payload(5)["rows"]
        self.assertEqual(rows[0]["ttft_ms"], 321.0)
        # 无缓存命中：1000×0.7 + 500×2.8 = 2100 微元
        self.assertAlmostEqual(rows[0]["_cost"]["cny"], 0.0021, places=9)
        self.assertEqual(led.backend, "mysql")
        # 只清行不删表：应用账号没有 DROP 权限（最小权限）。
        with store.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM `usage_selftest`")
            con.commit()

    def test_bad_pricing_file_degrades_to_unpriced(self):
        self.pricing.write_text("{not json", encoding="utf-8")
        row = self.ledger.record(model="glm-5.3-flash", prompt=10, completion=1,
                                 status=200, ok=True)
        self.assertIsNone(row["cost_cny"])
        self.assertEqual(row["cost_reason"], "价目未配置")


class TestReadModelTruth(unittest.TestCase):
    """概览页读的三件事：数据时标、健康分、窗口令牌——必须由账本真数推导。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pricing = self.dir / "pricing.json"
        self.pricing.write_text(json.dumps({"models": {
            "glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")
        self.ledger = LocalLedger(self.dir / "ledger.db", self.pricing)

    def _row(self, status=200, ok=True, rid="r1"):
        body = json.dumps({"model": "glm-5.3-flash", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 400},
        }}).encode("utf-8")
        return self.ledger.record_from_payload(
            body, request_model="glm-5.3-flash", status=status, ok=ok,
            elapsed_ms=100.0, rid=rid, channel="open-design")

    def test_status_carries_iso_and_derived_health(self):
        self._row()
        self._row(status=402, ok=False, rid="r2")
        st = self.ledger.status_payload("open-design")
        self.assertRegex(st["iso"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(st["at"], st["generated_at"])
        # 50% 成功率 → 100 − min(40, 100) = 60 → 降级档
        self.assertEqual(st["health_score"], 60)
        self.assertEqual(st["health_text"], "降级")
        self.assertIn("失败率", " ".join(st["health_reasons"]))

    def test_clean_day_scores_high(self):
        self._row()
        st = self.ledger.status_payload("open-design")
        self.assertEqual(st["health_score"], 100)
        self.assertEqual(st["health_text"], "正常")

    def test_dashboard_today_exposes_token_table_fields(self):
        self._row()
        today = self.ledger.dashboard_payload("open-design")["today"]
        self.assertEqual(today["prompt_tokens"], 1000)
        self.assertEqual(today["cached_tokens"], 400)
        self.assertEqual(today["completion_tokens"], 500)
        # 命中率 = 缓存 /（输入 + 缓存），与 status.traffic.today 同口径
        self.assertAlmostEqual(today["cache_hit_pct"], 28.57, places=2)

    def test_day_window_is_cst_not_host_timezone(self):
        from core import clock
        ts = 1793018963                     # 2026-10-26 20:49 CST
        self.assertEqual(clock.iso(clock.day_start(ts)), "2026-10-26 00:00:00")
        self.assertEqual(clock.day_start(ts) % 86400, 16 * 3600)   # 00:00 CST = 16:00 UTC


if __name__ == "__main__":
    unittest.main()
