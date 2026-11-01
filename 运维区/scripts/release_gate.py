#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""release_gate.py - HUB NEO 发版前一键门禁

用法：
  python 运维区/scripts/release_gate.py               # 单轮回归 → 满载 3 轮 → 实链路验收 → 便携 exe 构建冒烟 → exe 功能验收
  python 运维区/scripts/release_gate.py --rounds 5    # 满载跑 5 轮
  python 运维区/scripts/release_gate.py --out 目录     # 指定归档目录（默认 仓库区/releases/gate-<时间>）
  python 运维区/scripts/release_gate.py --no-exe      # 跳过便携 exe 两步（只改后端、不打包时）

纪律：
  · 任何一步非 0 立即熔断，后面的步骤不再跑，退出码 1；
  · 每步 stdout+stderr 完整落 `gate-<时间>/<步骤>.log`，终端只留该脚本自己的进度行
    （以 `[` 开头），不把 40KB 的测试输出灌进控制台；
  · 本轮 `运维区/data/test_runs/` 新增的 run-/stress- 日志一并归档进交付目录；
  · 归档目录写 `GATE_REPORT.md`：结论、步骤退出码与耗时、提交与工作区状态、归档清单。
"""

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # 运维区/scripts
ROOT = HERE.parent.parent                          # 仓库根
TEST_RUNS = ROOT / "运维区" / "data" / "test_runs"
RELEASE_DIR = ROOT / "仓库区" / "releases"
STRESS_ROUNDS = 3
# 只回显这些前缀：子脚本自己的进度标记，不含网关访问日志与用例明细。
ECHO_PREFIXES = ("[RUN]", "[LOG]", "[STRESS", "[PASS]", "[FAIL]", "=== [",
                 "LITE DESIGN")


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _say(msg: str) -> None:
    """行级 flush：重定向到文件时也要能实时 tail，不能等进程结束才吐。"""
    print(msg, flush=True)


def build_steps(python: str, rounds: int, with_exe: bool = True) -> list:
    """发版门禁的顺序与命令。改顺序只改这里，测试与报告共用同一份。

    `with_exe`：真打包一次（构建 + 冒烟 + 功能全量），打不开的包不能发。
    """
    steps = [
        # lite 切片放最前：几秒内就能判定（含设计闸门 + 真启动 + 接口），
        # 不过就别浪费后面的十几分钟回归。构建到临时目录，不碰 NEO LITE/。
        ("lite 构建与验收",
         [python, str(ROOT / "运维区" / "scripts" / "verify_lite_release.py"), "--build"]),
        ("全量回归", [python, str(ROOT / "编码区" / "scripts" / "run_tests.py")]),
        ("满载回归", [python, str(ROOT / "编码区" / "scripts" / "run_tests.py"),
                      "--stress", str(rounds)]),
        ("实链路验收", [python, str(ROOT / "运维区" / "scripts" / "verify_acceptance.py")]),
    ]
    if with_exe:
        # 先构建再验收：冒烟不过 build_exe 返回 4，功能验收不过返回 1，两者都会熔断。
        steps += [
            ("便携 exe 构建与冒烟", [python, str(ROOT / "运维区" / "scripts" / "build_exe.py"),
                                     "--smoke"]),
            ("便携 exe 功能验收", [python, str(ROOT / "运维区" / "scripts"
                                                / "verify_exe_functions.py")]),
        ]
    return steps


def _git_info() -> dict:
    def _out(*args):
        try:
            return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                                  text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    return {
        "commit": _out("rev-parse", "--short", "HEAD") or "—",
        "branch": _out("rev-parse", "--abbrev-ref", "HEAD") or "—",
        "dirty": len([x for x in _out("status", "--porcelain").splitlines() if x.strip()]),
    }


def _logs_before() -> set:
    if not TEST_RUNS.exists():
        return set()
    return {p.name for p in TEST_RUNS.glob("*.log")}


def _run_step(name: str, cmd: list, log_path: Path, echo=_say) -> tuple:
    """跑一步，输出全量落盘，只回显子脚本自己的进度行。返回 (rc, 秒)。

    白名单而非“以 [ 开头”：网关访问日志也是 `[HUB NEO 8888] ...`，
    否则一轮回归会把几千行请求日志灌进控制台。
    """
    started = datetime.datetime.now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write("[CMD] %s\n" % " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if line.startswith(ECHO_PREFIXES):
                echo("    " + line.rstrip())
        rc = proc.wait()
        log.write("[EXIT] %d\n" % rc)
    return rc, (datetime.datetime.now() - started).total_seconds()


def _archive_logs(out_dir: Path, before: set) -> list:
    """把本轮 test_runs 新增的日志复制进交付目录（不动原件）。"""
    copied = []
    if not TEST_RUNS.exists():
        return copied
    dest = out_dir / "test_runs"
    for path in sorted(TEST_RUNS.glob("*.log")):
        if path.name in before:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / path.name)
        copied.append(dest / path.name)
    return copied


def _report(out_dir: Path, stamp: str, rounds: int, results: list, copied: list,
            git: dict, verdict: str, failed_step: str, with_exe: bool = True) -> Path:
    lines = [
        "# 发版门禁报告 · %s" % stamp,
        "",
        "- **结论**：%s" % verdict,
        "- **满载轮次**：%d 轮" % rounds,
        "- **提交**：%s（分支 %s，工作区改动 %d 项）" % (git["commit"], git["branch"], git["dirty"]),
        "",
        "## 步骤",
        "",
        "| 步骤 | 退出码 | 耗时 | 日志 |",
        "|---|---|---|---|",
    ]
    for name, rc, secs, log_name in results:
        lines.append("| %s | %d | %.1fs | `%s` |" % (name, rc, secs, log_name))
    skipped = [s[0] for s in build_steps("python", rounds, with_exe)] if failed_step else []
    done = {r[0] for r in results}
    for name in skipped:
        if name not in done:
            lines.append("| %s | 未执行（已熔断） | — | — |" % name)
    if failed_step:
        lines += ["", "**熔断于**：%s —— 该步非 0，后续步骤未执行。" % failed_step]
    if with_exe:
        lines += ["", "## 便携 exe", "",
                  "- 构建产物：`NEO EXE/`（构建脚本自行隔离账本表与双击入口）",
                  "- 冒烟：`smoke_exe.py`（结构 + 真启动 + 关键接口）",
                  "- 功能：`verify_exe_functions.py`（静态资源 + 读/写接口 + KEY 生命周期 + 防护闸门）",
                  "- 未归档 zip：发版归档走 `build_exe.py --archive`，门禁只保证「包能跑、功能齐」。"]
    lines += ["", "## 归档", ""]
    if copied:
        lines += ["- `test_runs/%s`" % p.name for p in copied]
    else:
        lines.append("- 本轮没有新增 test_runs 日志")
    lines += ["",
              "> 步骤日志只留子脚本的进度行；逐用例明细在 `test_runs/` 下的 run-/stress- 日志里（原件仍在 `运维区/data/test_runs/`）。",
              "> 门禁脚本：`运维区/scripts/release_gate.py`。", ""]
    path = out_dir / "GATE_REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def run_gate(rounds: int = STRESS_ROUNDS, out_dir=None, echo=_say,
             with_exe: bool = True) -> int:
    stamp = _stamp()
    out_dir = Path(out_dir) if out_dir else (RELEASE_DIR / ("gate-" + stamp))
    out_dir.mkdir(parents=True, exist_ok=True)

    before = _logs_before()
    results, failed = [], ""
    verdict = "PASSED"
    echo("[GATE] 归档目录 %s" % out_dir)
    for name, cmd in build_steps(sys.executable, rounds, with_exe):
        echo("[GATE] >> %s" % name)
        log_path = out_dir / ("%s.log" % name)
        rc, secs = _run_step(name, cmd, log_path, echo=echo)
        results.append((name, rc, secs, log_path.name))
        if rc != 0:
            failed = name
            verdict = "FAILED"
            echo("[GATE] FAIL %s 退出码 %d（%.1fs）—— 熔断，后续步骤不执行" % (name, rc, secs))
            break
        echo("[GATE] OK %s（%.1fs）" % (name, secs))

    copied = _archive_logs(out_dir, before)
    report = _report(out_dir, stamp, rounds, results, copied, _git_info(), verdict, failed,
                     with_exe)
    if verdict == "PASSED":
        echo("[GATE PASSED] %d 项步骤全绿 · %d 份日志归档" % (len(results), len(copied)))
    else:
        echo("[GATE FAILED] 熔断于「%s」 · 报告 %s" % (failed, report))
    echo("[GATE] 报告 %s" % report)
    return 0 if verdict == "PASSED" else 1


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rounds = STRESS_ROUNDS
    out_dir = None
    with_exe = True
    for i, arg in enumerate(args):
        if arg == "--rounds":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if not nxt.isdigit():
                _say("--rounds 需要一个正整数")
                return 2
            rounds = max(1, int(nxt))
        elif arg.startswith("--rounds="):
            val = arg.split("=", 1)[1]
            if not val.isdigit():
                print("--rounds 需要一个正整数")
                return 2
            rounds = max(1, int(val))
        elif arg == "--out":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if not nxt:
                print("--out 需要一个目录")
                return 2
            out_dir = nxt
        elif arg == "--no-exe":
            with_exe = False
    return run_gate(rounds, out_dir, with_exe=with_exe)


if __name__ == "__main__":
    sys.exit(main())
