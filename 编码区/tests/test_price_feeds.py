#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_price_feeds.py —— 官方价目抓取器：只测解析（离线，零网络）。

覆盖口径：
- Ollama `(Off-Peak)` 行落到 `off_*`，基准行进 `in/cache/out`；
- OpenCode 先用「显示名 → model id」映射再落价，峰谷与分档变体折叠；
- `-` / 空不当 0；
- `rows_of` 产出 `apply_prices` 能吃的形状。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import price_feeds  # noqa: E402


OLLAMA_HTML = """
<table><thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead>
<tbody>
<tr><td>glm-5.3-flash</td><td>$0.30</td><td>$0.03</td><td>$1.20</td></tr>
<tr><td>glm-5.3-flash (Off-Peak)</td><td>$0.15</td><td>$0.015</td><td>$0.60</td></tr>
<tr><td>no-price</td><td>-</td><td>-</td><td>-</td></tr>
</tbody></table>
"""

OPENCODE_HTML = """
<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>
<tr><td>GLM-5.3-Flash</td><td>glm-5.3-flash</td><td>/v1</td></tr>
<tr><td>Qwen3-Max</td><td>qwen3-max</td><td>/v1</td></tr>
</table>
<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>
<tr><td>GLM-5.3-Flash</td><td>$0.15</td><td>$0.60</td><td>$0.003</td><td>$0.10</td></tr>
<tr><td>Qwen3-Max</td><td>$1.00</td><td>$4.00</td><td>$0.02</td><td>$0.50</td></tr>
</table>
"""


class TestOllama(unittest.TestCase):
    def test_off_peak_folds_into_same_model(self):
        out = price_feeds.parse_ollama(OLLAMA_HTML)
        self.assertTrue(out["ok"], out["error"])
        row = out["models"]["glm-5.3-flash"]
        self.assertEqual((row["in"], row["cache"], row["out"]), (0.30, 0.03, 1.20))
        self.assertEqual((row["off_in"], row["off_cache"], row["off_out"]), (0.15, 0.015, 0.60))
        self.assertNotIn("glm-5.3-flash (Off-Peak)", out["models"])

    def test_dash_is_missing_not_zero(self):
        out = price_feeds.parse_ollama(OLLAMA_HTML)
        self.assertNotIn("no-price", out["models"])


class TestOpenCode(unittest.TestCase):
    def test_display_name_maps_to_model_id(self):
        out = price_feeds.parse_opencode(OPENCODE_HTML)
        self.assertTrue(out["ok"], out["error"])
        self.assertIn("glm-5.3-flash", out["models"])
        self.assertNotIn("GLM-5.3-Flash", out["models"])
        self.assertEqual(out["models"]["glm-5.3-flash"]["cache"], 0.003)

    def test_cached_write_not_written(self):
        out = price_feeds.parse_opencode(OPENCODE_HTML)
        self.assertNotIn("cache_write", out["models"]["qwen3-max"])


class TestOpenDesign(unittest.TestCase):
    def test_parses_pricing_and_long_context(self):
        doc = {"data": [
            {"model": "deepseek-v4-flash",
             "pricing": {"input": 0.44, "output": 1.32, "cacheRead": 0.014}},
            {"model": "claude-haiku-5.5",
             "pricing": {"input": 0.1, "output": 0.5, "cacheRead": 0.01,
                         "longContext": {"input": 0.5, "output": 2.5, "cacheRead": 0.05}}},
            {"model": "", "pricing": {"input": 1}},
        ]}
        out = price_feeds.parse_opendesign(doc)
        self.assertEqual(out["count"] if "count" in out else len(out["models"]), 2)
        self.assertTrue(out["models"]["claude-haiku-5.5"]["long_context"])
        self.assertEqual(out["long_context"]["claude-haiku-5.5"]["in"], 0.5)


class TestRowsOf(unittest.TestCase):
    def test_rows_shape_for_apply_prices(self):
        rows = price_feeds.rows_of({"m1": {"in": 1, "out": 2, "cache": 0.1,
                                          "off_in": 0.5, "off_out": 1}})
        self.assertEqual(rows[0]["model"], "m1")
        self.assertEqual(rows[0]["off_in"], 0.5)
        self.assertNotIn("off_cache", rows[0])


class TestFeedsFor(unittest.TestCase):
    def test_alias_resolution(self):
        self.assertEqual(price_feeds.feeds_for("open-design"), ["open-design"])
        self.assertEqual(price_feeds.feeds_for("opendesign"), ["open-design"])
        self.assertEqual(price_feeds.feeds_for("ollama-v2"), ["ollama"])
        self.assertEqual(price_feeds.feeds_for("tokenharbor"), ["tokenharbor"])
        self.assertEqual(price_feeds.feeds_for("未知渠道"), [])


if __name__ == "__main__":
    unittest.main()
