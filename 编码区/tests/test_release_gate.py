#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_release_gate.py —— 发版门禁脚本：步骤顺序、失败熔断、日志归档与报告。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "运维区" / "scripts"))

import release_gate


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hubneo-gate-"))
        self.runs = self.tmp / "test_runs"
        self.runs.mkdir()
        self.out = self.tmp / "gate-out"
        self.old = (release_gate.TEST_RUNS, release_gate.RELEASE_DIR, release_gate.build_steps)
        release_gate.TEST_RUNS = self.runs
        release_gate.RELEASE_DIR = self.tmp / "releases"
        self.calls = []

    def tearDown(self):
        release_gate.TEST_RUNS, release_gate.RELEASE_DIR, release_gate.build_steps = self.old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSteps(unittest.TestCase):
    def test_order_and_stress_rounds(self):
        steps = release_gate.build_steps("python", 5)
        self.assertEqual([s[0] for s in steps],
                         ["lite 构建与验收", "全量回归", "满载回归", "实链路验收",
                          "便携 exe 构建与冒烟", "便携 exe 功能验收"])
        # lite 切片必须排在最前：它自带设计闸门且几秒出结果，违规时不必再跑十几分钟回归
        self.assertTrue(steps[0][1][1].endswith("verify_lite_release.py"))
        self.assertIn("--build", steps[0][1])
        stress_cmd = steps[2][1]
        self.assertIn("--stress", stress_cmd)
        self.assertEqual(stress_cmd[stress_cmd.index("--stress") + 1], "5")
        self.assertTrue(steps[1][1][1].endswith("run_tests.py"))
        self.assertTrue(steps[3][1][1].endswith("verify_acceptance.py"))
        # 打包步骤必须真冒烟：不带 --smoke 就等于只验证「构建成功」，包起不来照样绿
        self.assertIn("--smoke", steps[4][1])
        self.assertTrue(steps[4][1][1].endswith("build_exe.py"))
        self.assertTrue(steps[5][1][1].endswith("verify_exe_functions.py"))

    def test_without_exe_keeps_backend_only_steps(self):
        steps = release_gate.build_steps("python", 3, with_exe=False)
        self.assertEqual([s[0] for s in steps],
                         ["lite 构建与验收", "全量回归", "满载回归", "实链路验收"])


    def test_echo_filter_skips_access_logs(self):
        """网关访问日志也是 `[HUB NEO ...`，不能回显；只放子脚本的进度标记。"""
        self.assertFalse("[HUB NEO 8888] \"GET / HTTP/1.1\" 200 -".startswith(release_gate.ECHO_PREFIXES))
        self.assertFalse("test_foo (test_bar.Test) ... ok".startswith(release_gate.ECHO_PREFIXES))
        self.assertTrue("[STRESS r1/3] OK  164.1s".startswith(release_gate.ECHO_PREFIXES))
        self.assertTrue("[PASS] 01. GET /health".startswith(release_gate.ECHO_PREFIXES))

    def test_bad_args(self):
        self.assertEqual(release_gate.main(["--rounds", "abc"]), 2)
        self.assertEqual(release_gate.main(["--out"]), 2)


class TestGate(_Base):
    def test_fail_fast_stops_following_steps(self):
        steps = [("步骤A", ["true"]), ("步骤B", ["true"]), ("步骤C", ["true"])]
        release_gate.build_steps = lambda *a, **k: steps

        def fake_run(name, cmd, log_path, echo=print):
            self.calls.append(name)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("x", encoding="utf-8")
            return (1 if name == "步骤B" else 0), 0.1

        release_gate._run_step = fake_run
        old = release_gate._run_step
        try:
            rc = release_gate.run_gate(3, self.out, echo=lambda *_: None)
        finally:
            release_gate._run_step = old

        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, ["步骤A", "步骤B"])
        report = (self.out / "GATE_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("FAILED", report)
        self.assertIn("步骤C", report)
        self.assertIn("未执行（已熔断）", report)

    def test_pass_archives_new_logs_and_writes_report(self):
        steps = [("步骤A", ["true"]), ("步骤B", ["true"])]
        release_gate.build_steps = lambda *a, **k: steps
        (self.runs / "run-old.log").write_text("old", encoding="utf-8")

        def fake_run(name, cmd, log_path, echo=print):
            self.calls.append(name)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("step %s" % name, encoding="utf-8")
            (self.runs / ("run-%s.log" % name)).write_text("detail", encoding="utf-8")
            return 0, 0.2

        release_gate._run_step = fake_run
        old = release_gate._run_step
        try:
            rc = release_gate.run_gate(3, self.out, echo=lambda *_: None)
        finally:
            release_gate._run_step = old

        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, ["步骤A", "步骤B"])
        report = (self.out / "GATE_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("PASSED", report)
        self.assertIn("3 轮", report)
        archived = sorted(p.name for p in (self.out / "test_runs").iterdir())
        self.assertEqual(archived, ["run-步骤A.log", "run-步骤B.log"])
        # 归档是副本，不搬走原件
        self.assertTrue((self.runs / "run-old.log").exists())
        self.assertTrue((self.out / "步骤A.log").exists())

    def test_default_out_dir_uses_release_dir(self):
        self.assertFalse((self.tmp / "releases").exists())
        steps = [("步骤A", ["true"])]
        release_gate.build_steps = lambda *a, **k: steps

        def fake_run(name, cmd, log_path, echo=print):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("x", encoding="utf-8")
            return 0, 0.1

        release_gate._run_step = fake_run
        old = release_gate._run_step
        try:
            release_gate.run_gate(1, None, echo=lambda *_: None)
        finally:
            release_gate._run_step = old
        gates = list((self.tmp / "releases").glob("gate-*"))
        self.assertEqual(len(gates), 1)
        self.assertTrue((gates[0] / "GATE_REPORT.md").exists())


if __name__ == "__main__":
    unittest.main()
