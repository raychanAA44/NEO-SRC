# -*- coding: utf-8 -*-
"""编码区/tests/test_task_lock_dispatcher.py —— NEO-GW-02 单测(纯标准库 unittest)。

覆盖:
  · TaskLockStore:bind/get 往返、未知查询、TTL 惰性过期、release、clear、重复 bind 刷新
  · Dispatcher:直通零篡改(红线7)、分组未定义 KeyError、无活跃成员 RuntimeError、
    优先级选模(priority 最小)、粘性锁命中(locked=True)、task_lock 关闭不绑锁、
    task_id 为空不绑锁、非活跃成员被排除、catalog 返回 None 亦归一为 KeyError
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core.task_lock import TaskLockStore                      # noqa: E402
from core.dispatcher import (                                 # noqa: E402
    ResolvedTarget, RequestDispatcher, is_group_id)


class _FakeClock:
    """可拨动时钟:让 TTL 过期用例确定性可测。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, sec):
        self.now += sec


class _Catalog:
    """最小鸭子类型 catalog 桩:get_group 未命中抛 KeyError,选模走 get_active_members。"""

    def __init__(self, groups):
        self.groups = groups

    def get_group(self, gid):
        if gid not in self.groups:
            raise KeyError(gid)
        return self.groups[gid]

    def get_active_members(self, gid):
        group = self.groups.get(gid) or {}
        members = [m for m in (group.get("members") or [])
                   if m.get("enabled", True) is not False]
        return sorted(members, key=lambda m: int(m.get("priority", 0)))


class _NoneCatalog:
    """get_group 未命中返回 None 的容错口径(亦须归一为 KeyError)。"""

    def get_group(self, gid):
        return None


def _group(members, task_lock=True):
    return {
        "policy": {"task_lock": task_lock},
        "members": members,
    }


class TestTaskLockStore(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.store = TaskLockStore(clock=self.clock)

    def test_bind_get_roundtrip(self):
        self.store.bind("t1", "auto/code-heavy", "trae/claude-3-7-sonnet")
        self.assertEqual(self.store.get("t1", "auto/code-heavy"),
                         "trae/claude-3-7-sonnet")

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.store.get("nope", "auto/x"))

    def test_ttl_lazy_expiry(self):
        self.store.bind("t1", "auto/g", "m1", ttl=100)
        self.clock.advance(99)
        self.assertEqual(self.store.get("t1", "auto/g"), "m1", "TTL 内有效")
        self.clock.advance(1)
        self.assertIsNone(self.store.get("t1", "auto/g"), "过期即删返回 None")
        self.assertEqual(self.store.size(), 0, "过期记录应被惰性清除")

    def test_release_removes_binding(self):
        self.store.bind("t1", "auto/g", "m1")
        self.assertTrue(self.store.release("t1", "auto/g"), "命中锁应返回 True")
        self.assertIsNone(self.store.get("t1", "auto/g"))
        self.assertFalse(self.store.release("t1", "auto/g"), "重复释放应返回 False")

    def test_clear_empties_all(self):
        self.store.bind("t1", "auto/g", "m1")
        self.store.bind("t2", "auto/g", "m2")
        self.store.clear()
        self.assertEqual(self.store.size(), 0)
        self.assertIsNone(self.store.get("t1", "auto/g"))

    def test_rebind_refreshes_and_overrides(self):
        self.store.bind("t1", "auto/g", "m1")
        self.store.bind("t1", "auto/g", "m2", ttl=100)
        self.assertEqual(self.store.get("t1", "auto/g"), "m2", "重复 bind 应覆盖")
        self.clock.advance(100)
        self.assertIsNone(self.store.get("t1", "auto/g"), "TTL 应按最后一次 bind 计")

    def test_snapshot_lists_active_locks_with_ttl(self):
        self.store.bind("t2", "auto/b", "trae/m2", ttl=200)
        self.store.bind("t1", "auto/a", "trae/m1", ttl=100)
        rows = self.store.snapshot()
        self.assertEqual([r["task_id"] for r in rows], ["t1", "t2"], "按到期时间升序")
        self.assertEqual(rows[0]["model"], "trae/m1")
        self.assertEqual(rows[0]["group_id"], "auto/a")
        self.assertEqual(rows[0]["ttl_remaining"], 100)
        self.assertEqual(rows[1]["ttl_remaining"], 200)

    def test_snapshot_drops_expired_without_leaking(self):
        self.store.bind("t1", "auto/g", "m1", ttl=100)
        self.clock.advance(101)
        self.assertEqual(self.store.snapshot(), [])
        self.assertEqual(self.store.size(), 0, "快照应惰性清除过期记录")

    def test_bind_requires_non_empty_args(self):
        for args in (("", "auto/g", "m"), ("t1", "", "m"), ("t1", "auto/g", "")):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.store.bind(*args)


class TestIsGroupId(unittest.TestCase):
    def test_prefix_decision(self):
        self.assertTrue(is_group_id("auto/code-heavy"))
        self.assertFalse(is_group_id("workbuddy-intl/glm-5.3"))
        self.assertFalse(is_group_id(""))
        self.assertFalse(is_group_id(None))
        self.assertFalse(is_group_id("autobots/x"), "前缀必须完整匹配 auto/")


