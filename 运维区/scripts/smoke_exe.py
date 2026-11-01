#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_exe.py —— 便携 exe 的自动化冒烟：结构体检 → 真启动 → 打接口 → 收尾。

为什么要有这个脚本：`build_exe.py` 只保证「打出来了」，不保证「打出来能跑」。
2026-10-26 首次封装就翻过两次车（UI 目录解析错 → 全站 404；数据写回源码仓），
两类问题都只有真起进程打接口才看得见。

用法：

    python 运维区/scripts/smoke_exe.py                     # 默认查仓库根 NEO EXE/
    python 运维区/scripts/smoke_exe.py --dir "NEO EXE"
    python 运维区/scripts/smoke_exe.py --sqlite            # 本机无 MySQL 时临时改口径
    python 运维区/scripts/smoke_exe.py --keep              # 保留临时数据目录供排查

隔离纪律：数据一律落到临时目录（`HUB_DATA_DIR`），交付件里的账本与密钥仓一个字节都不碰。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# 源码在 NEO SRC/ 下，交付件落工作区根（与 NEO LITE/ 并列）。
DEFAULT_DIR = ROOT.parent / "NEO EXE"
EXE_NAME = "HUBNEO.exe"
LAUNCHER_NAME = "启动 HUB NEO.cmd"
STOPPER_NAME = "停止 HUB NEO.cmd"
MANUAL_NAME = "使用说明.md"
STARTUP_TIMEOUT_S = 45.0


# --------------------------------------------------------------------------- 结构


def check_layout(delivery: Path) -> list[tuple[bool, str, str]]:
    """静态体检：交付件该有的都有、不该有的都没有。返回 [(ok, 名称, 说明)]。"""
    results: list[tuple[bool, str, str]] = []
    exe = delivery / EXE_NAME
    internal = delivery / "_internal"
    ui = internal / "编码区" / "src" / "ui"

    results.append((exe.is_file(), "主程序", str(exe)))
    results.append((internal.is_dir(), "_internal 运行时", str(internal)))
    # 全站 404 的根因就是这条：UI 必须落在 _internal 里，server 才找得到
    results.append(((ui / "index.html").is_file(), "控制台首页资源",
                    str(ui / "index.html")))
    results.append(((ui / "assets" / "js" / "app.js").is_file(), "前端脚本",
                    str(ui / "assets" / "js" / "app.js")))

    configs = sorted((delivery / "运维区" / "configs").glob("*.json"))
    results.append((len(configs) >= 5, "外部配置",
                    f"{len(configs)} 个 json：{delivery / '运维区' / 'configs'}"))

    results.append(((delivery / LAUNCHER_NAME).is_file(), "双击启动入口",
                    str(delivery / LAUNCHER_NAME)))
    results.append(((delivery / STOPPER_NAME).is_file(), "双击停止入口",
                    str(delivery / STOPPER_NAME)))
    results.append(((delivery / MANUAL_NAME).is_file(), "使用说明",
                    str(delivery / MANUAL_NAME)))

    # 密钥 / 账本绝不允许进包：进了包等于随分发泄露
    leaked = [p for p in internal.rglob("*") if p.is_dir() and p.name == "data"
              and p.parent.name == "运维区"] if internal.is_dir() else []
    results.append((not leaked, "包内无 运维区/data",
                    "干净" if not leaked else ", ".join(str(p) for p in leaked)))
    secrets = [p for p in internal.rglob("*")
               if p.is_file() and (p.suffix in (".key", ".db", ".sqlite")
                                   or "creds" in p.name.lower())] \
        if internal.is_dir() else []
    results.append((not secrets, "包内无密钥/账本文件",
                    "干净" if not secrets else ", ".join(str(p) for p in secrets)))
    return results


