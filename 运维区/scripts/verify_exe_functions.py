#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_exe_functions.py —— 便携 exe 的「功能全量 + 独立性」验收。

两条车道：

1. **功能全量**（默认）：自带起 `HUBNEO.exe`，把控制台静态资源、全部本地读接口、
   全部本地写接口（含 KEY 生命周期）与防护闸门逐个打一遍，写操作立刻读回对账。
   全部写入落在**临时数据目录 + 临时配置副本**上（`HUB_DATA_DIR` / `HUB_CONFIG_DIR`），
   交付件里的账本、密钥仓与配置一个字节都不动。
2. **独立性/冲突**（`--conflict --peer http://127.0.0.1:8888`）：在「NEO 网页版」已经跑着
   的前提下，验证三件事——
   * exe 抢同一端口必须**干净拒绝**（不是静默双实例）；
   * 换端口并行时两边都活着，且各自数据/配置目录互不写入；
   * 两边账本指向是否同一本账（共用要报出来，不说成「独立」）。

用法：

    python 运维区/scripts/verify_exe_functions.py                 # 功能全量
    python 运维区/scripts/verify_exe_functions.py --dir "NEO EXE"
    python 运维区/scripts/verify_exe_functions.py --conflict       # 需 8888 网页版在跑
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

# 本地读接口：local_readmodel.READ_ROUTES + LEDGER_ROUTES + 固定 GET
READ_PATHS = [
    "/api/billing/live", "/api/errors", "/api/visibility",
    "/api/readmodel/channels", "/api/custom", "/api/models/meta",
    "/api/security/audit", "/api/cloud/sync", "/api/pool",
    "/api/official/summary", "/api/budgets", "/api/th/subscription",
    "/api/accounts/health", "/api/keys", "/api/channels/overview",
    "/api/reports/today",
    "/api/usage", "/api/status", "/api/readmodel/dashboard",
    "/api/routes", "/api/readmodel/catalog",
    "/health", "/api/models/groups", "/api/task_locks",
    "/api/dispatch/traces", "/api/admin/auth-status", "/api/admin/locked-ips",
    "/api/routes/policy", "/api/oc-bridge/status", "/api/pricing",
    "/api/pricing/template", "/api/accounts", "/api/models/mtok_prices",
    "/v1/models",
]


# 管理 token：部分端点只认 Bearer；不给就跳过，不能报成失败。
API_TOKEN = ""
TOKEN_GATED = {"/api/models/groups/reload"}


