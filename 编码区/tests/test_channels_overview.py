#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_channels_overview.py —— 06 渠道管理单入口读模型（NEO-F4）。

覆盖：
  · 一屏聚合：渠道启停 + 路由/出站头 + 账号池 + 官方额度 + 本地闸门；
  · **双轨归属**：`official`（官方源）与 `gateway`（本机 8888 预算）各自独立，
    不合并、不互相顶替；官方无数据时 `source=none` 且百分比为 None；
  · 出站头只出掩码；账号按渠道归属统计；本地花费取真实账本。
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core import quota  # noqa: E402
from core.local_control import LocalControl  # noqa: E402
from core.local_ledger import LocalLedger  # noqa: E402
from core.local_readmodel import channels_overview  # noqa: E402


class _Router:
    def __init__(self, rows):
        self.rows = rows

    def summary(self):
        return {"default_channel": "opencode", "channels": self.rows}


class _Creds:
    def __init__(self, rows):
        self.rows = rows

    def channels(self):
        return self.rows


class _Accounts:
    """只实现读模型用到的 list_accounts，按渠道过滤。"""

    ROWS = {
        "opencode": [
            {"id": 1, "channel": "opencode", "name": "a", "status": "active",
             "last_checkin": 0, "credits": 500.0, "token_masked": "sk●●●23"},
            {"id": 2, "channel": "opencode", "name": "b", "status": "need_refresh",
             "last_checkin": 0, "credits": 750.0, "token_masked": "sk●●●24"},
        ],
    }

    def list_accounts(self, ledger, channel=""):
        return list(self.ROWS.get(channel, []))


class TestChannelsOverview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ov_"))
        pricing = self.tmp / "pricing.json"
        pricing.write_text(json.dumps({"models": {
            "glm-5.3": {"in": 0.7, "cache": 0.07, "out": 2.8}}}), encoding="utf-8")
        self.ledger = LocalLedger(self.tmp / "ledger.db", pricing)
        self.ledger.record(model="glm-5.3", channel="opencode", prompt=1000,
                           completion=200, status=200, ok=True)
        self.control = LocalControl(self.tmp / "control.json", default_channel="opencode")
        self.control.set_budget("global", 10)
        self.control.toggle_visibility("opencode", True, "检修")    # hidden=true → 渠道停用
        self.control.toggle_model("opencode", "glm-5.3", True)

    def _overview(self, quotas=None, now=None, router=None, creds=None):
        return channels_overview(
            ledger=self.ledger, control=self.control, router=router, accounts_mod=_Accounts(),
            creds=creds, quota_map=quotas or {},
            reconcile={"opencode": {"comparable": True, "delta_requests": 3,
                                    "verdict": "official_ahead"}},
            now=now or int(time.time()))

    def test_two_axes_never_merge(self):
        quotas = {"opencode": quota.normalize("opencode", source="live", used=4, limit=10,
                                              unit="requests")}
        doc = self._overview(quotas=quotas)
        row = doc["channels"][0]
        self.assertEqual(row["official"]["used_pct"], 40.0)
        self.assertEqual(row["gateway"]["cap_cny"], 10.0)
        self.assertIsNotNone(row["gateway"]["used_pct"])
        self.assertNotEqual(row["official"]["used_pct"], row["gateway"]["used_pct"])
        self.assertEqual(row["official_source"], "live")
        self.assertEqual(row["reconcile"]["verdict"], "official_ahead")

    def test_missing_official_data_is_none_not_zero(self):
        doc = self._overview(quotas={"opencode": quota.normalize("opencode", source="none")})
        row = doc["channels"][0]
        self.assertEqual(row["official_source"], "none")
        self.assertIsNone(row["official"]["used_pct"], "没数据必须 None，不能编 0")
        self.assertEqual(doc["summary"]["official_sources"], {"none": 1})

    def test_accounts_and_headers_aggregate(self):
        router = _Router([{
            "channel": "opencode", "base_url": "https://x/v1",
            "pricing_channel": "opencode", "endpoint": "/v1/chat/completions",
            "decision": False, "models": ["glm-5.3"], "token_present": True,
            "headers": {"x-opencode-token": "●●●●●●"},
            "header_names": ["x-opencode-token"],
        }])
        creds = _Creds([{"channel": "opencode", "scope": "default", "updated_at": 1,
                         "masked": {"username": "neo", "token": "sk●●●23"}}])
        doc = self._overview(router=router, creds=creds)
        row = doc["channels"][0]
        self.assertEqual(row["header_names"], ["x-opencode-token"])
        self.assertEqual(row["headers"]["x-opencode-token"], "●●●●●●")
        self.assertEqual(row["accounts"]["total"], 2)
        self.assertEqual(row["accounts"]["active"], 1)
        self.assertEqual(row["accounts"]["need_refresh"], 1)
        self.assertEqual(row["accounts"]["credits"], 1250.0)
        self.assertEqual(row["creds"]["masked"]["username"], "neo")
        self.assertNotIn("sk-live", json.dumps(row))
        self.assertEqual(doc["summary"]["headers"], 1)

    def test_channel_state_and_local_spend_are_real(self):
        router = _Router([{"channel": "opencode", "base_url": "", "pricing_channel": "opencode",
                           "endpoint": "", "decision": False, "models": [],
                           "token_present": False, "headers": {}, "header_names": []}])
        doc = self._overview(router=router)
        row = doc["channels"][0]
        self.assertFalse(row["enabled"])
        self.assertTrue(row["hidden"])
        self.assertEqual(doc["summary"]["enabled"], 0)
        self.assertGreater(doc["summary"]["local_spent_cny"], 0, "本地花费要取真账本")

    def test_today_window_only(self):
        now = int(time.time())
        router = _Router([{"channel": "opencode", "base_url": "", "pricing_channel": "opencode",
                           "endpoint": "", "decision": False, "models": [],
                           "token_present": False, "headers": {}, "header_names": []}])
        doc = self._overview(router=router, now=now + 86400 * 2)   # 两天后看，今日无支出
        self.assertEqual(doc["summary"]["local_spent_cny"], 0.0)

    def test_no_router_no_crash(self):
        doc = self._overview(router=None)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["channels"], [])
        self.assertEqual(doc["summary"]["channels"], 0)


if __name__ == "__main__":
    unittest.main()
