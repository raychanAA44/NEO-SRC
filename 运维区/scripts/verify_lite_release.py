#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_lite_release.py —— KWSA lite 的发布验收切片：闸门 → 结构 → 真启动 → 打接口 → 收尾。

为什么要有这个脚本：`build_lite.py` 只保证「文件铺出来了」，不保证「铺出来能跑、门禁能过」。
lite 已经是独立交付件（`NEO LITE/`），它进云端前必须能回答四个问题：

    1. 面板还守不守设计红线（字号 / 热区 / 圆角 / 令牌 / 外部资源）
    2. 目录结构是不是自成一体（缺一个 configs 就起不来）
    3. 真起进程之后接口还活着吗（/health、登录页、数据面拒绝）
    4. 面板能不能真的登录进去（这句话只有拿到首启口令才验得了）

用法：

    python 运维区/scripts/verify_lite_release.py                 # 验现有 NEO LITE/ 的一份副本
    python 运维区/scripts/verify_lite_release.py --build         # 先重建到隔离目录再验
    python 运维区/scripts/verify_lite_release.py --dir <别的交付目录>
    python 运维区/scripts/verify_lite_release.py --keep          # 保留临时目录供排查

隔离纪律：一律在系统临时目录里跑副本，`NEO LITE/` 里的账本、面板口令、数据目录一个字节都不碰。
重建模式（`--build`）也只写到临时目录，所以连跑门禁都不会重置交付件的首次口令。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT.parent / "NEO LITE"          # 交付件在工作区根，与源码树并列
BUILD_LITE = ROOT / "运维区" / "scripts" / "build_lite.py"
DESIGN_GATE = ROOT / "编码区" / "scripts" / "verify_lite_design.py"
TLS_PROXY = ROOT / "运维区" / "scripts" / "kwsa_lite_tls_proxy.py"
START_SCRIPT = Path("运维区") / "scripts" / "start_kwsa_lite.py"
WEB_DIR = Path("编码区") / "src" / "kwsa_lite" / "web"

READY_TIMEOUT = 25.0
PASSWORD_MARK = "first-run panel password:"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_listening(port: int, timeout: float = READY_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def wait_released(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.25)
    return False


def http(url: str, method: str = "GET", payload: dict | None = None,
         cookie: str = "", extra: dict | None = None,
         insecure: bool = False) -> tuple[int, str, str]:
    """返回 (状态码, 正文, Set-Cookie)；连接失败按 0 处理，不抛给调用方。

    ``insecure`` 为真时跳过证书校验：演练用的是自签证书，不该为了跑通切片去装证书。
    ``extra`` 用于故意构造伪造头（验反代会不会覆盖它们）。
    """
    data = None
    headers = dict(extra or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body, response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Set-Cookie", "")
    except Exception:
        return 0, "", ""


# 上云清单的“安全头齐备”不能只靠眼看：这里逐项断言存在与关键值。
SECURITY_HEADERS = ("strict-transport-security", "x-frame-options",
                    "x-content-type-options", "referrer-policy", "content-security-policy")
CSP_EXPECT = ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'")


def http_header_map(url: str, insecure: bool = False) -> dict[str, str]:
    """拿响应头（键统一小写）；拿不到就返回空字典。"""
    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=10, context=context) as response:
            return {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        return {}


def security_header_gaps(headers: dict[str, str]) -> list[str]:
    """返回不符合预期的安全头描述；空列表表示全部达标。"""
    gaps = [name for name in SECURITY_HEADERS if name not in headers]
    if headers.get("x-frame-options", "").strip().upper() != "DENY":
        gaps.append("x-frame-options 不是 DENY")
    if headers.get("x-content-type-options", "").strip().lower() != "nosniff":
        gaps.append("x-content-type-options 不是 nosniff")
    csp = headers.get("content-security-policy", "")
    gaps += [f"CSP 缺 {piece}" for piece in CSP_EXPECT if piece not in csp]
    if "max-age=" not in headers.get("strict-transport-security", ""):
        gaps.append("HSTS 缺 max-age")
    return gaps


def make_cert(target: Path) -> tuple[Path, Path] | None:
    """生成一天期自签证书（仅本地演练用）。没有 openssl 就返回 None。"""
    cert, key = target / "cert.pem", target / "key.pem"
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", str(key), "-out", str(cert), "-days", "1",
           "-subj", "/CN=localhost",
           "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return None
    return (cert, key) if done.returncode == 0 and cert.is_file() else None


def stage_copy(source: Path, staging: Path) -> None:
    """把交付件复制成临时副本；数据目录不复制（要一份干净的首次启动）。"""
    shutil.copytree(source, staging, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("data", "*.pyc", "__pycache__"))


class Checker:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append((name, ok, detail))
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}: {detail}")
        return ok

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.items if ok)

    @property
    def total(self) -> int:
        return len(self.items)


