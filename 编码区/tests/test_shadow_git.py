# -*- coding: utf-8 -*-
"""编码区/tests/test_shadow_git.py —— CR-1 单测(移植自 client-runtime,unittest 版)。

覆盖:
  · 快照/回滚无损(内容含二进制、原本不存在的文件)
  · **原子性**:多文件补丁中某文件打不上 → 工作区零改动
  · apply 成功后 commit_transaction 固化、不产生 git commit
  · 性能:快照与回滚预算内（预热 + 5 轮最优墙钟，见 `_timing.py`）
  · 非 git 目录 → ShadowGitError
  · 上下文管理器异常自动回滚
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from _timing import (  # noqa: E402
    REFERENCE_LIMIT_MS, best_ms, best_wall_ms, reference_wall_ms)
from client_runtime.shadow_git import ShadowGitError, ShadowGitManager  # noqa: E402


def _run(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


class ShadowGitTestBase(unittest.TestCase):
    """每个用例一个真实的临时 git 仓库(单测不碰任何现有工程)。"""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="sgt_"))
        _run(str(self.repo), "init", "-q")
        _run(str(self.repo), "config", "user.email", "t@t.local")
        _run(str(self.repo), "config", "user.name", "t")
        (self.repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (self.repo / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        _run(str(self.repo), "add", "-A")
        _run(str(self.repo), "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)


class TestSnapshotRollback(ShadowGitTestBase):
    def test_snapshot_and_rollback_restores_content(self):
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["a.py"])
        (self.repo / "a.py").write_text("def a():\n    return 999  # 手改\n", encoding="utf-8")
        mgr.rollback(sid)
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"),
                         "def a():\n    return 1\n")

    def test_rollback_deletes_file_that_did_not_exist(self):
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["new_file.py"])            # 快照时不存在
        (self.repo / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        mgr.rollback(sid)
        self.assertFalse((self.repo / "new_file.py").exists(), "原本不存在的文件回滚后应被删除")

    def test_rollback_is_binary_safe_and_utf8(self):
        raw = bytes(range(256))
        (self.repo / "bin.dat").write_bytes(raw)
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["bin.dat"])
        (self.repo / "bin.dat").write_bytes(b"garbage")
        mgr.rollback(sid)
        self.assertEqual((self.repo / "bin.dat").read_bytes(), raw, "二进制内容必须逐字节无损")


class TestPatchTransaction(ShadowGitTestBase):
    def test_apply_patch_success(self):
        mgr = ShadowGitManager(str(self.repo))
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def a():\n"
            "-    return 1\n"
            "+    return 42\n"
        )
        sid = mgr.snapshot(["a.py", "b.py"])
        self.assertIs(mgr.apply_patch(patch, snapshot_id=sid), True)
        self.assertIn("return 42", (self.repo / "a.py").read_text(encoding="utf-8"))
        mgr.commit_transaction(sid)

    def test_atomicity_bad_patch_leaves_workspace_untouched(self):
        """核心断言:多文件补丁里有一处打不上 → 一个字节都不改。"""
        before_a = (self.repo / "a.py").read_text(encoding="utf-8")
        before_b = (self.repo / "b.py").read_text(encoding="utf-8")
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["a.py", "b.py"])
        # 第二个文件的上下文对不上(原文是 return 2,这里假装 return 999)
        patch = (
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 7\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n def b():\n-    return 999\n+    return 8\n"
        )
        ok = mgr.apply_patch(patch, snapshot_id=sid)
        self.assertFalse(ok, "打不上的补丁必须返回 False")
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"), before_a,
                         "第一个文件不能被改")
        self.assertEqual((self.repo / "b.py").read_text(encoding="utf-8"), before_b)
        self.assertEqual(mgr.dirty(["a.py", "b.py"]), [], "工作区必须零残留")

    def test_can_apply_is_dry_run(self):
        mgr = ShadowGitManager(str(self.repo))
        before = (self.repo / "a.py").read_text(encoding="utf-8")
        bad = ("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def a():\n"
               "-    no_such_line\n+    return 5\n")
        ok, why = mgr.can_apply(bad)
        self.assertFalse(ok, "坏补丁预检必须返回 False")
        self.assertTrue(why, "预检应失败并给出原因")
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"), before, "预检不得落盘")

    def test_commit_transaction_discards_snapshot_without_git_commit(self):
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["a.py"])
        n_before = _run(str(self.repo), "rev-list", "--count", "HEAD").stdout.strip()
        (self.repo / "a.py").write_text("def a():\n    return 5\n", encoding="utf-8")
        mgr.commit_transaction(sid)
        n_after = _run(str(self.repo), "rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(n_before, n_after, "commit_transaction 不应产生 git commit")
        self.assertEqual(mgr.open_snapshots(), [], "快照应被销毁")
        self.assertIn("return 5", (self.repo / "a.py").read_text(encoding="utf-8"), "修改应固化")

    def test_dirty_lists_changed_files(self):
        mgr = ShadowGitManager(str(self.repo))
        (self.repo / "b.py").write_text("def b():\n    return 3\n", encoding="utf-8")
        files = mgr.dirty(["a.py", "b.py"])
        self.assertTrue(any(f.endswith("b.py") for f in files),
                        "dirty 应报出被改的 b.py: %s" % files)
        self.assertFalse(any(f.endswith("a.py") for f in files), "未改的 a.py 不应出现")


class TestLatency(ShadowGitTestBase):
    # 单文件快照/回滚是 0.1ms 量级，低于 Windows 15.6ms 的 CPU 计时粒度：
    # CPU 口径在这里只会读出 0（等于恒绿），所以这两条用 `best_wall_ms`（预热 + 5 轮最优
    # 墙钟），预算仍是票面 15ms / 6ms。另：批量滚回同一个文件会制造真实工作流里不存在的
    # rename 竞争（WinError 5），也有把产品重试预算吃穿的风险，故不批量。
    ROUNDS = 5

    def _require_measurable_host(self):
        """宿主被压满时 rename 本身就从 0.1ms 涨到 10ms，亚毫秒动作的绝对预算不可测：
        如实 skip（并写明基准值），而不是把环境噪声报成代码回归。"""
        ref = reference_wall_ms()
        if ref > REFERENCE_LIMIT_MS:
            self.skipTest("宿主过载：基准 rename 最优 %.2fms > %.1fms，亚毫秒级绝对时延不可测"
                          % (ref, REFERENCE_LIMIT_MS))

    def test_snapshot_and_rollback_latency_under_budget(self):
        """票面要求:典型事务规模(1~3 文件)快照与回滚在预算内（各 15ms）。"""
        self._require_measurable_host()
        mgr = ShadowGitManager(str(self.repo))
        snap_wall = best_wall_ms(lambda: mgr.snapshot(["a.py", "b.py"]), rounds=self.ROUNDS)
        holder = {}

        def prepare():
            holder["sid"] = mgr.snapshot(["a.py", "b.py"])
            (self.repo / "a.py").write_text("x\n", encoding="utf-8")
            (self.repo / "b.py").write_text("y\n", encoding="utf-8")

        rb_wall = best_wall_ms(lambda: mgr.rollback(holder["sid"]), prepare=prepare,
                               rounds=self.ROUNDS)
        self.assertLess(snap_wall, 15.0, "快照最优 wall=%.2fms 应 < 15ms" % snap_wall)
        self.assertLess(rb_wall, 15.0, "回滚最优 wall=%.2fms 应 < 15ms(2 文件)" % rb_wall)

    def test_rollback_single_file_well_under_budget(self):
        """单文件回滚必须远低于预算(最常见场景:agent 改一个文件)。"""
        self._require_measurable_host()
        mgr = ShadowGitManager(str(self.repo))
        holder = {}

        def prepare():
            holder["sid"] = mgr.snapshot(["a.py"])
            (self.repo / "a.py").write_text("x\n", encoding="utf-8")

        wall = best_wall_ms(lambda: mgr.rollback(holder["sid"]), prepare=prepare,
                            rounds=self.ROUNDS)
        self.assertLess(wall, 6.0, "单文件回滚最优 wall=%.2fms 应 < 6ms(NTFS 容差)" % wall)

    def test_snapshot_scales_cheaply(self):
        """快照成本与文件数弱相关(只读+哈希,不落盘),20 文件也应在预算内。"""
        files = []
        for i in range(20):
            fn = "many_%02d.py" % i
            (self.repo / fn).write_text("def f%d():\n    return %d\n" % (i, i), encoding="utf-8")
            files.append(fn)
        mgr = ShadowGitManager(str(self.repo))

        def batch():                            # 单次 6ms 量级低于 CPU 计时粒度，批量 10 次上量摊薄
            for _ in range(10):
                mgr.snapshot(files)

        # 预算最紧的一条（10ms），故按 CPU 判定：宿主被抢占时墙钟会成倍放大，
        # CPU 消耗才是代码成本；预热 + 3 轮最优 ×10 次摊薄。
        cpu, wall = best_ms(batch)
        per_snap = cpu / 10
        self.assertLess(per_snap, 10.0, "20 文件快照 cpu=%.2fms/次 / wall=%.2fms/次 应 < 10ms"
                        % (per_snap, wall / 10))


class TestErrorPaths(ShadowGitTestBase):
    def test_replace_retry_survives_transient_lock(self):
        """WinError 5 竞争（杀软/索引器瞬时持句柄）必须重试，不能把一次碰撞当回滚失败。"""
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["a.py"])
        (self.repo / "a.py").write_text("改过的内容\n", encoding="utf-8")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:                      # 前两次假装被占用
                raise PermissionError(13, "被占用", str(dst))
            return real_replace(src, dst)

        os.replace = flaky_replace
        try:
            mgr.rollback(sid)
        finally:
            os.replace = real_replace
        self.assertEqual(calls["n"], 3, "应重试到成功为止")
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"),
                         "def a():\n    return 1\n", "重试后必须真的回滚成功")
        self.assertEqual(list(self.repo.glob("*.shadowgit-*")), [],
                         "失败重试不得残留临时文件")

    def test_replace_retry_gives_up_loudly(self):
        """重试用尽仍被占用 → 必须报错，绝不假回滚。"""
        mgr = ShadowGitManager(str(self.repo))
        sid = mgr.snapshot(["a.py"])
        (self.repo / "a.py").write_text("改过的内容\n", encoding="utf-8")

        real_replace = os.replace

        def always_locked(src, dst):
            raise PermissionError(13, "被占用", str(dst))

        os.replace = always_locked
        try:
            with self.assertRaises(PermissionError):
                mgr.rollback(sid)
        finally:
            os.replace = real_replace

    def test_non_git_dir_raises(self):
        bare = tempfile.mkdtemp(prefix="sgt_bare_")
        try:
            with self.assertRaises(ShadowGitError):
                ShadowGitManager(bare)
        finally:
            shutil.rmtree(bare, ignore_errors=True)

    def test_unknown_snapshot_raises(self):
        mgr = ShadowGitManager(str(self.repo))
        with self.assertRaises(ShadowGitError):
            mgr.rollback("nope-not-exist")

    def test_context_manager_rolls_back_on_exception(self):
        before = (self.repo / "a.py").read_text(encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with ShadowGitManager(str(self.repo)) as mgr:
                sid = mgr.snapshot(["a.py"])
                (self.repo / "a.py").write_text("broken\n", encoding="utf-8")
                raise RuntimeError("linter 失败")
        self.assertEqual((self.repo / "a.py").read_text(encoding="utf-8"), before,
                         "异常出口应自动回滚")


if __name__ == "__main__":
    unittest.main()
