#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_model_upstreams.py —— 按模型选择上游与 systemone 改道。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core.model_upstreams import ModelUpstreamRouter  # noqa: E402


class TestModelUpstreams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "model_upstreams.json"
        self.path.write_text(json.dumps({
            "schema": 1,
            "default_channel": "open-design",
            "channels": {
                "open-design": {
                    "base_url": "https://design.example/v1",
                    "token_env": "HUB_MODEL_TOKEN",
                    "pricing_channel": "open-design",
                },
                "ollama": {
                    "base_url": "https://ollama.com/v1",
                    "token_env": "HUB_OLLAMA_TOKEN",
                    "pricing_channel": "ollama",
                    "endpoint": "/v1/systemone",
                    "decision": True,
                    "models": ["clef:27b"],
                    "aliases": {"clef": "clef:27b"},
                    "catalog": [{"id": "clef:27b", "owned_by": "ollama",
                                 "channel": "ollama", "decision": True,
                                 "billing_basis": "input_only"}],
                },
            },
        }), encoding="utf-8")
        self.router = ModelUpstreamRouter(
            self.path, default_upstream="https://fallback.example/v1",
            default_channel="open-design", default_token="dt",
            environ={"HUB_MODEL_TOKEN": "mt", "HUB_OLLAMA_TOKEN": "ot"})

    def test_clef_aliases_route_to_systemone(self):
        for alias in ("clef:27b", "clef", "ollama/clef:27b",
                      "custom/ollama/clef:27b"):
            route = self.router.resolve(alias, "/v1/chat/completions")
            self.assertEqual(route.channel, "ollama", alias)
            self.assertEqual(route.request_model, "clef:27b", alias)
            self.assertEqual(route.path, "/v1/systemone", alias)
            self.assertTrue(route.decision, alias)
            self.assertEqual(route.url, "https://ollama.com/v1/systemone", alias)
            self.assertEqual(route.token, "ot", alias)

    def test_unknown_model_keeps_default_upstream(self):
        route = self.router.resolve("glm-5.3", "/v1/chat/completions")
        self.assertEqual(route.channel, "open-design")
        self.assertFalse(route.decision)
        self.assertEqual(route.request_model, "glm-5.3")
        self.assertEqual(route.url, "https://design.example/v1/chat/completions")
        self.assertEqual(route.token, "mt")

    def test_summary_never_exposes_tokens(self):
        summary = self.router.summary()
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("mt", blob)
        self.assertNotIn("ot", blob)
        channels = {row["channel"]: row for row in summary["channels"]}
        self.assertTrue(channels["ollama"]["token_present"])
        self.assertEqual(channels["ollama"]["models"], ["clef:27b"])

    def test_catalog_marks_decision_and_billing_basis(self):
        row = self.router.catalog_models()[0]
        self.assertEqual(row["id"], "clef:27b")
        self.assertEqual(row["channel"], "ollama")
        self.assertTrue(row["decision"])
        self.assertEqual(row["endpoint"], "/v1/systemone")
        self.assertEqual(row["billing_basis"], "input_only")


if __name__ == "__main__":
    unittest.main()