def check_structure(root: Path, check: Checker) -> bool:
    required = [
        START_SCRIPT,
        WEB_DIR / "index.html",
        WEB_DIR / "login.html",
        Path("运维区") / "configs" / "kwsa_lite.json",
        Path("启动 NEO lite.cmd"),
    ]
    missing = [str(p) for p in required if not (root / p).is_file()]
    return check.record("目录结构自持", not missing,
                        "5 项关键文件齐备" if not missing else "缺: " + ", ".join(missing))


def _utf8_env() -> dict:
    """子进程统一吐 UTF-8：Windows 默认用 GBK 管道，中文字会解成乱码。"""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run_design_gate(root: Path, check: Checker) -> bool:
    proc = subprocess.run([sys.executable, str(DESIGN_GATE), "--dir", str(root)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=_utf8_env())
    detail = (proc.stdout or proc.stderr).strip().splitlines()
    return check.record("设计红线闸门", proc.returncode == 0,
                        detail[-1] if detail else f"exit={proc.returncode}")


def drain(stream, sink: list[str]) -> None:
    try:
        for line in stream:
            sink.append(line.rstrip())
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KWSA lite 发布验收切片")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="交付目录（默认 ../NEO LITE）")
    parser.add_argument("--build", action="store_true",
                        help="先跑 build_lite.py 构建到临时目录再验（不碰交付件）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录供排查")
    args = parser.parse_args(argv)
    # 控制台可能是 GBK：宁可显示成 ?，也不要因为一个替换字符就崩在这里。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    source = Path(args.dir).resolve()
    if not args.build and not source.is_dir():
        print(f"[lite] 交付目录不存在: {source}（先跑 build_lite.py 或用 --build）",
              file=sys.stderr)
        return 2

    staging_root = Path(tempfile.mkdtemp(prefix="neo-lite-verify-"))
    staging = staging_root / "NEO LITE"
    check = Checker()
    proc = None
    print(f"[lite] 验收目录: {source if not args.build else '（构建产物）'}")
    print(f"[lite] 隔离副本: {staging}")

    try:
        if args.build:
            build = subprocess.run([sys.executable, str(BUILD_LITE), "--out", str(staging)],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", env=_utf8_env())
            tail = (build.stdout or "").strip().splitlines()
            if not check.record("构建交付件", build.returncode == 0,
                                tail[-1] if tail else f"exit={build.returncode}"):
                print("[lite] 结论: FAIL（构建未过，不再启动）")
                return 1
        else:
            stage_copy(source, staging)

        # 干净首启：绝不带着旧口令/旧账本进验收
        stale = staging / "运维区" / "data"
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

        if not check_structure(staging, check):
            print("[lite] 结论: FAIL（结构不完整，不再启动）")
            return 1
        if not run_design_gate(staging, check):
            print("[lite] 结论: FAIL（设计闸门未过，不再启动）")
            return 1

        port = free_port()
        log: list[str] = []
        proc = subprocess.Popen(
            [sys.executable, str(START_SCRIPT), "--port", str(port), "--host", "127.0.0.1"],
            cwd=str(staging), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=drain, args=(proc.stdout, log), daemon=True).start()

        ready = wait_listening(port)
        if not check.record("进程真启动", ready, f"监听 127.0.0.1:{port}" if ready else "超时未监听"):
            print("[lite] 结论: FAIL（起不来，跳过接口检查）")
            return 1

        base = f"http://127.0.0.1:{port}"
        status, body, _ = http(f"{base}/health")
        healthy = status == 200
        if healthy:
            try:
                healthy = bool(json.loads(body))
            except Exception:
                healthy = False
        check.record("健康端点 /health", healthy, f"HTTP {status}")

        status, body, _ = http(f"{base}/login.html")
        check.record("登录页可访问", status == 200 and "面板登录" in body, f"HTTP {status}")

        status, body, _ = http(f"{base}/")
        unauth_ok = status == 200 and "面板登录" in body and "网关状态台" not in body
        check.record("未登录不泄露面板", unauth_ok, f"HTTP {status} · 落在登录页")

        status, _, _ = http(f"{base}/v1/models")
        check.record("数据面无 Key 拒绝", status in (401, 403, 503), f"HTTP {status}")

        gaps = security_header_gaps(http_header_map(f"{base}/health"))
        check.record("安全响应头齐全", not gaps,
                     "HSTS / XFO / nosniff / Referrer / CSP 全达标" if not gaps
                     else "; ".join(gaps))

        password = ""
        for line in log:
            if PASSWORD_MARK in line:
                password = line.split(PASSWORD_MARK, 1)[1].strip()
        direct_cookie = ""
        if password:
            status, _, set_cookie = http(f"{base}/api/login", "POST",
                                         {"username": "admin", "password": password})
            direct_cookie = set_cookie
            cookie = set_cookie.split(";", 1)[0].strip()
            check.record("面板登录", status == 200 and "kwsa_lite_session" in cookie,
                         f"HTTP {status} · 已种会话")
            if cookie:
                status, body, _ = http(f"{base}/", cookie=cookie)
                check.record("登录后读面板", status == 200 and "网关状态台" in body,
                             f"HTTP {status} · 控制台")
        else:
            check.record("面板登录", False, "未拿到首启口令（日志里没有 " + PASSWORD_MARK + "）")

        # ── 反代场景 ───────────────────────────────────────────────
        # 上云清单里“Secure 靠 X-Forwarded-Proto 触发”这句必须真测，否则只是读代码的推论。
        # 基线：直连 HTTP（无 XFP）不得加 Secure，否则后面就分不清是谁加的。
        check.record("直连不加 Secure（基线）", bool(direct_cookie) and "Secure" not in direct_cookie.lower(),
                     "Set-Cookie 无 Secure" if direct_cookie else "未拿到 Set-Cookie")

        cert_pair = make_cert(staging_root) if password else None
        if cert_pair is None:
            check.record("反代 Secure Cookie", False,
                         "未能生成自签证书（需 openssl），无法验证反代链路")
        else:
            proxy_port = free_port()
            tls_log: list[str] = []
            proxy = subprocess.Popen(
                [sys.executable, str(TLS_PROXY), "--listen-host", "127.0.0.1",
                 "--listen-port", str(proxy_port), "--upstream-host", "127.0.0.1",
                 "--upstream-port", str(port), "--cert", str(cert_pair[0]),
                 "--key", str(cert_pair[1])],
                cwd=str(staging), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            threading.Thread(target=drain, args=(proxy.stdout, tls_log), daemon=True).start()
            https = f"https://127.0.0.1:{proxy_port}"
            try:
                up = wait_listening(proxy_port)
                check.record("TLS 反代启动", up, f"监听 127.0.0.1:{proxy_port}" if up else "超时未监听")
                if up:
                    status, _, _ = http(f"{https}/health", insecure=True)
                    check.record("经反代 /health", status == 200, f"HTTPS {status}")

                    # 反代很容易把上游响应头吃掉一半，这里紧跟一道：透传后仍须齐全。
                    proxy_gaps = security_header_gaps(
                        http_header_map(f"{https}/health", insecure=True))
                    check.record("反代透传安全头", not proxy_gaps,
                                 "HSTS / XFO / nosniff / Referrer / CSP 经反代仍全"
                                 if not proxy_gaps else "; ".join(proxy_gaps))

                    status, _, proxied = http(
                        f"{https}/api/login", "POST",
                        {"username": "admin", "password": password}, insecure=True)
                    secure_ok = status == 200 and "secure" in proxied.lower()
                    check.record("反代登录加 Secure", secure_ok,
                                 f"HTTPS {status} · "
                                 + ("Set-Cookie 含 Secure" if secure_ok else "缺 Secure（反代未传 XFP）"))

                    # 客户端伪造 X-Forwarded-Proto: http，反代必须覆盖成 https，仍应加 Secure。
                    status, _, spoofed = http(
                        f"{https}/api/login", "POST",
                        {"username": "admin", "password": password}, insecure=True,
                        extra={"X-Forwarded-Proto": "http", "X-Forwarded-For": "9.9.9.9"})
                    spoof_ok = status == 200 and "secure" in spoofed.lower()
                    check.record("伪造转发头被覆盖", spoof_ok,
                                 "仍含 Secure（反代已接管 X-Forwarded-*）" if spoof_ok
                                 else "伪造头生效了：反代没覆盖 X-Forwarded-*")
            finally:
                if proxy.poll() is None:
                    proxy.terminate()
                    try:
                        proxy.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proxy.kill()

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc is not None:
            released = wait_released(port) if "port" in locals() else True
            check.record("优雅退出", released, "端口已释放" if released else "端口仍被占用")
        if args.keep:
            print(f"[lite] 临时目录保留: {staging_root}")
        else:
            shutil.rmtree(staging_root, ignore_errors=True)

    ok = check.total == check.passed and check.total > 0
    print(f"[lite] 结论: {'PASS' if ok else 'FAIL'} · {check.passed}/{check.total} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