class TestRequestDispatcher(unittest.TestCase):
    def setUp(self):
        self.catalog = _Catalog({
            "auto/code-heavy": _group([
                {"name": "trae/gpt-5-turbo", "priority": 2, "enabled": True, "tier": "weak"},
                {"name": "trae/claude-3-7-sonnet", "priority": 1, "enabled": True, "tier": "strong"},
                {"name": "trae/deepseek-v4", "priority": 3, "enabled": False},
            ]),
            "auto/no-lock": _group([
                {"name": "trae/gpt-5-turbo", "priority": 1, "enabled": True},
            ], task_lock=False),
            "auto/all-down": _group([
                {"name": "trae/gpt-5-turbo", "priority": 1, "enabled": False},
            ]),
            "auto/no-members": _group([]),
            "auto/bare-upstream": _group([
                {"name": "open-design/deepseek-v4-pro",
                 "upstream_name": "deepseek-v4-pro",
                 "priority": 1, "enabled": True, "tier": "strong"},
            ]),
        })
        self.lock = TaskLockStore()
        self.d = RequestDispatcher(task_lock=self.lock)

    def test_direct_passthrough_untouched(self):
        """红线7:显式单模型绝对不干预、原样直通。"""
        t = self.d.dispatch("workbuddy-intl/glm-5.3", {"messages": []}, "t1", self.catalog)
        self.assertEqual(t.model, "workbuddy-intl/glm-5.3")
        self.assertFalse(t.is_group)
        self.assertFalse(t.locked)
        self.assertEqual(t.group_id, "")

    def test_undefined_group_raises_keyerror(self):
        with self.assertRaises(KeyError) as ctx:
            self.d.dispatch("auto/nope", {}, "t1", self.catalog)
        self.assertIn("未定义的分组", str(ctx.exception))

    def test_none_returning_catalog_also_keyerror(self):
        with self.assertRaises(KeyError):
            self.d.dispatch("auto/anything", {}, "t1", _NoneCatalog())

    def test_no_active_members_raises_runtime_error(self):
        for gid in ("auto/all-down", "auto/no-members"):
            with self.subTest(gid=gid):
                with self.assertRaises(RuntimeError) as ctx:
                    self.d.dispatch(gid, {}, "t1", self.catalog)
                self.assertIn("无可用活跃成员", str(ctx.exception))

    def test_first_round_picks_highest_priority(self):
        t = self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        self.assertEqual(t.model, "trae/claude-3-7-sonnet", "priority=1 最小者胜出")
        self.assertTrue(t.is_group)
        self.assertFalse(t.locked, "首轮是无锁新选")
        self.assertEqual(t.group_id, "auto/code-heavy")
        self.assertEqual(t.tier, "strong", "档位来自真实组员属性")
        self.assertEqual(t.priority, 1)
        self.assertTrue(t.policy["task_lock"])

    def test_lock_binds_on_first_round(self):
        self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        self.assertEqual(self.lock.get("t1", "auto/code-heavy"),
                         "trae/claude-3-7-sonnet", "首轮应固化粘性锁")

    def test_second_round_reuses_lock(self):
        self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        t2 = self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        self.assertEqual(t2.model, "trae/claude-3-7-sonnet")
        self.assertTrue(t2.locked, "命中已有 Task Model Lock")
        self.assertTrue(t2.is_group)
        self.assertEqual(t2.group_id, "auto/code-heavy")
        self.assertEqual(t2.tier, "strong", "锁命中也能回读组员档位")
        self.assertEqual(t2.priority, 1)

    def test_lock_hit_matches_bare_upstream_name(self):
        """锁里存上游裸名；HIT 仍要回读到 strong/priority，不能丢档位。"""
        t1 = self.d.dispatch("auto/bare-upstream", {}, "t1", self.catalog)
        self.assertEqual(t1.model, "deepseek-v4-pro")
        t2 = self.d.dispatch("auto/bare-upstream", {}, "t1", self.catalog)
        self.assertTrue(t2.locked)
        self.assertEqual(t2.tier, "strong")
        self.assertEqual(t2.priority, 1)

    def test_locks_are_per_task_and_per_group(self):
        self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        t2 = self.d.dispatch("auto/code-heavy", {}, "t2", self.catalog)
        self.assertFalse(t2.locked, "不同 task_id 互不共用锁")

    def test_lock_disabled_reselects_each_round(self):
        t1 = self.d.dispatch("auto/no-lock", {}, "t1", self.catalog)
        t2 = self.d.dispatch("auto/no-lock", {}, "t1", self.catalog)
        self.assertEqual(t1.model, t2.model)
        self.assertFalse(t1.locked and t2.locked)
        self.assertEqual(self.lock.size(), 0, "policy.task_lock=False 不得绑锁")

    def test_empty_task_id_bypasses_lock(self):
        t = self.d.dispatch("auto/code-heavy", {}, "", self.catalog)
        self.assertFalse(t.locked)
        self.assertEqual(self.lock.size(), 0, "task_id 为空视为无主请求,不绑锁")

    def test_missing_tier_is_empty_not_guessed(self):
        t = self.d.dispatch("auto/no-lock", {}, "t1", self.catalog)
        self.assertEqual(t.tier, "", "组员未声明 tier 时留空")
        self.assertFalse(t.policy["task_lock"])

    def test_inactive_member_excluded_from_selection(self):
        t = self.d.dispatch("auto/code-heavy", {}, "t9", self.catalog)
        self.assertNotEqual(t.model, "trae/deepseek-v4", "非活跃成员不得入选")

    def test_resolved_target_contract_fields(self):
        t = self.d.dispatch("auto/code-heavy", {}, "t1", self.catalog)
        self.assertIsInstance(t, ResolvedTarget)
        # 工单四字段为基础契约;并行演进允许追加(tier/policy/priority),但四字段必须常在
        self.assertLessEqual({"model", "is_group", "locked", "group_id"},
                             set(t.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
