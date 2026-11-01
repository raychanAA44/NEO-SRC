#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_official_windows.py —— 官方分窗用量（今日 / 近 5 小时 / 本周 / 本月）。

锁三件事：
  · open-design 登录态：`coding-plan-usage.windows[]` 的 5h/7d 拆成 h5/week，
    `billing/summary.todayConsumedUsd` 当今日，月度发放额官方不给 ⇒ missing 要写明；
  · ollama：`balance` 的 allowance/balance 出月上限，`/api/usage` 的时间桶按 CST
    窗口聚合，legacy plan 的 `session/weekly.remaining_percent` 出 5h/周百分比；
  · 没凭据时如实给 missing，不抛异常、不补 0。
"""

import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core import clock  # noqa: E402
from core import official_windows as ow  # noqa: E402


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class _FakeOD:
    """假 open-design 官方：workspaces/current + coding-plan-usage + billing/summary。"""

    def __init__(self, windows=None, summary=None, status=200):
        self.windows = windows if windows is not None else [
            {"durationSeconds": 18000, "usedCredits": 12.5, "limitCredits": 50,
             "resetsAt": "2026-10-26T18:00:00Z"},
            {"durationSeconds": 604800, "usedCredits": 210, "limitCredits": 400,
             "resetsAt": "2026-11-02T00:00:00Z"},
        ]
        self.summary = summary if summary is not None else {
            "todayConsumedUsd": 3.25, "totalAvailableCreditsUsd": 88.5,
            "subscriptionCreditsUsd": 60, "rechargeCreditsUsd": 28.5,
            "subscriptionCurrentPeriodEnd": "2026-11-01T00:00:00Z"}
        self.status = status
        self.seen = []

    def __call__(self, url, headers, timeout):
        self.seen.append((url, dict(headers)))
        if "workspaces/current" in url:
            return 200, {"workspaceId": "ws-1"}, ""
        if "coding-plan-usage" in url:
            assert headers.get("x-vela-workspace-id") == "ws-1"
            return self.status, {"windows": self.windows, "tier": "pro"}, ""
        if "billing/summary" in url:
            return self.status, self.summary, ""
        return 404, None, "HTTP 404"


class _FakeOllama:
    """假 ollama：balance + three usage ranges；桶按小时/按天给。"""

    def __init__(self, balance=None, hourly=None, daily=None):
        now = time.time()
        self.balance = balance if balance is not None else {
            "included": {"balance_usd": 42.5, "allowance_usd": 60,
                         "period": {"from": _iso(now - 10 * 86400),
                                    "until": _iso(now + 20 * 86400)}},
            "purchased": {"balance_usd": 5}}
        # 桶要同时落在「今日」与「近 5 小时」的交集里：优先取 now-2min/now-1min，
        # 但不得跨过 CST 零点——按零点固定放，下午跑时 h5 窗会整窗为空（那是真实行为，
        # 但断言也就跟着随运行时刻飘了；2026-10-27 下午实测 h5 KeyError 即此因）。
        day0 = clock.day_start(now)
        first = max(now - 120, day0 + 60)
        second = max(now - 60, day0 + 120)
        self.hourly = hourly if hourly is not None else [
            {"from": _iso(first), "request_count": 4, "usage_usd": 0.5,
             "input_tokens": 1000, "output_tokens": 200, "partial": True},
            {"from": _iso(second), "request_count": 3, "usage_usd": 0.25,
             "input_tokens": 800, "output_tokens": 100, "partial": False},
        ]
        self.daily = daily if daily is not None else [
            {"from": _iso(clock.day_start(now)), "request_count": 7, "usage_usd": 0.75},
            {"from": _iso(clock.day_start(now) - 86400), "request_count": 9, "usage_usd": 1.5},
        ]
        self.seen = []

    def __call__(self, url, headers, timeout):
        self.seen.append(url)
        if url.endswith("/api/balance"):
            return 200, self.balance, ""
        if "range=24h" in url:
            return 200, {"granularity": "hour", "buckets": self.hourly}, ""
        if "range=7d" in url:
            return 200, {"granularity": "day", "buckets": self.daily}, ""
        if "range=30d" in url:
            return 200, {"granularity": "day", "buckets": self.daily + [
                {"from": _iso(clock.month_start(time.time()) - 86400), "usage_usd": 9.0}]}, ""
        return 404, None, "HTTP 404"


class _FreshCache(unittest.TestCase):
    def setUp(self):
        ow.invalidate()


class TestOpenDesignWindows(_FreshCache):
    def test_windows_from_coding_plan_and_summary(self):
        fake = _FakeOD()
        got = ow.collect("open-design", blocking=True, creds={"session": "s-1"}, fetch=fake)
        self.assertTrue(got["ok"], got.get("missing"))
        wins = got["windows"]
        self.assertEqual(wins["h5"]["used"], 12.5)
        self.assertEqual(wins["h5"]["limit"], 50.0)
        self.assertEqual(wins["h5"]["used_pct"], 25.0)
        self.assertEqual(wins["week"]["used_pct"], 52.5)
        self.assertEqual(wins["today"]["used"], 3.25)
        self.assertIsNone(wins["today"]["limit"], "官方不给日上限就不要造分母")
        self.assertNotIn("month", wins)
        missing = {m["window"]: m["why"] for m in got["missing"]}
        self.assertIn("月度发放额", missing["month"])
        self.assertEqual(got["extra"]["balance_usd"], 88.5)

    def test_auth_failure_is_explained_not_hidden(self):
        fake = _FakeOD(status=401)
        got = ow.collect("open-design", blocking=True, creds={"session": "bad"}, fetch=fake)
        self.assertFalse(got["ok"])
        joined = " ".join(m["why"] for m in got["missing"])
        self.assertIn("401", joined)

    def test_no_session_says_what_is_missing(self):
        got = ow.collect("open-design", blocking=True, creds={}, fetch=_FakeOD())
        self.assertFalse(got["ok"])
        self.assertTrue(got["missing"])
        self.assertIn("会话", got["missing"][0]["why"])


class TestOllamaWindows(_FreshCache):
    def test_month_limit_from_allowance_and_buckets_fill_the_rest(self):
        fake = _FakeOllama()
        got = ow.collect("ollama", blocking=True, creds={"api_key": "k"}, fetch=fake)
        self.assertTrue(got["ok"], got.get("missing"))
        wins = got["windows"]
        self.assertEqual(wins["month"]["limit"], 60.0)
        self.assertEqual(wins["month"]["used"], 17.5)              # 60 - 42.5
        self.assertEqual(wins["month"]["used_pct"], 29.2)
        self.assertEqual(wins["today"]["used"], 0.75)              # 今日桶
        self.assertEqual(wins["h5"]["used"], 0.75)                 # 近 5 小时两桶相加
        self.assertEqual(wins["h5"]["requests"], 7)
        # 本周含“今天”与“昨天”（周一起算；凌晨跑时昨天已在本周内）
        now = time.time()
        expect_week = 0.75 + (1.5 if clock.day_start(now) - 86400 >= clock.week_start(now) else 0)
        self.assertEqual(wins["week"]["used"], expect_week)
        self.assertTrue(any("range=24h" in u for u in fake.seen))

    def test_legacy_plan_percentages(self):
        now = time.time()
        fake = _FakeOllama(balance={"included": {
            "session": {"remaining_percent": 62.0, "resets_at": _iso(now + 3600)},
            "weekly": {"remaining_percent": 15.5, "resets_at": _iso(now + 3 * 86400)}}})
        got = ow.collect("ollama", blocking=True, creds={"api_key": "k"}, fetch=fake)
        wins = got["windows"]
        self.assertEqual(wins["h5"]["used_pct"], 38.0)
        self.assertEqual(wins["week"]["used_pct"], 84.5)
        self.assertIsNone(wins["month"]["limit"], "legacy plan 没有 allowance_usd 就不要造月上限")

    def test_missing_key_is_stated(self):
        got = ow.collect("ollama", blocking=True, creds={}, fetch=_FakeOllama())
        self.assertFalse(got["ok"])
        self.assertIn("key", got["missing"][0]["why"].lower())


class TestCacheAndScope(_FreshCache):
    def test_unsupported_channel_is_explicit(self):
        got = ow.collect("tokenharbor", blocking=True)
        self.assertFalse(got["ok"])
        self.assertIn("未接入", got["missing"][0]["why"])

    def test_cache_serves_second_read_without_refetch(self):
        ow.invalidate()
        fake = _FakeOD()
        ow.collect("open-design", blocking=True, creds={"session": "s"}, fetch=fake)
        calls = len(fake.seen)
        ow.collect("open-design")
        self.assertEqual(len(fake.seen), calls, "60s 缓存内不再打官方端点")


if __name__ == "__main__":
    unittest.main()