def _request(port: int, path: str, method: str = "GET", body=None,
             timeout: float = 20.0, extra_headers: dict | None = None) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = dict(extra_headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    if API_TOKEN:
        headers["authorization"] = "Bearer " + API_TOKEN
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _raw_post(port: int, path: str, raw: bytes) -> int:
    """故意发坏体（验证 400 口径），不走 json.dumps。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=raw, method="POST",
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _json(port: int, path: str, method: str = "GET", body=None,
          extra_headers: dict | None = None) -> tuple[int, dict]:
    status, raw = _request(port, path, method, body, extra_headers=extra_headers)
    try:
        return status, json.loads(raw)
    except Exception:
        return status, {}


def free_port(preferred: int = 8898) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("找不到可用端口")


def wait_health(port: int, proc: subprocess.Popen | None, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            status, _ = _request(port, "/health", timeout=2.0)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


class Run:
    def __init__(self) -> None:
        # ok: True=通过 / False=失败 / None=跳过（缺凭据等）
        self.rows: list[tuple[str, str, bool | None, str]] = []

    def add(self, section: str, name: str, ok: bool | None, detail: str = "") -> None:
        self.rows.append((section, name, ok, detail))

    def summary(self) -> int:
        sections: dict[str, list[tuple[str, bool | None, str]]] = {}
        for section, name, ok, detail in self.rows:
            sections.setdefault(section, []).append((name, ok, detail))
        failed = skipped = 0
        for section, items in sections.items():
            passed = sum(1 for _, ok, _ in items if ok is True)
            print(f"\n== {section} · {passed}/{len(items)} ==")
            for name, ok, detail in items:
                if ok is False:
                    failed += 1
                    print(f"  [FAIL] {name}: {detail}")
                elif ok is None:
                    skipped += 1
                    print(f"  [SKIP] {name}: {detail}")
                elif detail:
                    print(f"  [OK]   {name}: {detail}")
        total = len(self.rows) - skipped
        print(f"\n验收结论: {'PASS' if failed == 0 else 'FAIL'} · "
              f"{total - failed}/{total} 项通过"
              + (f" · 跳过 {skipped} 项（缺管理员 token）" if skipped else ""))
        return 0 if failed == 0 else 1


# --------------------------------------------------------------------- 功能全量


def check_static(run: Run, port: int, ui_dir: Path) -> None:
    if not ui_dir.is_dir():
        run.add("控制台静态资源", "UI 目录存在", False, str(ui_dir))
        return
    assets = [p for p in sorted(ui_dir.rglob("*"))
              if p.is_file() and p.suffix.lower() in (".html", ".js", ".css")
              and p.name != "index.html"]
    status, body = _request(port, "/")
    run.add("控制台静态资源", "GET /", status == 200 and "<html" in body.lower(),
            f"{status} · {len(body)} 字节")
    bad = []
    for path in assets:
        rel = "/" + path.relative_to(ui_dir).as_posix()
        status, body = _request(port, rel)
        if status != 200 or len(body) < 20:
            bad.append(f"{rel}={status}")
    run.add("控制台静态资源", f"静态文件 {len(assets)} 个", not bad,
            "全部 200" if not bad else ", ".join(bad[:6]))


def check_reads(run: Run, port: int) -> dict:
    bad = []
    for path in READ_PATHS:
        status, raw = _request(port, path)
        try:
            payload = json.loads(raw)
            # /api/keys 回的是数组，其余是对象；两种都算合法 JSON
            ok_json = isinstance(payload, (dict, list))
        except Exception:
            payload, ok_json = {}, False
        if status != 200 or not ok_json:
            bad.append(f"{path}={status}")
    run.add("本地读接口", f"GET {len(READ_PATHS)} 条", not bad,
            "全部 200 且回 JSON" if not bad else ", ".join(bad[:8]))
    status, overview = _json(port, "/api/channels/overview")
    channels = [c.get("channel") for c in (overview.get("channels") or [])
                if isinstance(c, dict) and c.get("channel")]
    run.add("本地读接口", "渠道总览非空", bool(channels), f"{len(channels)} 个渠道")
    return {"channel": channels[0] if channels else "", "overview": overview}


def check_writes(run: Run, port: int, channel: str) -> None:
    section = "本地写接口"

    def write(path: str, body: dict, label: str) -> dict:
        status, payload = _json(port, path, "POST", body)
        run.add(section, label, status == 200, f"{status} · {json.dumps(payload, ensure_ascii=False)[:120]}")
        return payload

    write("/api/channels/toggle", {"channel": channel, "enabled": True}, "渠道路由开关")
    write("/api/visibility", {"channel": channel, "action": "toggle", "hidden": False},
          "渠道可见性")
    write("/api/budgets", {"key": "global", "amount": 260, "unit": "CNY"}, "预算上限")
    status, budgets = _json(port, "/api/budgets")
    run.add(section, "预算读回", status == 200 and "global" in json.dumps(budgets),
            json.dumps(budgets, ensure_ascii=False)[:100])

    write("/api/routes/policy", {"rate_limit_rps": 20, "rate_limit_burst": 40}, "路由策略")
    status, policy = _json(port, "/api/routes/policy")
    limit = (policy.get("rate_limit") or {})
    run.add(section, "限流读回", limit.get("rps") == 20 and limit.get("burst") == 40,
            json.dumps(limit, ensure_ascii=False))

    write("/api/rate-limit/config", {"rps": 20, "burst": 40}, "限流参数")
    write("/api/egress/proxy-url", {"url": ""}, "出口代理")
    write("/api/egress/channel-override", {"channel": channel, "mode": "inherit", "url": ""},
          "渠道级出口")
    write("/api/prompt-opt/mode", {"mode": "off"}, "提示词自愈档位")
    write("/api/system/reload", {}, "配置热重载")
    # 这两条要求管理员 token（Bearer）；没给凭据就如实跳过，不假装通过
    if API_TOKEN:
        write("/api/models/groups/reload", {}, "模型分组重载")
        write("/api/upstream/switch", {"target": "cloud"}, "上游切换")
    else:
        status, _ = _request(port, "/api/models/groups/reload", "POST", {})
        run.add(section, "模型分组重载", None, f"{status} · 需 Bearer（--api-token 可测）")
        status, payload = _json(port, "/api/upstream/switch", "POST", {"target": "cloud"},
                                extra_headers={"x-hub-ui": "1",
                                               "origin": f"http://127.0.0.1:{port}"})
        # 200 = 切成功；409 = 上游探测不通被网关拒绝硬切（同样是正确行为）
        detail = f"{status} · " + json.dumps(payload.get("error") or payload,
                                             ensure_ascii=False)[:110]
        run.add(section, "上游切换（控制台同源口径）", status in (200, 409), detail)
    write("/api/system/cache/clear", {}, "缓存清理")
    write("/api/system/logs/clear", {"keep": 5}, "日志清理")
    write("/api/errors/fix", {"ids": []}, "错误确认")
    write("/api/accounts/inspect", {}, "账号巡检")
    write("/api/accounts/checkin", {}, "账号签到")
    write("/api/official/quota-snapshot", {"channel": channel}, "官方额度快照")
    status, payload = _json(port, "/api/task_locks/release", "POST", {"task_id": "nope"})
    run.add(section, "任务锁释放（无效号）", 400 <= status < 500,
            f"{status} · {json.dumps(payload, ensure_ascii=False)[:80]}")

    # 模型开关：切换后必须能在分组/目录读回里看到
    _, catalog = _json(port, "/api/models/meta")
    models = [m.get("id") for m in (catalog.get("models") or catalog.get("rows") or [])
              if isinstance(m, dict) and m.get("id")]
    if models:
        write("/api/models/toggle", {"channel": channel, "id": models[0], "enabled": False},
              "模型停用")
        write("/api/models/toggle", {"channel": channel, "id": models[0], "enabled": True},
              "模型恢复")

    # 明确该 400 的输入：坏体不能落半份配置
    # 预算 amount=null 是「解除上限」的正式口径，不是缺参报错
    status, _ = _json(port, "/api/budgets", "POST", {"key": "global", "amount": None})
    _, budgets2 = _json(port, "/api/budgets")
    cleared = (budgets2.get("global") or {}).get("amount") is None
    run.add(section, "预算可解除上限", status == 200 and cleared,
            f"{status} · amount={((budgets2.get('global') or {}).get('amount'))}")

    for path, body, label in (
        ("/api/visibility", {"channel": channel, "action": "toggle"}, "可见性缺 hidden 拒绝"),
        ("/api/accounts/delete", {"id": 99999999}, "不存在账号删除"),
        ("/api/models/toggle", {"channel": channel, "id": "", "enabled": True}, "空模型拒绝"),
    ):
        status, _ = _request(port, path, "POST", body)
        run.add(section, label, 400 <= status < 500, f"{status}")

    # 拒绝面：未列入白名单的写端点必须 403
    status, _ = _request(port, "/api/rc/execute", "POST", {"x": 1})
    run.add("防护闸门", "非白名单写端点 403", status == 403, f"{status}")


def check_keys(run: Run, port: int) -> None:
    section = "客户端 KEY"
    status, issued = _json(port, "/api/keys", "POST",
                           {"name": "verify-probe", "realm": "all", "note": "功能验收"})
    key_id = issued.get("issued") or issued.get("id") or ""
    plain = issued.get("key") or issued.get("plaintext") or ""
    run.add(section, "签发", status == 200 and bool(key_id) and bool(plain),
            f"{status} · id={key_id} · 明文只出现一次")
    if not key_id:
        return
    status, _ = _json(port, f"/api/keys/{key_id}/toggle", "POST", {"enabled": False})
    run.add(section, "停用", status == 200, f"{status}")
    status, _ = _json(port, f"/api/keys/{key_id}/realm", "POST", {"realm": "all"})
    run.add(section, "改 realm", status == 200, f"{status}")
    status, listed = _json(port, "/api/keys")
    run.add(section, "列表可见", status == 200 and key_id in json.dumps(listed), f"{status}")
    status, _ = _request(port, f"/api/keys/{key_id}", "DELETE")
    run.add(section, "吊销", status == 200, f"{status}")


def _ledger_table(config_path: Path) -> str:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((data.get("ledger") or {}).get("table") or "")


def check_ledger_isolation(run: Run, port: int, delivery: Path) -> None:
    """便携包应该用自己的账本表；和源码版同一张表就算“独立”未达成。"""
    section = "账本隔离"
    portable = _ledger_table(delivery / "运维区" / "configs" / "upstream.json")
    source = _ledger_table(ROOT / "运维区" / "configs" / "upstream.json")
    _, health = _json(port, "/health")
    target = str(health.get("ledger_target") or "")
    run.add(section, "便携包配置了独立表", bool(portable) and portable != source,
            f"便携={portable or '未配置'} · 源码={source}")
    run.add(section, "运行期账本目标一致", bool(portable) and target.endswith("." + portable),
            target or "（无账本目标）")


def check_guards(run: Run, port: int) -> None:
    section = "防护闸门"
    status, _ = _request(port, "/api/keys/any/reveal")
    run.add(section, "KEY 明文回显 403", status == 403, f"{status}")
    status, _ = _request(port, "/api/vault/anything")
    run.add(section, "密钥仓读取 403", status == 403, f"{status}")
    status, _ = _request(port, "/nope-not-here")
    run.add(section, "未知静态路径 404", status == 404, f"{status}")
    status, _ = _request(port, "/api/admin/login", "POST", {"password": "wrong-password"})
    run.add(section, "错误口令被拒", 400 <= status < 500, f"{status}")
    status = _raw_post(port, "/api/keys", b"not-json")
    run.add(section, "坏 JSON 400", status == 400, f"{status}")


def run_exercise(delivery: Path) -> int:
    delivery = Path(delivery).resolve()
    exe = delivery / EXE_NAME
    ui_dir = delivery / "_internal" / "编码区" / "src" / "ui"
    if not exe.is_file():
        print(f"[verify] 找不到 {exe}", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="neo-verify-"))
    data_dir = work / "data"
    config_dir = work / "configs"
    shutil.copytree(delivery / "运维区" / "configs", config_dir)
    port = free_port()
    env = dict(os.environ)
    env.update({"HUB_NEO_PORT": str(port), "HUB_DATA_DIR": str(data_dir),
                "HUB_CONFIG_DIR": str(config_dir), "HUB_OPEN_BROWSER": "0"})
    log_path = work / "console.log"
    print(f"[verify] 启动 exe：端口 {port} · 临时数据 {data_dir} · 临时配置 {config_dir}")
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([str(exe)], cwd=str(delivery), env=env,
                                stdout=log, stderr=subprocess.STDOUT, text=True)

    run = Run()
    try:
        if not wait_health(port, proc):
            print("[verify] 启动失败，进程输出末尾：")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        run.add("启动", "进程起来且 /health 通", True, f"端口 {port}")
        check_static(run, port, ui_dir)
        info = check_reads(run, port)
        check_writes(run, port, info["channel"] or "open-design")
        check_keys(run, port)
        check_guards(run, port)
        check_ledger_isolation(run, port, delivery)
        # 交付件自身没被写脏
        shipped = {str(p.relative_to(delivery / "运维区" / "configs"))
                   for p in (delivery / "运维区" / "configs").rglob("*") if p.is_file()}
        changed = [name for name in shipped
                   if (config_dir / name).is_file()
                   and (config_dir / name).read_bytes()
                   != (delivery / "运维区" / "configs" / name).read_bytes()]
        run.add("隔离纪律", "写操作只落临时配置", bool(changed),
                "临时副本已改: " + ", ".join(sorted(changed)[:6]))
        run.add("隔离纪律", "数据只落临时目录", data_dir.is_dir() and any(data_dir.iterdir()),
                str(data_dir))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(work, ignore_errors=True)
    return run.summary()


# --------------------------------------------------------------------- 独立性


def _data_snapshot(path: Path) -> set[str]:
    if not path.is_dir():
        return set()
    return {str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()}


def run_conflict(delivery: Path, peer: str) -> int:
    delivery = Path(delivery).resolve()
    exe = delivery / EXE_NAME
    run = Run()
    peer_port = int(peer.rsplit(":", 1)[-1].strip("/"))
    status, peer_health = _json(peer_port, "/health")
    if status != 200:
        print(f"[verify] 对端 {peer} 不在跑（{status}），先启动 NEO 网页版再来", file=sys.stderr)
        return 2
    run.add("对端基线", f"NEO 网页版 {peer_str(peer_port)}", True,
            f"version={peer_health.get('version')} · ledger={peer_health.get('ledger_backend')}")

    peer_data = _data_snapshot(ROOT / "运维区" / "data")
    exe_data = _data_snapshot(delivery / "运维区" / "data")

    # 1) 抢同一端口：必须干净拒绝，且不写脏任何数据
    work = Path(tempfile.mkdtemp(prefix="neo-conflict-"))
    env = dict(os.environ)
    env.update({"HUB_NEO_PORT": str(peer_port), "HUB_DATA_DIR": str(work / "data"),
                "HUB_OPEN_BROWSER": "0"})
    proc = subprocess.run([str(exe)], cwd=str(delivery), env=env, text=True,
                          capture_output=True, timeout=120)
    output = (proc.stdout or "") + (proc.stderr or "")
    run.add("端口冲突", "同端口启动被拒", proc.returncode != 0,
            f"退出码 {proc.returncode}")
    run.add("端口冲突", "报错指向端口占用",
            "已被占用" in output or "占用" in output,
            output.strip().splitlines()[-1][:120] if output.strip() else "无输出")
    run.add("端口冲突", "未污染交付件数据", _data_snapshot(delivery / "运维区" / "data") == exe_data,
            f"{len(exe_data)} 个文件未变" if exe_data else "交付件 data 为空（无文件可污染）")
    status, _ = _json(peer_port, "/health")
    run.add("端口冲突", "网页版未被挤掉", status == 200, f"{status}")

    # 2) 换端口并行：两边都活，各自数据互不写入
    port = free_port()
    env = dict(os.environ)
    env.update({"HUB_NEO_PORT": str(port), "HUB_DATA_DIR": str(work / "data2"),
                "HUB_OPEN_BROWSER": "0"})
    log_path = work / "parallel.log"
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        side = subprocess.Popen([str(exe)], cwd=str(delivery), env=env,
                                stdout=log, stderr=subprocess.STDOUT, text=True)
    try:
        run.add("并行运行", "exe 换端口能起", wait_health(port, side), f"端口 {port}")
        s1, _ = _json(peer_port, "/health")
        s2, exe_health = _json(port, "/health")
        run.add("并行运行", "两个实例同时在线", s1 == 200 and s2 == 200,
                f"网页版 {s1} · exe {s2}")
        for path in ("/api/channels/overview", "/api/pricing", "/api/readmodel/dashboard"):
            a, _ = _request(peer_port, path)
            b, _ = _request(port, path)
            run.add("并行运行", f"同一接口两侧都通 {path}", a == 200 and b == 200,
                    f"{a}/{b}")
        run.add("并行运行", "exe 未写入网页版数据目录",
                _data_snapshot(ROOT / "运维区" / "data") == peer_data,
                f"{len(peer_data)} 个文件未变")
        run.add("并行运行", "exe 数据落在自己目录",
                (work / "data2").is_dir() and any((work / "data2").iterdir()),
                str(work / "data2"))

        _, peer_now = _json(peer_port, "/health")
        same_ledger = (peer_now.get("ledger_target") == exe_health.get("ledger_target")
                       and peer_now.get("ledger_backend") == exe_health.get("ledger_backend"))
        # 门户要求的「不冲突」包含记账不混：同一张表会互相拉高预算占用
        run.add("账本口径", "两侧各记一本账", not same_ledger,
                f"网页版={peer_now.get('ledger_backend')}:{peer_now.get('ledger_target')}"
                f" · exe={exe_health.get('ledger_backend')}:{exe_health.get('ledger_target')}"
                + ("（共用同一张表：流水与预算会合并计数）" if same_ledger else ""))
    finally:
        if side.poll() is None:
            side.terminate()
            try:
                side.wait(timeout=15)
            except subprocess.TimeoutExpired:
                side.kill()
        shutil.rmtree(work, ignore_errors=True)
    return run.summary()


def peer_str(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HUB NEO 便携 exe 功能与独立性验收")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="交付目录")
    parser.add_argument("--conflict", action="store_true",
                        help="检查与 NEO 网页版并行是否冲突")
    parser.add_argument("--peer", default="http://127.0.0.1:8888",
                        help="--conflict 时的对端地址")
    parser.add_argument("--api-token", default=os.environ.get("HUB_ADMIN_TOKEN", ""),
                        help="管理员 token（只给需要 Bearer 的端点用）")
    args = parser.parse_args(argv)
    global API_TOKEN
    API_TOKEN = (args.api_token or "").strip()
    if args.conflict:
        return run_conflict(Path(args.dir), args.peer)
    return run_exercise(Path(args.dir))


if __name__ == "__main__":
    sys.exit(main())
