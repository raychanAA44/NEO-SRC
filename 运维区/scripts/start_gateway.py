#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_gateway.py - HUB NEO 网关一键启动脚本 (监听 8888 端口)

用法：
    python 运维区/scripts/start_gateway.py
或传入管理员 Token：
    HUB_ADMIN_TOKEN='...' python 运维区/scripts/start_gateway.py

冻结包（HUBNEO.exe）里配置与数据都在 exe 同级的 `运维区/`，不读 `_internal/`：
`_internal` 只放运行时与 UI 静态资源，换版本整包替换也不会带走数据。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):
    # 便携文件夹：exe 旁边的 运维区/configs 才是可编辑配置
    ROOT_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent.parent
    BUNDLE_DIR = ROOT_DIR
# 配置目录默认与 exe 并排；``HUB_CONFIG_DIR`` 可整体改道（功能测验拿临时副本跑写接口）。
CONFIG_DIR = Path(os.environ["HUB_CONFIG_DIR"]).expanduser().resolve() \
    if os.environ.get("HUB_CONFIG_DIR") else ROOT_DIR / "运维区" / "configs"
SRC_DIR = BUNDLE_DIR / "编码区" / "src"
UPSTREAM_CONFIG = CONFIG_DIR / "upstream.json"
MODEL_UPSTREAMS_CONFIG = Path(
    os.environ.get("HUB_MODEL_UPSTREAMS") or (CONFIG_DIR / "model_upstreams.json")
)