def _snapshot(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- 运行


def free_port(preferred: int = 8899) -> int:
    """优先用 8899；被占就让系统给一个，冒烟不该因为端口撞车失败。"""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("找不到可用端口")


def launch(delivery: Path, port: int, data_dir: Path,
           ledger_backend: str | None, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["HUB_NEO_PORT"] = str(port)
    env["HUB_DATA_DIR"] = str(data_dir)
    env["HUB_OPEN_BROWSER"] = "0"          # 冒烟不弹浏览器
    if ledger_backend:
        env["HUB_LEDGER_BACKEND"] = ledger_backend
    # 输出落文件而不是 PIPE：网关每个请求都会打一行，管道不读会写堵塞死进程。
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        return subprocess.Popen(
            [str(delivery / EXE_NAME)],
            cwd=str(delivery), env=env,
            stdout=log, stderr=subprocess.STDOUT, text=True,
        )


def http_get(port: int, path: str, timeout: float = 10.0) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_health(proc: subprocess.Popen, port: int,
                timeout: float = STARTUP_TIMEOUT_S) -> tuple[bool, str]:
    """等 /health 通；进程提前退出就立刻返回（别白等到超时）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"进程提前退出，退出码 {proc.returncode}"
        try:
            status, body = http_get(port, "/health", timeout=2.0)
            if status == 200:
                return True, body
        except Exception:
            pass
        time.sleep(0.4)
    return False, f"{timeout:.0f}s 内 /health 未就绪"


def probe(port: int) -> list[tuple[bool, str, str]]:
    """接口体检：控制台、静态资源、三个本地读接口。"""
    results: list[tuple[bool, str, str]] = []

    status, body = http_get(port, "/health")
    health = {}
    try:
        health = json.loads(body) if isinstance(body, str) else {}
    except Exception:
        health = {}
    results.append((status == 200 and health.get("service") == "hub-neo-gateway",
                    "GET /health", f"{status} · version={health.get('version')} "
                                    f"· ledger={health.get('ledger_backend')}"))
    results.append((health.get("port") == port, "端口自述一致",
                    f"health.port={health.get('port')} / 实际 {port}"))
    results.append((bool(health.get("ledger_backend")), "账本后端已选",
                    str(health.get("ledger_backend"))))

    status, body = http_get(port, "/")
    results.append((status == 200 and "<html" in body.lower(), "GET / 控制台首页",
                    f"{status} · {len(body)} 字节"))

    status, body = http_get(port, "/assets/js/app.js")
    results.append((status == 200 and len(body) > 100, "GET 静态资源 app.js",
                    f"{status} · {len(body)} 字节"))

    for path, predicate, label in (
        ("/api/channels/overview", lambda d: isinstance(d, dict), "GET 渠道总览"),
        ("/api/pricing", lambda d: isinstance(d, dict), "GET 价目"),
        ("/api/routes/policy", lambda d: isinstance(d, dict), "GET 路由策略"),
    ):
        status, body = http_get(port, path)
        payload = None
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        results.append((status == 200 and isinstance(payload, dict), label,
                        f"{status} · {len(body)} 字节"))
    return results


def shutdown(proc: subprocess.Popen, grace: float = 15.0) -> str:
    """先 terminate 再 kill；返回收尾描述（残留进程要被看见，不能静默）。"""
    if proc.poll() is not None:
        return f"已自行退出（码 {proc.returncode}）"
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace)
        return "terminate 超时，已强杀"
    return f"已停止（码 {proc.returncode}）"


def _log_tail(log_path: Path, limit: int = 15) -> str:
    try:
        out = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        out = ""
    lines = [line for line in out.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


# --------------------------------------------------------------------------- 入口


def run_smoke(delivery: Path, ledger_backend: str | None = None,
              keep: bool = False) -> int:
    delivery = Path(delivery).resolve()
    print(f"[smoke] 交付目录: {delivery}")
    results = check_layout(delivery)
    for ok, name, detail in results:
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}: {detail}")
    if not all(ok for ok, _, _ in results):
        print("[smoke] 结论: FAIL（结构体检未过，不启动进程）")
        return 1

    port = free_port()
    work_dir = Path(tempfile.mkdtemp(prefix="neo-smoke-"))
    # 数据目录故意不给它建：能不能自己建出 运维区/data 也是体检项。
    data_dir = work_dir / "data"
    log_path = work_dir / "console.log"
    before = _snapshot(delivery / "运维区" / "data")
    print(f"[smoke] 启动 exe：端口 {port} · 数据目录 {data_dir}")
    proc = launch(delivery, port, data_dir, ledger_backend, log_path)
    exit_code = 1
    try:
        ready, detail = wait_health(proc, port)
        if not ready:
            print(f"[smoke] 启动失败：{detail}")
            tail = _log_tail(log_path)
            if tail:
                print("[smoke] 进程输出（末 15 行）:\n" + tail)
            print("[smoke] 结论: FAIL（启动未就绪）")
            return 1
        probe_results = probe(port)
        after = _snapshot(delivery / "运维区" / "data")
        probe_results.append((not (after - before), "交付件数据目录未被写入",
                              "干净" if not (after - before)
                              else ", ".join(sorted(after - before))))
        probe_results.append((data_dir.is_dir() and any(data_dir.iterdir()),
                              "运行时数据落在指定目录",
                              f"{data_dir} · " + ("已写入" if data_dir.is_dir()
                                                  and any(data_dir.iterdir()) else "空")))
        for ok, name, info in probe_results:
            print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}: {info}")
        results = probe_results
        exit_code = 0 if all(ok for ok, _, _ in results) else 1
    finally:
        print(f"[smoke] 收尾: {shutdown(proc)}")
        if keep:
            print(f"[smoke] 临时目录保留: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"[smoke] 结论: {'PASS' if exit_code == 0 else 'FAIL'} · {passed}/{total} 项")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HUB NEO 便携 exe 冒烟")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="交付目录")
    parser.add_argument("--sqlite", action="store_true",
                        help="强制 sqlite 账本（本机 MySQL 不可用时）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = parser.parse_args(argv)
    try:
        return run_smoke(Path(args.dir),
                         ledger_backend="sqlite" if args.sqlite else None,
                         keep=args.keep)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
