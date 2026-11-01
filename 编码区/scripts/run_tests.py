#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_tests.py - HUB NEO 一键回归测试脚本

用法：
  python 编码区/scripts/run_tests.py                # 普通单轮（日常门禁）
  python 编码区/scripts/run_tests.py --stress       # 满载 CPU 连跑 3 轮，轮轮全绿才算通过
  python 编码区/scripts/run_tests.py --stress 5     # 同上，5 轮（或 --stress=5）

纪律：
  · 全程输出（stdout + stderr）同时写 `运维区/data/test_runs/run-<时间>.log`，
    完整不截断，只保留最近 20 份；
  · 失败时终端只摆三件事：失败用例名、每个用例的首条异常行、stderr 摘要
    （头尾各 8 行 / ≤2KB），根因不用再从一屏噪声里翻；
  · `--stress`：先起 `核数-2` 个满载 CPU 子进程，再把全量回归**每轮跑在一个独立子进程**里
    （避免上一轮的内存状态把下一轮染色），轮次全绿才算通过；日志写 `stress-<时间>.log`；
  · 退出码 0=全绿 / 1=有失败，可直接当 DoD 门禁。
"""

import datetime
import http.server
import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 编码区/scripts
LOG_DIR = HERE.parent.parent / "运维区" / "data" / "test_runs"
KEEP_LOGS = 20                                   # 只留最近 N 份，避免越攒越多
STDERR_HEAD = 8                                  # stderr 摘要：头 8 行
STDERR_TAIL = 8                                  # stderr 摘要：尾 8 行
STDERR_MAX_BYTES = 2048                          # stderr 摘要总字节上限（AGENTS 日志护栏）
LINE_MAX_CHARS = 200                             # 单行截断，防一整行 JSON 铺满屏
STRESS_DEFAULT_ROUNDS = 3                        # --stress 不带轮数时的默认轮次
STRESS_ROUND_TIMEOUT = 1800                      # 单轮硬顶：跑不完不是抖动，是卡死


class _Tee:
    """把一份输出同时送到终端、日志文件，另可再留一份内存副本。"""

    def __init__(self, stream, log, capture=None):
        self.stream = stream
        self.log = log
        self.capture = capture

    def write(self, text):
        if not text:
            return 0
        for target in (self.stream, self.log, self.capture):
            if target is None:
                continue
            try:
                target.write(text)
            except UnicodeEncodeError:
                # 终端编码不认某些字符（GBK/CP1252）：写盘照旧，终端降级替换。
                target.write(text.encode(target.encoding or "utf-8", "replace")
                             .decode(target.encoding or "utf-8", "replace"))
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        for target in (self.stream, self.log, self.capture):
            if target is None:
                continue
            try:
                target.flush()
            except Exception:                    # noqa: BLE001 收尾失败不该掩盖用例结果
                pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _clip(text: str, limit: int = LINE_MAX_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _exception_line(traceback_text: str) -> str:
    """用例 traceback 的最后一条非空行 = 真正的异常行。"""
    for line in reversed(str(traceback_text or "").strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _failure_rows(result) -> list:
    rows = []
    for kind, items in (("FAIL", getattr(result, "failures", [])),
                        ("ERROR", getattr(result, "errors", []))):
        for test, detail in items:
            rows.append((kind, str(getattr(test, "id", lambda: test)()),
                         _exception_line(detail)))
    return rows


def _stderr_excerpt(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "[stderr] 无输出（没有 mock 服务器噪声或运行时警告）"
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) <= STDERR_HEAD + STDERR_TAIL:
        picked = lines
        note = "全部 %d 行" % len(lines)
    else:
        picked = (lines[:STDERR_HEAD]
                  + ["... 省略 %d 行 ..." % (len(lines) - STDERR_HEAD - STDERR_TAIL)]
                  + lines[-STDERR_TAIL:])
        note = "共 %d 行，头尾各 %d 行" % (len(lines), STDERR_HEAD)
    header = "[stderr] %s（%d 字节）:" % (note, len(raw.encode("utf-8")))
    budget = STDERR_MAX_BYTES
    body = []
    for line in picked:
        clipped = line[:LINE_MAX_CHARS]
        cost = len(clipped.encode("utf-8")) + 4
        if cost > budget:
            body.append("  ... 已到 %d 字节上限，其余见完整日志 ..." % STDERR_MAX_BYTES)
            break
        body.append("  " + clipped)
        budget -= cost
    return "\n".join([header] + body)


def _print_failure_report(result, rows, stderr_text, log_path):
    rule = "-" * 72
    print("")
    print("=" * 72)
    print("[TESTS FAILED] 失败 %d 项（FAIL %d / ERROR %d / skipped %d）"
          % (len(rows), len(getattr(result, "failures", [])),
             len(getattr(result, "errors", [])), len(getattr(result, "skipped", []))))
    for kind, test_id, detail in rows:
        print("  [%s] %s" % (kind, test_id))
        if detail:
            print("         %s" % _clip(detail))
    print(rule)
    print(_stderr_excerpt(stderr_text))
    print(rule)
    print("完整日志（stdout + stderr 全量）：%s" % log_path)


def _prune(log_dir: Path, keep: int = KEEP_LOGS):
    try:
        logs = []
        for pattern in ("run-*.log", "stress-*.log"):
            logs += list(log_dir.glob(pattern))
        logs.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in logs[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def _parse_args(argv):
    """→ `(rounds, child)`。rounds=0 表示普通单轮；child=True 表示压力门禁的子进程单轮。"""
    args = list(argv or [])
    if "--round" in args:
        return 0, True
    for i, arg in enumerate(args):
        if arg == "--stress":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            return (max(1, int(nxt)) if nxt.isdigit() else STRESS_DEFAULT_ROUNDS), False
        if arg.startswith("--stress="):
            val = arg.split("=", 1)[1]
            return (max(1, int(val)) if val.isdigit() else STRESS_DEFAULT_ROUNDS), False
    return 0, False


def _single(tests_dir, write_log: bool = True) -> int:
    """跑一轮全量回归。write_log=False 是 `--round` 子进程模式：只打屏，日志交给父进程归集。"""
    log = None
    log_path = None
    if write_log:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / ("run-%s.log" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        log = log_path.open("w", encoding="utf-8", newline="\n")
    stderr_capture = io.StringIO()
    out_tee = _Tee(sys.stdout, log)
    err_tee = _Tee(sys.stderr, log, stderr_capture)
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_tee, err_tee
    try:
        if log_path is not None:
            print("[RUN] %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            print("[LOG] %s" % log_path)
        # 测试自己起的 http.server 也是 stdlib 默认队列深度 5。满载（--stress 并行 10 份
        # 全量套件）时 accept 被饿住会让用例的客户端拿到 10053，跟被测代码无关。
        # 只调队列容量，不动任何断言；产品代码的同类修复在 core/server_runtime.py。
        http.server.ThreadingHTTPServer.request_queue_size = 128
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=str(tests_dir), pattern="test_*.py")
        runner = unittest.TextTestRunner(verbosity=2, stream=out_tee)
        result = runner.run(suite)

        if result.wasSuccessful():
            print("\n[ALL TESTS PASSED] HUB NEO 网关门禁全绿！")
            if log_path is not None:
                print("[LOG] %s" % log_path)
            return 0
        _print_failure_report(result, _failure_rows(result), stderr_capture.getvalue(),
                              log_path if log_path is not None else "见门禁汇总日志")
        return 1
    finally:
        out_tee.flush()
        err_tee.flush()
        sys.stdout, sys.stderr = old_out, old_err
        if log is not None:
            log.close()
            _prune(LOG_DIR)


_HOG_CODE = ("import time\n"
             "end = time.time() + %d\n"
             "x = 0\n"
             "while time.time() < end:\n"
             "    x = (x * 31 + 7) %% 1000003\n")


def _spawn_load(count: int, seconds: int) -> list:
    """起 count 个纯计算子进程把宿主 CPU 占满。子进程自带超时兜底：
    父进程被强杀时也不会留下永久烧 CPU 的孤儿。"""
    procs = []
    for _ in range(count):
        try:
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _HOG_CODE % int(seconds)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL))
        except OSError:
            break                                    # 进程起不来就少几个，不因负载而失败
    return procs


def _stop_load(procs) -> None:
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except Exception:                            # noqa: BLE001 收尾失败不该改写门禁结论
            try:
                proc.kill()
            except OSError:
                pass


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(str(text or "").splitlines()[-lines:])


def _safe_print(text: str) -> None:
    """打子进程输出：GBK 终端不认的字符降级替换，不让门禁自己因打印崩掉。"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(str(text).encode(enc, "replace").decode(enc, "replace"))


