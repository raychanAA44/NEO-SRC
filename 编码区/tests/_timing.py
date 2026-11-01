# -*- coding: utf-8 -*-
"""_timing.py - 墙钟预算测量的统一口径（时延类用例专用）

单次计时在负载下必然抖动：一次 GC、一次调度切片就能让 8ms 变成 10ms。
本模块把口径收成一处，所有时延预算断言都走这里：

  · **预热一轮不计时**：首笔含模块导入、目录首次扫描、OS/文件系统冷缓存与哈希缓存冷启动；
  · **多轮取最优**：最小值最接近“纯代码成本”，宿主调度只会加时不会减时；
  · **断言用 CPU 时间，不用墙钟**：`process_time` 量的是本进程真消耗的 CPU，
    宿主被别的进程占满时它基本不变；墙钟会被抢占直接放大（实测 12 个满载进程下
    墙钟 +5%，CPU 几乎不变），拿墙钟卡阈值只会误红；
  · **阈值不放宽**：预算仍是票面值，只去掉与实现无关的环境噪声；
  · **墙钟只在“预算本就是等待”时用**：子进程超时杀、网络超时熔断这类用例的成本
    就是等，CPU 时间恒为 0，此时用 `best_wall_ms`（同样预热 + 多轮取最优）。

用法::

    from _timing import best_ms

    # 纯动作
    cpu, wall = best_ms(lambda: mgr.snapshot(files))

    # 需要每轮先准备（准备耗时不进计时区）
    holder = {}
    def prepare():
        holder["sid"] = mgr.snapshot(["a.py"])
        (repo / "a.py").write_text("x\\n", encoding="utf-8")
    cpu, wall = best_ms(lambda: mgr.rollback(holder["sid"]), prepare=prepare)

    # 亚毫秒量级：一轮批量多次，总时除以次数
    cpu, wall = best_ms(lambda: [g.check("read_file") for _ in range(200)], rounds=5)
    per_call = cpu / 200

    # 纯等待型预算（子进程超时、网络熔断）：取最优墙钟毫秒
    from _timing import best_wall_ms
    best = best_wall_ms(lambda: g.execute_command_sandboxed(cmd, timeout=0.6), rounds=2)

断言写法（两者都进消息，方便一眼看出是代码慢还是机器忙）::

    self.assertLess(cpu, 1.0, "单次决策 cpu=%.3fms / wall=%.3fms 应 < 1ms" % (cpu, wall))
"""

import os
import shutil
import tempfile
import time

DEFAULT_ROUNDS = 3
# 亚毫秒级动作（<1ms）在 Windows 上既不能拿 CPU 量（process_time 步长 15.6ms），
# 也不能在宿主满载时拿墙钟量（实测 rename 从 0.1ms 涨到 10ms）。
# 基准 rename 超过这个值即认为“此刻不可测”，用例 skip 而不是误红。
REFERENCE_LIMIT_MS = 1.0


def best_ms(action, prepare=None, rounds: int = DEFAULT_ROUNDS, warmup: bool = True):
    """预热 + 多轮取最优，返回 `(最优 CPU 毫秒, 最优墙钟毫秒)`。

    prepare：每轮计时前执行、不计时的准备工作（可为 None）。
    断言请用 CPU 那一项：墙钟只作参考（wall 大而 cpu 正常 = 机器忙，不是代码慢）。
    """
    def once():
        if prepare is not None:
            prepare()
        wall0, cpu0 = time.perf_counter(), time.process_time()
        action()
        return ((time.process_time() - cpu0) * 1000,
                (time.perf_counter() - wall0) * 1000)

    if warmup:
        once()
    samples = [once() for _ in range(max(1, int(rounds)))]
    return (min(s[0] for s in samples), min(s[1] for s in samples))


def reference_wall_ms(rounds: int = 5) -> float:
    """基准动作（写 64B + 同目录原子替换）的最优墙钟毫秒：判断宿主此刻能不能测亚毫秒时延。

    空闲机器约 0.4ms；被 10 个满载进程压住会到 1.7ms 以上，此时任何 6ms 级绝对预算都不可测。
    """
    folder = tempfile.mkdtemp(prefix="timing_ref_")
    target = os.path.join(folder, "t.bin")
    tmp = os.path.join(folder, "t.tmp")

    def once():
        with open(tmp, "wb") as handle:
            handle.write(b"x" * 64)
        started = time.perf_counter()
        os.replace(tmp, target)
        return (time.perf_counter() - started) * 1000

    try:
        once()
        return min(once() for _ in range(max(1, int(rounds))))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def best_wall_ms(action, prepare=None, rounds: int = DEFAULT_ROUNDS, warmup=None):
    """预热 + 多轮取最优**墙钟**，返回最优墙钟毫秒。

    只在“预算覆盖的是等待而非计算”时使用（子进程超时杀、网络超时熔断）：
    这类用例的 CPU 时间恒为 0，墙钟才是被测行为本身。取最小值同样只为剔除
    宿主抢占带来的单向放大——被抢占只会更慢，不会更快。

    warmup：预热动作，默认跑一轮 action；给一个更便宜的代表动作可省测试时间。
    """
    def once():
        if prepare is not None:
            prepare()
        t0 = time.perf_counter()
        action()
        return (time.perf_counter() - t0) * 1000

    if warmup is None:
        once()                       # 完整一轮（含 prepare），否则 prepare 里备好的状态还没就位
    else:
        warmup()
    return min(once() for _ in range(max(1, int(rounds))))


__all__ = ["best_ms", "best_wall_ms", "reference_wall_ms", "DEFAULT_ROUNDS",
           "REFERENCE_LIMIT_MS"]
