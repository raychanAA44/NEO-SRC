#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_legacy_price_migration.py —— 旧渠道价目迁移的取舍与幂等。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))
sys.path.insert(0, str(ROOT_DIR / "编码区" / "scripts"))

from core import pricing  # noqa: E402
import migrate_legacy_prices as migration  # noqa: E402


class TestLegacyPriceMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "custom_prices.json"
        self.target = Path(self.tmp.name) / "pricing.json"
        self.doc = {
            "usd_cny": 6.72,
            "upstreams": {
                "ollama": {
                    "peak_rule": {"kind": "utc_weekday_window",
                                  "start_hour": 12, "end_hour": 18},
                    "display_discount": 0.334,
                    "models": {
                        "deepseek-v4.1-flash": {
                            "in": 0.3, "cache": 0.006, "out": 1.2,
                            "off_in": 0.15, "off_cache": 0.003, "off_out": 0.6,
                            "source": "legacy", "checked_at": "2026-10-16",
                        },
                        "clef:27b": {"in": 0.24},
                    },
                },
                "tokenharbor": {
                    "billing_kind": "subscription",
                    "plan_book": "tokenharbor",
                    "models": {
                        "claude-sonnet-4.6": {
                            "in": 3.0, "cache": 0.3, "out": 15.0,
                        },
                    },
                },
                "open-design": {
                    "models": {
                        "existing-model": {"in": 1.0, "cache": 0.1, "out": 2.0},
                    },
                },
            },
        }
        self.source.write_text(json.dumps(self.doc, ensure_ascii=False), encoding="utf-8")
        self.target.write_text(json.dumps({
            "schema": 1, "unit": "USD_per_mtok", "usd_cny": 6.72,
            "upstreams": {"open-design": {"models": {
                "existing-model": {"in": 1.0, "cache": 0.1, "out": 2.0},
            }}},
        }), encoding="utf-8")

    def test_partial_rows_are_skipped_and_values_preserved(self):
        payload, skipped, stats = migration.build_payload(
            self.doc, self.source, ("ollama", "tokenharbor"))
        self.assertEqual(stats["ollama"]["source_rows"], 2)
        self.assertEqual(stats["ollama"]["imported_rows"], 1)
        self.assertEqual(skipped["ollama"][0]["model"], "clef:27b")
        self.assertEqual(skipped["ollama"][0]["missing"], ["cache", "out"])
        self.assertNotIn("open-design", payload["upstreams"])
        self.assertEqual(payload["upstreams"]["tokenharbor"]["discount"], 1.0)

        candidate = pricing.canonical_doc(payload)
        parity = migration.compare_rows(candidate, self.doc, ("ollama", "tokenharbor"))
        self.assertTrue(parity["ok"], parity["mismatches"])
        self.assertEqual(parity["checked_rows"], 2)

    def test_dry_run_then_apply_is_idempotent(self):
        payload, _, _ = migration.build_payload(
            self.doc, self.source, ("ollama", "tokenharbor"))
        before = self.target.read_text(encoding="utf-8")
        dry = pricing.import_payload(payload, self.target, mode="merge", dry_run=True)
        self.assertEqual(dry["diff"]["added_count"], 2)
        self.assertEqual(dry["summary"]["models"], 3)
        self.assertEqual(self.target.read_text(encoding="utf-8"), before)

        done = pricing.import_payload(payload, self.target, mode="merge")
        self.assertEqual(done["diff"]["added_count"], 2)
        again = pricing.import_payload(payload, self.target, mode="merge", dry_run=True)
        self.assertEqual(again["diff"]["added_count"], 0)
        self.assertEqual(again["diff"]["updated_count"], 0)
        self.assertEqual(again["diff"]["unchanged"], 3)


if __name__ == "__main__":
    unittest.main()