def load_upstream_config(path=UPSTREAM_CONFIG):
    """从外部配置加载上游与 token；环境变量优先，缺失时保持默认。

    token 不落仓库：配置文件只写 token 文件路径与字段名。
    """
    path = Path(path)
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[HUB NEO] 上游配置读取失败: {exc}", file=sys.stderr)
        return

    base_url = str(config.get("base_url") or "").strip()
    if base_url and not os.environ.get("HUB_UPSTREAM"):
        os.environ["HUB_UPSTREAM"] = base_url
    if base_url and not os.environ.get("HUB_UPSTREAM_CLOUD"):
        os.environ["HUB_UPSTREAM_CLOUD"] = base_url

    fallback_url = str(config.get("fallback_url") or "").strip()
    if fallback_url and not os.environ.get("HUB_UPSTREAM_LOCAL"):
        os.environ["HUB_UPSTREAM_LOCAL"] = fallback_url

    # 推理链路与账本是本地自持的：模型上游、渠道名、账本模式各自一个 env。
    for cfg_key, env_key in (("model_upstream", "HUB_MODEL_UPSTREAM"),
                             ("model_channel", "HUB_MODEL_CHANNEL"),
                             ("ledger_mode", "HUB_LEDGER_MODE")):
        value = str(config.get(cfg_key) or "").strip()
        if value and not os.environ.get(env_key):
            os.environ[env_key] = value

    for key in ("probe_interval_s", "probe_timeout_s"):
        value = config.get(key)
        env_key = "HUB_UPSTREAM_" + key.replace("_s", "").upper()
        if value is not None and not os.environ.get(env_key):
            os.environ[env_key] = str(value)

    # R-1/NEO-GW-05:探针路径可配置(第三方上游可能没有 /health)
    probe_path = str(config.get("probe_path") or "").strip()
    if probe_path and not os.environ.get("HUB_UPSTREAM_PROBE_PATH"):
        os.environ["HUB_UPSTREAM_PROBE_PATH"] = probe_path

    # NEO-GW-04/06: 提示词自愈模式 (off/shadow/enforce)
    prompt_opt = str(config.get("prompt_opt") or "").strip()
    if prompt_opt and not os.environ.get("HUB_PROMPT_OPT"):
        os.environ["HUB_PROMPT_OPT"] = prompt_opt

    # 账本后端：MySQL 连接的密码仍从 secrets 文件的键读，配置文件里只写键名。
    ledger = config.get("ledger") or {}
    for cfg_key, env_key in (("backend", "HUB_LEDGER_BACKEND"),
                             ("host", "HUB_LEDGER_MYSQL_HOST"),
                             ("port", "HUB_LEDGER_MYSQL_PORT"),
                             ("user", "HUB_LEDGER_MYSQL_USER"),
                             ("database", "HUB_LEDGER_MYSQL_DB"),
                             ("table", "HUB_LEDGER_MYSQL_TABLE")):
        value = ledger.get(cfg_key)
        if value not in (None, "") and not os.environ.get(env_key):
            os.environ[env_key] = str(value)

    source = config.get("token_source") or {}
    token_file = str(source.get("file") or "").strip()
    token_key = str(source.get("key") or "").strip()
    fallback_key = str(source.get("fallback_key") or "").strip()
    model_key = str(source.get("model_key") or "").strip()
    if not token_file or not token_key:
        return

    token_path = Path(os.path.expandvars(os.path.expanduser(token_file)))
    if not token_path.is_absolute():
        token_path = ROOT_DIR / token_path
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[HUB NEO] token 源不可用: {exc}", file=sys.stderr)
        return
    if not os.environ.get("HUB_ADMIN_TOKEN"):
        token = str(data.get(token_key) or "").strip()
        if token:
            os.environ["HUB_ADMIN_TOKEN"] = token
    if fallback_key and not os.environ.get("HUB_LOCAL_ADMIN_TOKEN"):
        local_token = str(data.get(fallback_key) or "").strip()
        if local_token:
            os.environ["HUB_LOCAL_ADMIN_TOKEN"] = local_token
    if model_key and not os.environ.get("HUB_MODEL_TOKEN"):
        model_token = str(data.get(model_key) or "").strip()
        if model_token:
            os.environ["HUB_MODEL_TOKEN"] = model_token
    # 额外模型上游（如 Ollama clef:27b）也只从密钥库取 token：
    # 配置文件里写 token_env（进程环境变量名）与 token_key（密钥库键名）。
    try:
        routes = json.loads(MODEL_UPSTREAMS_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        routes = {}
    for name, entry in ((routes.get("channels") or {}) if isinstance(routes, dict) else {}).items():
        if not isinstance(entry, dict):
            continue
        env_key = str(entry.get("token_env") or "").strip()
        key_name = str(entry.get("token_key") or "").strip()
        if not env_key or not key_name or os.environ.get(env_key):
            continue
        value = str(data.get(key_name) or "").strip()
        if value:
            os.environ[env_key] = value
    ledger_pw_key = str(ledger.get("password_key") or "").strip()
    if ledger_pw_key and not os.environ.get("HUB_LEDGER_MYSQL_PASSWORD"):
        ledger_pw = str(data.get(ledger_pw_key) or "").strip()
        if ledger_pw:
            os.environ["HUB_LEDGER_MYSQL_PASSWORD"] = ledger_pw


def _is_mysql_connect_error(exc: BaseException) -> bool:
    """server 导入期就会建账本连接；只对 pymysql 的连接类错误给可读提示。"""
    return (type(exc).__name__ == "OperationalError"
            and type(exc).__module__.startswith("pymysql"))


def _mysql_hint() -> str:
    host = os.environ.get("HUB_LEDGER_MYSQL_HOST") or "127.0.0.1"
    port = os.environ.get("HUB_LEDGER_MYSQL_PORT") or "3306"
    database = os.environ.get("HUB_LEDGER_MYSQL_DB") or "hub_neo"
    return (f"MySQL 不可达（{host}:{port} / 库 {database}）。\n"
            "  先启动 MySQL 再重试；或临时改用 sqlite 验证：\n"
            "    set HUB_LEDGER_BACKEND=sqlite   （PowerShell: $env:HUB_LEDGER_BACKEND='sqlite'）")


def _listener_pid(port: int) -> int | None:
    """谁在监听这个端口（只用系统自带 netstat，冻结包不能依赖 psutil）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15, errors="replace").stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in line:
            continue
        if parts[1].endswith(":%d" % port):
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def _process_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             errors="replace").stdout
    except Exception:
        return ""
    first = out.strip().splitlines()[0] if out.strip() else ""
    return first.split(",")[0].strip('"') if first else ""


def stop_running(port: int, timeout: float = 8.0) -> int:
    """停掉正在监听该端口的网关（双击入口里的「停止」走这里）。

    只对 HUBNEO.exe / python 进程下手，别把占着端口的其它程序当网关杀了。
    直接 `/F`：控制台进程收不到 `taskkill`（不带 /F）的 WM_CLOSE，先试只会白等；
    代价是正在跑的请求会被截断——网关无持久化中间态，重启即恢复。
    返回 0 = 已停 / 3 = 本来就没跑 / 4 = 端口被别的程序占用 / 5 = 停不下来。
    """
    pid = _listener_pid(port)
    if pid is None:
        print(f"[HUB NEO] 端口 {port} 上没有网关在跑，无需停止。")
        return 3
    name = _process_name(pid)
    if not (name.lower() == "hubneo.exe" or name.lower().startswith("python")):
        print(f"[HUB NEO] 端口 {port} 被 {name or '未知程序'}（PID {pid}）占用，"
              "不是网关，已放弃停止。", file=sys.stderr)
        return 4
    print(f"[HUB NEO] 正在停止 {name}（PID {pid}，端口 {port}）…")
    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                   capture_output=True, text=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _listener_pid(port) is None:
            print("[HUB NEO] 已停止，端口已释放。")
            return 0
        time.sleep(0.3)
    print(f"[HUB NEO] 停不下来（PID {pid} 仍在监听 {port}）。", file=sys.stderr)
    return 5


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def open_browser_when_ready(port: int, timeout: float = 30.0) -> bool:
    """等端口真能连上再拉浏览器；双击 exe 时省掉“刷新一下才出来”的一步。

    返回是否成功打开。只在 ``HUB_OPEN_BROWSER`` 未关闭且为冻结包时被调用。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return webbrowser.open(f"http://127.0.0.1:{port}/")
        time.sleep(0.3)
    return False


def _writable_base() -> Path:
    """可写根：``HUB_DATA_DIR`` 改道优先，否则用应用自己的 运维区/data。"""
    raw = os.environ.get("HUB_DATA_DIR")
    return Path(raw).expanduser() if raw else ROOT_DIR / "运维区" / "data"


def _redirect_stdio_for_windowed() -> None:
    """--windowed 打包没有控制台：stdout/stderr 是 None，任何 print 都会炸。

    统一落到数据目录下的日志文件，顺带保住排障线索。源码态（有控制台）不动。
    ``HUB_DATA_DIR`` 改道时日志跟着走，冒烟/功能测验不会往交付件里写东西。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = _writable_base() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "desktop.log", "a", encoding="utf-8",
                      errors="replace", buffering=1)
    except Exception:                            # 日志开不出来也不能拦住启动
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _show_startup_error(message: str) -> None:
    """启动失败时弹系统消息框——没有控制台，不能只写日志了事。"""
    print(f"[HUB NEO] 启动失败: {message}", file=sys.stderr, flush=True)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "HUB NEO 启动失败", 0x10)
    except Exception as exc:                     # noqa: BLE001
        print(f"[HUB NEO] 错误提示也弹不出来: {exc}", file=sys.stderr, flush=True)


# 原生窗口宿主（构建时由 csc 直编，与 HUBNEO.exe 并排）。
SHELL_NAME = "HUBNEO-Window.exe"


def _edge_executable():
    """定位 Edge 主程序；Win10/11 标配，找不到返回 None。"""
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        path = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if path.is_file():
            return path
    return None


def _run_app_window(port: int) -> None:
    """Edge 应用窗口：无地址栏/无标签页，等到窗口关闭才返回。

    必须带自己的 ``--user-data-dir``：否则进程会把请求交给已在跑的 Edge 后立刻退出，
    窗口还在而我们已经把网关关了。独立数据目录同时守住了“不写到文件夹外”。
    """
    edge = _edge_executable()
    if edge is None:
        print("[HUB NEO] 找不到 Edge，无法打开应用窗口", file=sys.stderr, flush=True)
        return
    profile = _writable_base() / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    print(f"[HUB NEO] 应用窗口: {edge} (数据目录 {profile})", flush=True)
    proc = subprocess.Popen([
        str(edge), f"--user-data-dir={profile}",
        f"--app=http://127.0.0.1:{port}/", "--window-size=1600,1000",
        "--no-first-run", "--no-default-browser-check",
    ])
    proc.wait()


def _run_desktop(port: int):
    """原生窗口模式：服务跑后台线程，自编的 WebView2 壳承载前端，关窗即停服。

    壳是 csc 直编的 ``HUBNEO-Window.exe``（源码见 编码区/shell/HUBNEOWindow.cs）：
    它初始化 WebView2 失败时以退出码 3 退出且**不显示窗口**，这里据此静默改用
    Edge 应用窗口，不会留下白屏。监听放 daemon 线程，关窗后调 ``server.shutdown()``
    让服务自己收尾。
    """
    from server import run as serve, GatewayAlreadyRunningError

    ready = threading.Event()
    holder = {}

    def _mark_ready(srv):
        holder["server"] = srv
        ready.set()

    def _worker():
        try:
            serve(port, on_ready=_mark_ready)
        except GatewayAlreadyRunningError as exc:
            holder["error"] = str(exc)
        except Exception as exc:                 # noqa: BLE001 启动失败要能看到原因
            holder["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            ready.set()

    threading.Thread(target=_worker, name="gateway", daemon=True).start()

    if not ready.wait(timeout=60):
        _show_startup_error(f"网关 60 秒内未就绪（端口 {port}）。")
        return 1
    if "server" not in holder:
        _show_startup_error(holder.get("error") or f"端口 {port} 启动失败。")
        return 1

    shell = Path(sys.executable).resolve().parent / SHELL_NAME
    code = None
    if shell.is_file():
        # WebView2 用户数据目录显式钉在应用自己的 data/webview，不外写。
        profile = _writable_base() / "webview"
        profile.mkdir(parents=True, exist_ok=True)
        print(f"[HUB NEO] 窗口: {shell.name}（数据目录 {profile}）", flush=True)
        code = subprocess.Popen([
            str(shell), "--port", str(port), "--profile", str(profile),
        ]).wait()
    else:
        print("[HUB NEO] 找不到原生窗口宿主，改用 Edge 应用窗口",
              file=sys.stderr, flush=True)

    if code != 0:
        print(f"[HUB NEO] 原生窗口未启用（退出码 {code}），改用 Edge 应用窗口",
              flush=True)
        _run_app_window(port)

    holder["server"].shutdown()
    print(f"[HUB NEO] 窗口已关闭，网关停止（端口 {port}）", flush=True)
    return 0


def main():
    _redirect_stdio_for_windowed()
    if "--stop" in sys.argv[1:]:
        port = int(os.environ.get("HUB_NEO_PORT") or 8888)
        sys.exit(stop_running(port))

    # 必须在 import server 之前加载：server.TOKEN / server.UPSTREAM 在模块导入时读取。
    load_upstream_config()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    try:
        from core.paths import ensure_runtime_dirs, DATA_DIR, CONFIG_DIR
        for created in ensure_runtime_dirs():
            print(f"[HUB NEO] 已创建运行目录: {created}")
    except Exception as exc:                     # 目录建不出来就让后面的报错自己说
        DATA_DIR = CONFIG_DIR = None
        print(f"[HUB NEO] 运行目录准备失败: {exc}", file=sys.stderr)

    try:
        from server import run, PORT, GatewayAlreadyRunningError
    except Exception as exc:
        if _is_mysql_connect_error(exc):
            print(f"[HUB NEO] 启动中止：账本后端{_mysql_hint()}", file=sys.stderr, flush=True)
            sys.exit(2)
        raise

    # 便携桌面包：原生窗口承载前端，关窗即停服；没有窗口能力时退回浏览器。
    # HUB_OPEN_BROWSER=0 表示不弹界面（冒烟 / 功能测验走这条），只起控制台服务。
    if getattr(sys, "frozen", False) and _env_flag("HUB_OPEN_BROWSER", True):
        code = _run_desktop(PORT)
        if code is not None:
            sys.exit(code)

    print(f"=== 正在启动 HUB NEO 网关系统 (Port: {PORT}) ===")
    print(f"工作区根目录: {ROOT_DIR}")
    print(f"控制台地址:   http://127.0.0.1:{PORT}/   （关闭本窗口即停止网关）")
    if DATA_DIR:
        print(f"数据目录:     {DATA_DIR}")
        print(f"配置目录:     {CONFIG_DIR}")
    try:
        run(PORT)
    except GatewayAlreadyRunningError as exc:
        print(f"[HUB NEO] 启动中止: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
