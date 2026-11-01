#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_readmodel.py - 本地读模型（替代云端读接口）的口径回归。"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core.local_ledger import LocalLedger  # noqa: E402
from core.local_readmodel import LocalReadModel, READ_ROUTES  # noqa: E402


class TestLocalReadModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.pricing = tmp / "pricing.json"
        self.pricing.write_text(json.dumps({"models": {
            "glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")
        self.ledger = LocalLedger(tmp / "ledger.db", self.pricing)
        self.models = [{"id": "glm-5.3-flash"}, {"id": "mystery-model"}]
        self.rm = LocalReadModel(self.ledger, channel="open-design",
                                 upstream="https://amr-link.example/v1",
                                 models=self.models, token="sk-amr-test1234")
        self.now = int(time.time())
        self.ledger.record(model="glm-5.3-flash", prompt=1000, cached=0, completion=500,
                           at=self.now - 60, ok=True, status=200, elapsed_ms=1200,
                           channel="open-design", rid="r1")
        self.ledger.record(model="glm-5.3-flash", prompt=2000, cached=500, completion=300,
                           at=self.now - 30, ok=False, status=429, elapsed_ms=900,
                           channel="open-design", rid="r2", error="rate limited")

    def test_billing_today_and_share(self):
        b = self.rm.billing_live(self.now)
        self.assertEqual(b["today"]["requests"], 2)
        self.assertEqual(b["today"]["ok"], 1)
        self.assertEqual(len(b["series"]), 30)
        self.assertEqual(b["series"][-1]["n"], 2)          # 最近 60s 内的两次都落在末桶
        self.assertEqual(b["channels"][0]["channel"], "open-design")
        self.assertAlmostEqual(b["channels"][0]["share"], 100.0)
        # 均单只除成功行：失败行不计入分母
        self.assertEqual(b["avg_per_call"]["n"], 1)
        self.assertIsNotNone(b["avg_per_call"]["cny"])
        self.assertIsNone(b["budget"])

    def test_series_bucket_no_negative(self):
        self.ledger.record(model="glm-5.3-flash", prompt=10, completion=5,
                           at=self.now - 7200, ok=True, status=200)
        s = self.rm.billing_live(self.now)["series"]
        self.assertEqual(sum(x["n"] for x in s), 2)        # 2 小时前的行不进 60min 窗

    def test_errors_only_failed_rows(self):
        e = self.rm.errors("limit=5")
        self.assertEqual(e["total"], 1)
        self.assertEqual(e["rows"][0]["status"], 429)
        self.assertEqual(e["kind_counts"], {"HTTP 429": 1})

    def test_channels_pricing_coverage(self):
        c = self.rm.channels(self.now)
        ch = c["channels"][0]
        self.assertEqual(ch["models"], 2)
        self.assertEqual(ch["priced"], 1)
        self.assertEqual(ch["unpriced"], ["mystery-model"])
        self.assertEqual(ch["accounts"], 0)
        self.assertEqual(ch["today"]["requests"], 2)

    def test_visibility_and_custom_key_masking(self):
        v = self.rm.visibility()
        self.assertEqual([x["channel"] for x in v["channels"]], ["open-design"])
        self.assertFalse(v["channels"][0]["hidden"])
        cu = self.rm.custom()
        item = cu["items"][0]
        self.assertTrue(item["has_key"])
        self.assertNotIn("sk-amr-test1234", json.dumps(cu))  # 明文密钥不出网

    def test_keys_is_bare_array(self):
        # views-vault 的 keys() 直接 .filter，必须是数组而不是 {data:[]}
        self.assertEqual(self.rm.payload("/api/keys"), [])

    def test_cloud_only_endpoints_are_explicitly_empty(self):
        self.assertEqual(self.rm.official_summary("days=1")["channels"], [])
        self.assertEqual(self.rm.pool()["accounts"], [])
        self.assertEqual(self.rm.budgets()["items"], [])
        self.assertFalse(self.rm.cloud_sync(self.now)["enabled"])
        self.assertIsNone(self.rm.subscription()["tier"])
        self.assertEqual(self.rm.accounts_health()["accounts"], [])

    def test_security_audit_scores_findings(self):
        a = self.rm.security_audit(self.now)
        self.assertEqual(a["score"], 100)
        self.assertEqual(a["level"], "PASS")
        self.assertEqual(a["stats"]["keys"], 1)
        bare = LocalReadModel(self.ledger, channel="open-design", upstream="", models=[])
        self.assertEqual(bare.security_audit(self.now)["score"], 70)

    def test_routes_cover_frontend_calls(self):
        for path in ("/api/billing/live", "/api/models/meta", "/api/visibility",
                     "/api/readmodel/channels", "/api/custom", "/api/keys",
                     "/api/budgets", "/api/errors", "/api/cloud/sync",
                     "/api/official/summary", "/api/security/audit",
                     "/api/th/subscription", "/api/accounts/health", "/api/pool"):
            self.assertIn(path, READ_ROUTES)
            self.assertIsNotNone(self.rm.payload(path), path)


if __name__ == "__main__":
    unittest.main()
