#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_daily_report_api.py —— 今日全量聚合报表（纪事信笺数据源）回归。

覆盖：
- `LocalLedger.daily_summary()` 对当日全量行统计，昨日行不入账；
- 渠道 / 模型 / 租户（无 KEY 时按 client_ip）/ 错误分布口径；
- 成本缺价目时给 None 而不是 0；
- `GET /api/reports/today` 返回 5 大账册所需的全部字段，且不依赖 50 条样本。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server  # noqa: E402
from core import clock  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402


def _today_start(now=None):
    """与生产同源：账本的「今日」按 CST 00:00 切，不用宿主机时区。"""
    return clock.day_start(now)


def _today_at(now, ago):
    """今天窗口内的时刻：now-ago，但绝不早于今天 00:00:01。

    回归在零点后运行时，`now - 7200` 会落到昨天，把「今日报表」seed 成空表。
    夹具必须锚在今天的自然日内，而不是「距今 N 秒」。
    """
    return max(_today_start(now) + 1, now - ago)


class TestDailySummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pricing = root / "pricing.json"
        self.pricing.write_text(json.dumps({
            "models": {"glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
                       "mystery-model": {"in": 1.0, "cache": 0.1, "out": 2.0}},
        }), encoding="utf-8")
        self.ledger = LocalLedger(root / "ledger.db", self.pricing)
        now = time.time()
        self.now = now
        self.ledger.record(model="glm-5.3-flash", prompt=1000, cached=200, completion=500,
                           channel="open-design", client_ip="10.0.0.1", ok=True,
                           status=200, elapsed_ms=1200, ttft_ms=300, at=_today_at(now, 7200), rid="r1")
        self.ledger.record(model="glm-5.3-flash", prompt=2000, cached=0, completion=800,
                           channel="open-design", client_ip="10.0.0.2", ok=True,
                           status=200, elapsed_ms=2400, ttft_ms=500, at=_today_at(now, 3600), rid="r2")
        self.ledger.record(model="mystery-model", prompt=500, cached=0, completion=0,
                           channel="ollama", client_ip="10.0.0.1", ok=False,
                           status=429, error="rate limited", elapsed_ms=900,
                           at=_today_at(now, 1800), rid="r3")
        # 昨日行：不应进入今日报表
        self.ledger.record(model="glm-5.3-flash", prompt=999, completion=999,
                           channel="open-design", client_ip="10.0.0.9", ok=True,
                           status=200, at=_today_start(now) - 3600, rid="old")

    def test_executive_totals(self):
        doc = self.ledger.daily_summary()
        ex = doc["executive"]
        self.assertEqual(ex["calls"], 3)
        self.assertEqual(ex["ok"], 2)
        self.assertEqual(ex["errors"], 1)
        self.assertAlmostEqual(ex["success_rate"], 66.67, places=2)
        self.assertEqual(ex["tokens_total"], 1000 + 500 + 2000 + 800 + 500)
        self.assertIsNotNone(ex["cost_cny"])
        self.assertIsNotNone(ex["avg_cost_cny"])
        self.assertEqual(ex["latency"]["ttft_avg_ms"], 400.0)
        self.assertIsNotNone(ex["latency"]["p95_ms"])
        self.assertEqual(doc["date"], clock.date(self.now))

    def test_channel_model_tenant_breakdown(self):
        doc = self.ledger.daily_summary()
        self.assertEqual([row["channel"] for row in doc["channels"]],
                         ["open-design", "ollama"])
        self.assertEqual(doc["channels"][0]["calls"], 2)
        self.assertEqual(doc["channels"][1]["errors"], 1)
        # 失败行不计费：ollama 没有可计价行 → None 而不是 0
        self.assertIsNone(doc["channels"][1]["cost_cny"])

        models = doc["models"]
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["calls"], 2)
        self.assertIn("channel", models[0])

        self.assertEqual(doc["tenants"]["kind"], "client_ip")
        self.assertEqual(len(doc["tenants"]["rows"]), 2)
        busiest = doc["tenants"]["rows"][0]
        self.assertEqual(busiest["tenant"], "10.0.0.1")
        self.assertEqual(busiest["calls"], 2)

    def test_incidents(self):
        inc = self.ledger.daily_summary()["incidents"]
        self.assertEqual(inc["error_rows"], 1)
        self.assertEqual(inc["status_counts"], [{"status": 429, "count": 1}])
        self.assertEqual(inc["cooldown_signals"], 1)
        self.assertEqual(len(inc["recent_errors"]), 1)
        self.assertEqual(inc["recent_errors"][0]["channel"], "ollama")


class TestDailyReportHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        pricing = root / "pricing.json"
        pricing.write_text(json.dumps({
            "models": {"glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8}},
        }), encoding="utf-8")
        self.ledger = LocalLedger(root / "ledger.db", pricing)
        now = time.time()
        self.ledger.record(model="glm-5.3-flash", prompt=1000, cached=100,
                           completion=500, channel="open-design",
                           client_ip="10.1.1.1", ok=True, status=200,
                           elapsed_ms=1100, ttft_ms=280, at=_today_at(now, 600), rid="x1")
        self.old_ledger = server.LEDGER
        self.old_control = server.CONTROL
        self.old_quota = server.channel_quota_map
        server.LEDGER = self.ledger
        server.CONTROL = LocalControl(root / "control.json", default_channel="open-design")
        server.channel_quota_map = lambda *a, **k: {}
        server.reset_budget_cache()
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HubNeoHandler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.LEDGER = self.old_ledger
        server.CONTROL = self.old_control
        server.channel_quota_map = self.old_quota
        server.reset_budget_cache()

    def test_report_payload_has_all_ledgers(self):
        with urllib.request.urlopen(self.base + "/api/reports/today", timeout=10) as res:
            self.assertEqual(res.status, 200)
            doc = json.loads(res.read().decode("utf-8"))
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["dispatch_no"], clock.date())
        self.assertEqual(doc["port"], server.PORT)
        for key in ("executive", "reconciliation", "models", "tenants", "incidents", "budgets"):
            self.assertIn(key, doc)
        self.assertEqual(doc["executive"]["calls"], 1)
        self.assertEqual(doc["reconciliation"]["total"], 1)
        self.assertEqual(doc["reconciliation"]["rows"][0]["verdict"], "no_official")
        self.assertEqual(doc["tenants"]["rows"][0]["tenant"], "10.1.1.1")
        self.assertEqual(doc["budgets"]["cap_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
