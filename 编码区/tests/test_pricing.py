#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_pricing.py —— 渠道化价目表的解析、计价与录入校验。"""

import calendar
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core import pricing  # noqa: E402


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pricing.json"
        self.doc = {
            "schema": 1,
            "unit": "USD_per_mtok",
            "usd_cny": 6.72,
            "upstreams": {
                "open-design": {
                    "discount": 0.5,
                    "peak_rule": {"kind": "utc_weekday_window",
                                  "start_hour": 12, "end_hour": 18},
                    "models": {
                        "deepseek-v4-pro": {
                            "in": 1.32, "cache": 0.044, "out": 3.96,
                            "off_in": 0.66, "off_cache": 0.022, "off_out": 1.98,
                        }
                    },
                },
                "other": {
                    "models": {
                        "deepseek-v4-pro": {"in": 2.0, "cache": 0.2, "out": 8.0},
                    }
                },
            },
        }
        self.path.write_text(json.dumps(self.doc, ensure_ascii=False), encoding="utf-8")

    def test_channel_specific_rate(self):
        doc = pricing.load_doc(self.path)
        peak = calendar.timegm((2026, 10, 26, 13, 0, 0))
        a = pricing.rate_of(doc, "open-design", "deepseek-v4-pro", ts=peak)
        b = pricing.rate_of(doc, "other", "deepseek-v4-pro", ts=peak)
        self.assertAlmostEqual(a["in"], 4.4352, places=9)
        self.assertAlmostEqual(b["in"], 13.44, places=9)
        self.assertNotEqual(a["channel"], b["channel"])

    def test_fx_cache_and_vendor_prefix(self):
        doc = pricing.load_doc(self.path)
        peak = calendar.timegm((2026, 10, 26, 13, 0, 0))
        parts = pricing.cost_parts(doc, "open-design", "vendor/deepseek-v4-pro",
                                   1000, 400, 500, ts=peak)
        # 工作日 13:00 UTC（峰）：600×1.32 + 400×0.044 + 500×3.96，再 ×6.72×0.5
        expected = (600 * 1.32 + 400 * 0.044 + 500 * 3.96) * 6.72 * 0.5 / 1_000_000
        self.assertAlmostEqual(parts["in"] + parts["cache"] + parts["out"], expected, places=9)

    def test_rows_expose_cny_alias_for_legacy_ui(self):
        row = pricing.rows(self.path, "open-design")[0]
        self.assertAlmostEqual(row["cny_in"], 4.4352, places=9)
        self.assertAlmostEqual(row["in_cny"], row["cny_in"], places=12)
        self.assertAlmostEqual(row["out_cny"], row["cny_out"], places=12)

    def test_peak_and_off_peak(self):
        doc = pricing.load_doc(self.path)
        peak = calendar.timegm((2026, 10, 26, 13, 0, 0))   # Monday 13:00 UTC
        off = calendar.timegm((2026, 10, 25, 13, 0, 0))    # Sunday
        a = pricing.rate_of(doc, "open-design", "deepseek-v4-pro", ts=peak)
        b = pricing.rate_of(doc, "open-design", "deepseek-v4-pro", ts=off)
        self.assertTrue(a["peak"])
        self.assertFalse(b["peak"])
        self.assertAlmostEqual(a["in"], 4.4352, places=9)
        self.assertAlmostEqual(b["in"], 2.2176, places=9)

    def test_legacy_flat_table_still_reads_as_cny(self):
        legacy = Path(self.tmp.name) / "legacy.json"
        legacy.write_text(json.dumps({"models": {
            "glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")
        doc = pricing.load_doc(legacy)
        self.assertEqual(doc["unit"], "CNY_per_mtok")
        rate = pricing.rate_of(doc, "open-design", "glm-5.3-flash", ts=0)
        self.assertAlmostEqual(rate["in"], 0.7, places=9)

    def test_import_rows_dry_run_then_merge(self):
        payload = {
            "channel": "open-design",
            "unit": "USD_per_mtok",
            "rows": [{"model": "new-model", "in": 0.2, "cache": 0.02, "out": 0.8}],
        }
        before = self.path.read_text(encoding="utf-8")
        dry = pricing.import_payload(payload, self.path, mode="merge", dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["diff"]["added_count"], 1)
        # dry-run 的 summary 必须是「写入后」的样子，而不是当前文件。
        self.assertEqual(dry["summary"]["models"], 3)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

        done = pricing.import_payload(payload, self.path, mode="merge")
        self.assertFalse(done["dry_run"])
        doc = pricing.load_doc(self.path)
        self.assertIn("new-model", doc["upstreams"]["open-design"]["models"])
        # 既有渠道/模型未被删
        self.assertIn("deepseek-v4-pro", doc["upstreams"]["open-design"]["models"])
        self.assertIn("other", doc["upstreams"])

    def test_import_replace_removes_old_rows(self):
        payload = {
            "channel": "fresh",
            "rows": [{"model": "only", "in": 1, "cache": 0.1, "out": 2}],
        }
        pricing.import_payload(payload, self.path, mode="replace")
        doc = pricing.load_doc(self.path)
        self.assertEqual(list(doc["upstreams"]), ["fresh"])
        self.assertIn("only", doc["upstreams"]["fresh"]["models"])

    def test_invalid_missing_cache_is_rejected_without_write(self):
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(pricing.PricingError):
            pricing.import_payload(
                {"channel": "x", "rows": [{"model": "bad", "in": 1, "out": 2}]},
                self.path, mode="merge")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_import_without_fx_keeps_existing_fx(self):
        pricing.import_payload(
            {"channel": "open-design", "rows": [{"model": "m", "in": 1, "cache": 0.1, "out": 2}]},
            self.path, mode="merge")
        doc = pricing.load_doc(self.path)
        self.assertEqual(doc["usd_cny"], 6.72)

    def test_summary_reports_channels_and_models(self):
        summary = pricing.summary(self.path)
        self.assertEqual(summary["channels"], ["open-design", "other"])
        self.assertEqual(summary["models"], 2)
        self.assertEqual(summary["error"], "")

    def test_repo_ollama_partial_rows_are_priced_with_marked_fallback(self):
        """三行缺价必须真正可计价，且 cache=in / out=0 的来源标记随行透出。"""
        path = ROOT_DIR / "运维区" / "configs" / "pricing.json"
        doc = pricing.load_doc(path)
        models = doc["upstreams"]["ollama"]["models"]
        peak = calendar.timegm((2026, 10, 26, 13, 0, 0))  # Monday 13:00 UTC
        expected = {
            "clef:27b": (0.24, 0.0),
            "nemotron-3-nano:30b": (0.06, 0.24),
            "mistral-large-3:675b": (0.5, 1.5),
        }
        for model, (usd_in, usd_out) in expected.items():
            row = models[model]
            self.assertEqual(row["cache"], row["in"])
            self.assertEqual(row["cache_source"], "input_fallback")
            self.assertEqual(row["out"], usd_out)
            rate = pricing.rate_of(doc, "ollama", model, ts=peak)
            self.assertAlmostEqual(rate["in"], usd_in * 6.72, places=9)
            self.assertAlmostEqual(rate["cache"], usd_in * 6.72, places=9)
            self.assertAlmostEqual(rate["out"], usd_out * 6.72, places=9)

        clef = models["clef:27b"]
        self.assertEqual(clef["out_source"], "official_input_only")
        self.assertEqual(clef["billing_basis"], "input_only")
        parts = pricing.cost_parts(doc, "ollama", "clef:27b",
                                   prompt=1000, cached=400, completion=500, ts=peak)
        self.assertAlmostEqual(parts["cache"], 400 * 0.24 * 6.72 / 1_000_000, places=12)
        self.assertAlmostEqual(parts["out"], 0.0, places=12)

        nano = pricing.cost_parts(doc, "ollama", "nemotron-3-nano:30b",
                                  prompt=1000, cached=400, completion=500, ts=peak)
        expected_nano = (600 * 0.06 + 400 * 0.06 + 500 * 0.24) * 6.72 / 1_000_000
        self.assertAlmostEqual(nano["in"] + nano["cache"] + nano["out"], expected_nano, places=12)

        summary = pricing.summarize_doc(doc)
        self.assertGreaterEqual(summary["fallback_rows"], 3)
        self.assertEqual(summary["pricing_policy"]["cache_missing"], "input_fallback")
        row = next(item for item in pricing.rows(path)
                   if item["channel"] == "ollama" and item["model"] == "clef:27b")
        self.assertEqual(row["cache_source"], "input_fallback")
        self.assertEqual(row["out_source"], "official_input_only")


if __name__ == "__main__":
    unittest.main()
