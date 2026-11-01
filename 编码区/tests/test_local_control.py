#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_control.py - 本地控制面（渠道启停 / 模型开关 / 预算上限）回归。

覆盖：默认值、原子落盘、幂等、批量、可见性快照还原、预算校验、
外部改文件后的重读、坏 JSON 容错，以及读模型叠加是不是真数。
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.local_readmodel import LocalReadModel  # noqa: E402


class TestLocalControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "control.json"
        self.ctl = LocalControl(self.path, default_channel="open-design")

    def _reload(self):
        return LocalControl(self.path, default_channel="open-design")

    def test_defaults_without_file(self):
        self.assertTrue(self.ctl.channel_enabled("open-design"))
        self.assertFalse(self.ctl.exists())
        self.assertIsNone(self.ctl.model_enabled("open-design", "any-model"))
        self.assertEqual(self.ctl.disabled_models("open-design"), set())
        # 文件不存在时读也不抛，返回默认预算
        self.assertIsNone(self.ctl.snapshot()["budgets"]["global"]["amount"])

    def test_toggle_model_persists_and_is_idempotent(self):
        first = self.ctl.toggle_model("open-design", "glm-5.3-flash", False)
        self.assertEqual(first["changed"], 1)
        self.assertEqual(first["saved"], 1)
        self.assertIs(self.ctl.model_enabled("open-design", "glm-5.3-flash"), False)
        self.assertEqual(self.ctl.disabled_models("open-design"), {"glm-5.3-flash"})
        second = self.ctl.toggle_model("open-design", "glm-5.3-flash", False)
        self.assertEqual(second["changed"], 0, "同值重复写不应算变更")
        # 新实例从磁盘读到同一结论（不是进程内内存）
        self.assertIs(self._reload().model_enabled("open-design", "glm-5.3-flash"), False)
        self.assertIsNotNone(self._reload().snapshot()["updated_at"])

    def test_toggle_bulk_counts_only_real_changes(self):
        self.ctl.toggle_models("open-design", ["a", "b"], True)
        res = self.ctl.toggle_models("open-design", ["a", "b", "c"], True)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["changed"], 1, "只有 c 是新变更")
        self.assertEqual(res["saved"], 1)

    def test_empty_args_rejected(self):
        with self.assertRaises(ValueError):
            self.ctl.toggle_model("", "m", True)
        with self.assertRaises(ValueError):
            self.ctl.toggle_models("open-design", [], True)
        with self.assertRaises(ValueError):
            self.ctl.toggle_visibility("", True)

    def test_visibility_snapshot_restore(self):
        # a 明确开、b 明确关、c 未表态
        self.ctl.toggle_model("open-design", "a", True)
        self.ctl.toggle_model("open-design", "b", False)
        res = self.ctl.toggle_visibility("open-design", True, "检修",
                                         known_models=["a", "b", "c", "d"])
        self.assertEqual(res["changed"], 1)
        self.assertEqual(res["models"], 4)
        self.assertFalse(self.ctl.channel_enabled("open-design"))
        self.assertTrue(self.ctl.channel_entry("open-design")["hidden"])
        # 隐藏后全部已知模型停用，含未表态的 c/d
        self.assertEqual(self.ctl.disabled_models("open-design"), {"a", "b", "c", "d"})
        # 还原：a 回到开、b 仍是关、c/d 回到未表态（不留假 OFF）
        self.ctl.toggle_visibility("open-design", False)
        self.assertTrue(self.ctl.channel_enabled("open-design"))
        self.assertEqual(self.ctl.disabled_models("open-design"), {"b"})
        self.assertIs(self.ctl.model_enabled("open-design", "a"), True)
        self.assertIsNone(self.ctl.model_enabled("open-design", "c"))

    def test_budget_set_and_clear(self):
        self.ctl.set_budget("global", "12.5")
        self.assertEqual(self.ctl.snapshot()["budgets"]["global"]["amount"], 12.5)
        self.ctl.set_budget("open-design", 8, label="OpenDesign")
        item = self.ctl.snapshot()["budgets"]["items"][0]
        self.assertEqual((item["key"], item["amount"], item["unit"]),
                         ("open-design", 8.0, "CNY"))
        # 留空 = 清掉硬顶，不是 0
        self.ctl.set_budget("global", None)
        self.assertIsNone(self.ctl.snapshot()["budgets"]["global"]["amount"])
        with self.assertRaises(ValueError):
            self.ctl.set_budget("global", -1)
        with self.assertRaises(ValueError):
            self.ctl.set_budget("global", "abc")

    def test_ack_errors_persist(self):
        res = self.ctl.ack_errors(["rid-1", "rid-2", "rid-1"])
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["done"], 2)
        self.assertEqual(self.ctl.acked_errors(), {"rid-1", "rid-2"})
        self.assertEqual(self.ctl.ack_errors(["rid-1"])["done"], 0,
                         "重复确认不重复计数")
        self.assertIn("rid-1", self._reload().acked_errors())
        self.assertEqual(self.ctl.ack_errors([])["total"], 0)

    def test_egress_set_clear_and_validate(self):
        self.assertEqual(self.ctl.egress_payload(),
                         {"proxy_url": "", "proxy_enabled": False})
        res = self.ctl.set_egress("http://127.0.0.1:7890")
        self.assertTrue(res["proxy_enabled"])
        self.assertEqual(res["changed"], 1)
        self.assertEqual(self._reload().egress_proxy(), "http://127.0.0.1:7890")
        self.ctl.set_egress("")
        self.assertFalse(self.ctl.egress_payload()["proxy_enabled"])
        with self.assertRaises(ValueError):
            self.ctl.set_egress("socks5://127.0.0.1:1080")
        with self.assertRaises(ValueError):
            self.ctl.set_egress("ftp://127.0.0.1:21")

    def test_atomic_write_leaves_no_tmp(self):
        self.ctl.toggle_model("open-design", "m", False)
        self.assertTrue(self.path.exists())
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        self.assertEqual(list(self.dir.glob("*.tmp*")), [])
        json.loads(self.path.read_text(encoding="utf-8"))  # 必须是完整 JSON

    def test_external_edit_is_picked_up(self):
        self.ctl.toggle_model("open-design", "m", False)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["models"]["open-design"]["m"] = True
        doc["models"]["open-design"]["n"] = False
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIs(self.ctl.model_enabled("open-design", "m"), True,
                      "手改配置应被 mtime 重读")
        self.assertEqual(self.ctl.disabled_models("open-design"), {"n"})

    def test_broken_json_falls_back_to_defaults(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertTrue(self.ctl.channel_enabled("open-design"))
        self.assertIsNone(self.ctl.model_enabled("open-design", "m"))


class TestBudgetGate(unittest.TestCase):
    """预算硬闸门：只看已设上限的档，到上限即拒；没上限一定放行。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctl = LocalControl(Path(self.tmp.name) / "control.json",
                                default_channel="open-design")

    def test_no_cap_never_blocks(self):
        gate = self.ctl.budget_gate("open-design", 999.0, 999.0)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["cap_count"], 0)

    def test_global_cap_blocks_at_and_over(self):
        self.ctl.set_budget("global", 5)
        self.assertFalse(self.ctl.budget_gate("open-design", 4.99, 0)["blocked"])
        gate = self.ctl.budget_gate("open-design", 5, 0)
        self.assertTrue(gate["blocked"], "恰好到上限就要拦，不等到超额")
        self.assertEqual((gate["key"], gate["cap_cny"], gate["spent_cny"]),
                         ("global", 5.0, 5.0))
        self.assertTrue(self.ctl.budget_gate("open-design", 9.5, 0)["blocked"])

    def test_channel_cap_uses_channel_spend(self):
        self.ctl.set_budget("open-design", 2, label="OpenDesign")
        # 全局支出很大但渠道只花 1.9：不该因全局数误拦
        self.assertFalse(self.ctl.budget_gate("open-design", 500.0, 1.9)["blocked"])
        gate = self.ctl.budget_gate("open-design", 500.0, 2.0)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["key"], "open-design")
        self.assertEqual(gate["cap_cny"], 2.0)

    def test_global_cap_wins_over_channel(self):
        self.ctl.set_budget("global", 1)
        self.ctl.set_budget("open-design", 100)
        self.assertEqual(self.ctl.budget_gate("open-design", 1.5, 0.5)["key"], "global")

    def test_zero_cap_is_hard_stop(self):
        self.ctl.set_budget("global", 0)
        self.assertTrue(self.ctl.budget_gate("open-design", 0.0, 0.0)["blocked"],
                        "0 是硬停，不等于未设上限")

    def test_clearing_cap_unblocks(self):
        self.ctl.set_budget("global", 1)
        self.assertTrue(self.ctl.budget_gate("open-design", 3, 3)["blocked"])
        self.ctl.set_budget("global", None)
        self.assertFalse(self.ctl.budget_gate("open-design", 3, 3)["blocked"])

    def test_warn_line_at_80_percent(self):
        self.ctl.set_budget("global", 10)
        self.assertFalse(self.ctl.budget_gate("open-design", 7.9, 0)["warn"],
                         "79% 不报预警")
        gate = self.ctl.budget_gate("open-design", 8.0, 0)
        self.assertTrue(gate["warn"])
        self.assertFalse(gate["blocked"])
        self.assertEqual((gate["used_pct"], gate["warn_pct"]), (80.0, 80))
        # 到顶就是拦截，不再只是预警
        full = self.ctl.budget_gate("open-design", 10.0, 0)
        self.assertTrue(full["blocked"] and full["warn"])
        self.assertEqual(full["used_pct"], 100.0)

    def test_warn_picks_the_hottest_cap(self):
        self.ctl.set_budget("global", 1000)
        self.ctl.set_budget("open-design", 10)
        gate = self.ctl.budget_gate("open-design", 90, 8.5)
        self.assertFalse(gate["blocked"])
        self.assertEqual((gate["key"], gate["used_pct"], gate["warn"]),
                         ("open-design", 85.0, True),
                         "报的是占用率最高的那一档（全 9% / 渠道 85%）")

    def test_warn_is_none_without_cap(self):
        gate = self.ctl.budget_gate("open-design", 50, 50)
        self.assertFalse(gate["warn"])
        self.assertIsNone(gate["used_pct"])

    def test_other_channel_cap_does_not_leak(self):
        self.ctl.set_budget("tokenharbor", 1)
        self.assertFalse(self.ctl.budget_gate("open-design", 99, 99)["blocked"],
                         "别的渠道的上限不能拦本渠道")


class TestControlOverlay(unittest.TestCase):
    """读模型与目录必须叠加控制面：面板 OFF 就要在读数里体现出来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        pricing = tmp / "pricing.json"
        pricing.write_text(json.dumps({"models": {
            "glm-5.3-flash": {"in": 0.7, "cache": 0.07, "out": 2.8},
        }}), encoding="utf-8")
        self.ledger = LocalLedger(tmp / "ledger.db", pricing)
        self.ctl = LocalControl(tmp / "control.json", default_channel="open-design")
        self.models = [{"id": "glm-5.3-flash"}, {"id": "mystery-model"}]
        self.now = int(time.time())
        self.ledger.record(model="glm-5.3-flash", prompt=1000, cached=0, completion=500,
                           at=self.now - 60, ok=True, status=200,
                           channel="open-design", rid="r1")

    def rm(self):
        return LocalReadModel(self.ledger, channel="open-design",
                              upstream="https://amr-link.example/v1",
                              models=self.models, token="sk-amr-test1234",
                              control=self.ctl)

    def test_visibility_reflects_control(self):
        vis = self.rm().visibility()
        self.assertEqual(vis["source"], "local-control")
        self.assertFalse(vis["channels"][0]["hidden"])
        self.ctl.toggle_visibility("open-design", True, "检修",
                                   known_models=["glm-5.3-flash"])
        vis = self.rm().visibility()
        self.assertTrue(vis["channels"][0]["hidden"])
        self.assertEqual(vis["hidden_channels"], ["open-design"])
        self.assertFalse(vis["channels"][0]["enabled"])

    def test_channels_and_custom_carry_enabled(self):
        self.ctl.toggle_visibility("open-design", True, "检修", known_models=[])
        self.assertFalse(self.rm().channels(self.now)["channels"][0]["enabled"])
        self.assertFalse(self.rm().custom()["data"][0]["enabled"])

    def test_catalog_enabled_overlay(self):
        self.ctl.toggle_model("open-design", "glm-5.3-flash", False)
        cat = self.ledger.catalog_payload(
            self.models, "open-design",
            disabled=self.ctl.disabled_models("open-design"),
            channel_enabled=self.ctl.channel_enabled("open-design"))
        status = {m["id"]: m["enabled"] for m in cat["models"]}
        self.assertFalse(status["glm-5.3-flash"], "本地面板停用的模型必须显示 OFF")
        self.assertTrue(status["mystery-model"], "未表态的模型跟随上游，不被连带停用")
        # 渠道停用：整个渠道的模型都算停用
        cat = self.ledger.catalog_payload(self.models, "open-design", disabled=set(),
                                          channel_enabled=False)
        self.assertFalse(any(m["enabled"] for m in cat["models"]))

    def test_errors_ack_overlay(self):
        self.ledger.record(model="glm-5.3-flash", prompt=10, completion=0,
                           at=self.now - 30, ok=False, status=429,
                           error="rate limited", channel="open-design", rid="bad-rid")
        row = [r for r in self.rm().errors("limit=5")["rows"]
               if r["req_id"] == "bad-rid"][0]
        self.assertFalse(row["acked"])
        self.ctl.ack_errors(["bad-rid"])
        doc = self.rm().errors("limit=5")
        row = [r for r in doc["rows"] if r["req_id"] == "bad-rid"][0]
        self.assertTrue(row["acked"])
        self.assertEqual(doc["acked_total"], 1)

    def test_pool_egress_reflects_control(self):
        self.assertEqual(self.rm().pool()["egress"],
                         {"proxy_url": "", "proxy_enabled": False})
        self.ctl.set_egress("http://127.0.0.1:7890")
        self.assertTrue(self.rm().pool()["egress"]["proxy_enabled"])

    def test_budgets_merge_cap_and_spend(self):
        self.ctl.set_budget("open-design", 1.0, label="OpenDesign")
        b = self.rm().budgets(self.now)
        self.assertEqual(b["source"], "local-control")
        self.assertEqual(b["global"]["spent_cny"], b["items"][0]["spent_cny"])
        self.assertTrue(b["items"][0]["spent_cny"] > 0, "今日账本支出要进预算行")
        self.assertEqual(b["items"][0]["amount"], 1.0)
        self.assertIsNotNone(b["items"][0]["used_pct"])
        # 无硬顶时不给 0% 的假占用率
        self.ctl.set_budget("open-design", None)
        row = self.rm().budgets(self.now)["items"][0]
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["used_pct"])

    def test_budgets_flags_over_and_reports_enforcement(self):
        total, by_channel = self.ledger.spend_since(0)
        self.assertTrue(total > 0, "账本必须有真实支出可用")
        self.assertEqual(by_channel.get("open-design"), total)
        self.ctl.set_budget("open-design", 0.000001)
        doc = self.rm().budgets(self.now)
        self.assertTrue(doc["enforced"])
        self.assertTrue(doc["items"][0]["over"], "已超上限必须标 over，供前端出“已满”")
        self.ctl.set_budget("open-design", None)
        self.assertFalse(self.rm().budgets(self.now)["items"][0]["over"])

    def test_budgets_flags_80pct_warn(self):
        # 今日支出已知（>0）：把上限设成它的 1.1 倍 → 约 90%，应报预警而非拦截
        total, _ = self.ledger.spend_since(0)
        self.assertTrue(total > 0)
        # 上限设为支出的 90% 倒数 → 占用率约 90%，应报预警而非拦截
        self.ctl.set_budget("open-design", round(total / 0.9, 4))
        row = self.rm().budgets(self.now)["items"][0]
        self.assertTrue(row["warn"], "到 80% 要提前报出来")
        self.assertFalse(row["over"])
        self.assertGreaterEqual(row["used_pct"], 80)
        self.assertEqual(self.rm().budgets(self.now)["warn_pct"], 80)
        # 上限放宽到 5 倍（占用 20%）→ 不再预警
        self.ctl.set_budget("open-design", round(total / 0.2, 4))
        self.assertFalse(self.rm().budgets(self.now)["items"][0]["warn"])


if __name__ == "__main__":
    unittest.main()