def _src_signature() -> tuple:
    """`编码区/src` 的 (py 文件数, 最大 mtime)，用来识别“跑动过程中源码被改了”。"""
    try:
        stats = [p.stat() for p in (HERE.parent / "src").rglob("*.py")]
    except OSError:
        return (0, 0.0)
    return (len(stats), max((st.st_mtime for st in stats), default=0.0))


def _stress(tests_dir, rounds: int) -> int:
    """满载 CPU 下连跑 rounds 轮全量回归：每轮都是独立子进程，轮轮全绿才算通过。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / ("stress-%s.log" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    log = log_path.open("w", encoding="utf-8", newline="\n")
    cores = os.cpu_count() or 4
    hogs = max(2, cores - 2)
    header = ("[STRESS] 负载 %d 个满载进程 / %d 核；轮次 %d；每轮独立进程跑全量回归"
              % (hogs, cores, rounds))
    print(header)
    print("[LOG] %s" % log_path)
    log.write(header + "\n[LOG] %s\n" % log_path)
    started_all = time.perf_counter()
    procs = _spawn_load(hogs, rounds * 150 + 120)
    try:
        for idx in range(1, rounds + 1):
            src_before = _src_signature()
            started = time.perf_counter()
            try:
                child = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--round"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=STRESS_ROUND_TIMEOUT)
                rc, out, err = child.returncode, child.stdout or "", child.stderr or ""
            except subprocess.TimeoutExpired:
                rc, out, err = 124, "", "单轮超过 %ds 未结束" % STRESS_ROUND_TIMEOUT
            cost = time.perf_counter() - started
            log.write("\n===== [STRESS r%d/%d] rc=%d %.1fs =====\n%s\n%s\n"
                      % (idx, rounds, rc, cost, out, err))
            log.flush()
            if rc == 0:
                print("[STRESS r%d/%d] OK  %.1fs" % (idx, rounds, cost))
                continue
            print("[STRESS r%d/%d] FAILED  rc=%d  %.1fs" % (idx, rounds, rc, cost))
            if _src_signature() != src_before:
                print("[STRESS] 注意：本轮期间 `编码区/src` 被改过 — 失败可能是未完成的编辑，"
                      "不是本轮的抖动/回归")
            _safe_print(_tail(out, 40))
            if err.strip():
                _safe_print(_tail(err, 12))
            print("[STRESS] 第 %d/%d 轮未全绿 → 门禁不通过；完整输出：%s"
                  % (idx, rounds, log_path))
            return 1
        total = time.perf_counter() - started_all
        print("[STRESS PASSED] %d/%d 轮全绿（负载 %d 个满载进程，共 %.1fs）"
              % (rounds, rounds, hogs, total))
        print("[LOG] %s" % log_path)
        return 0
    finally:
        _stop_load(procs)
        log.close()
        _prune(LOG_DIR)


def main(argv=None) -> int:
    # 测试里的 DISPATCH_LEDGER.clear() 只能清内存，绝不能抹掉生产派发轨迹库。
    os.environ.setdefault("HUB_DISPATCH_PERSIST", "0")
    tests_dir = HERE.parent / "tests"
    rounds, child = _parse_args(sys.argv[1:] if argv is None else argv)
    if child:
        return _single(tests_dir, write_log=False)
    if rounds:
        return _stress(tests_dir, rounds)
    return _single(tests_dir)


if __name__ == "__main__":
    sys.exit(main())
