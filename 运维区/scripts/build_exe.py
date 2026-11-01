#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_exe.py - 把 HUB NEO 网关打成便携 exe（PyInstaller onedir）。

产物布局（整目录拷走即可用）：

    NEO EXE/
      HUBNEO.exe
      启动 HUB NEO.cmd        双击入口（拉起桌面窗口后立即退出，不留黑窗）
      使用说明.md             目录说明与操作/排障手册
      _internal/              Python 运行时 + 编码区/src/ui 静态资源（只读）
      运维区/configs/*.json    可编辑配置（与 exe 并排，升级不覆盖已有文件）
      运维区/data/             运行时数据（账本 / 密钥仓 / 轨迹 / PID / 日志）

纪律：
* `_internal` 里**不允许**出现 `运维区/data` —— 密钥仓、账本、日志一概不进包；
* 配置从源码仓 `运维区/configs` 复制，目标目录里已存在的文件默认不覆盖
  （`--force-configs` 才覆盖），避免升级时冲掉手改配置；
* 入口固定 `运维区/scripts/start_gateway.py`，UI 静态资源按 `--add-data` 带进包。
* 双击入口以 GBK 落盘：cmd.exe 按系统 OEM 代码页读批处理，UTF-8 中文会变乱码。
* 窗口版没有控制台，进程 stdout/stderr 落到 ``运维区/data/logs/desktop.log``；
  启动失败也会弹窗说明原因，不是“双击没反应”。

用法：
    python 运维区/scripts/build_exe.py                     # 默认输出到仓库根 NEO EXE/
    python 运维区/scripts/build_exe.py --out dist           # 换输出目录
    python 运维区/scripts/build_exe.py --no-clean           # 保留旧产物与构建缓存
    python 运维区/scripts/build_exe.py --smoke              # 构建后跑 smoke_exe.py
    python 运维区/scripts/build_exe.py --smoke --archive    # 再归档 zip + 校验和 + 清单
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# 源码收敛在 NEO SRC/ 下，交付件仍落工作区根（与 NEO LITE/ 并列）：
# ROOT = <工作区>/NEO SRC，WORKSPACE = <工作区>。
WORKSPACE = ROOT.parent
SRC_DIR = ROOT / "编码区" / "src"
ENTRY = ROOT / "运维区" / "scripts" / "start_gateway.py"
UI_ASSETS = ROOT / "编码区" / "src" / "ui"
CONFIG_SRC = ROOT / "运维区" / "configs"
DEFAULT_OUT = WORKSPACE / "NEO EXE"
APP_NAME = "HUBNEO"
# UI 在包内的落点：core/paths.py 的 UI_DIR = _MEIPASS/编码区/src/ui
UI_DEST = "编码区/src/ui"
LAUNCHER_NAME = "启动 HUB NEO.cmd"
STOPPER_NAME = "停止 HUB NEO.cmd"
MANUAL_SRC = ROOT / "运维区" / "说明书-便携exe.md"
MANUAL_NAME = "使用说明.md"
# 原生窗口宿主：csc 直编的 WinForms + WebView2 小壳，绕开 pywebview/pythonnet。
SHELL_SRC = ROOT / "编码区" / "shell" / "HUBNEOWindow.cs"
SHELL_NAME = "HUBNEO-Window.exe"
CSC_CANDIDATES = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
]
ARCHIVE_ROOT = ROOT / "仓库区" / "releases"
CHECKSUM_ROOT = ROOT / "仓库区" / "checksums"
SMOKE_SCRIPT = ROOT / "运维区" / "scripts" / "smoke_exe.py"

# 双击入口：cmd.exe 用 GBK 读批处理，所以内容按 GBK 编码落盘。
LAUNCHER_TEXT = """@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0{exe}" (
  echo [错误] 同目录找不到 {exe}，请把整个文件夹一起复制。
  pause
  exit /b 2
)
rem 窗口版：批处理只负责拉起进程，随即退出；关掉应用窗口即停止网关。
start "" "%~dp0{exe}" %*
exit /b 0
"""

STOPPER_TEXT = """@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title HUB NEO 停止
echo ============================================\necho   正在停止 HUB NEO 网关 …
echo ============================================\necho.
if not exist "%~dp0{exe}" (
  echo [错误] 同目录找不到 {exe}，请把整个文件夹一起复制。
  pause
  exit /b 2
)
"%~dp0{exe}" --stop
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="3" (
  echo 网关本来就没在跑。
) else if not "%RC%"=="0" (
  echo 停止失败，退出码 %RC%。
)
pause
exit /b %RC%
"""

# 便携包默认用自己的账本表：与源码版（hub_neo.usage）各记一本账，互不合并。
DEFAULT_LEDGER_TABLE = "usage_portable"


def webview_runtime_dir() -> Path | None:
    """WebView2 原生 loader 所在目录：``webview/lib/runtimes``（不在则返回 None）。"""
    try:
        import importlib.util
        spec = importlib.util.find_spec("webview")
        if not spec or not spec.origin:
            return None
        path = Path(spec.origin).parent / "lib" / "runtimes"
        return path if path.is_dir() else None
    except Exception:                            # noqa: BLE001 没装 pywebview 也不拦构建
        return None


def build_command(out_dir: Path, work_dir: Path) -> list[str]:
    """拼 PyInstaller 命令行（测试直接断言这份参数，不真跑构建）。"""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--distpath", str(out_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
        # 入口用 sys.path.insert 才 import 到 server/core，PyInstaller 分析期不认这种
        # 运行时注入：不把 src 加进 --paths，产物里就没有 server（实测启动即 ModuleNotFoundError）。
        "--paths", str(SRC_DIR),
        "--add-data", f"{UI_ASSETS}{os.pathsep}{UI_DEST}",
        # 账本只在 MySQL 模式才 import pymysql；静态分析能捞到，仍然显式钉住。
        "--hidden-import", "pymysql",
        "--hidden-import", "pymysql.cursors",
    ]
    # 窗口不再走 pywebview：改由 csc 直编的原生壳（HUBNEO-Window.exe）承载，
    # 所以包内无需 webview/pythonnet 的隐藏导入与 DLL 收集。
    cmd.append(str(ENTRY))
    return cmd


def build_window_shell(out_dir: Path) -> str:
    """用 csc 直编 WinForms+WebView2 宿主，并把托管装配与原生 loader 摆到 exe 旁边。

    为什么不用 pywebview：它在 PyInstaller 冻结包里初始化 WebView2 会报
    E_ACCESSDENIED(0x80070005)（源码态正常，最小化冻结应用同样复现）。csc 直编的
    原生壳绕开 pythonnet 整条链路，窗口是自己的，无 Edge 地址栏/标签页/品牌痕迹。
    编译不了不拦打包：运行时检测不到壳就退回 Edge 应用窗口。
    """
    if not SHELL_SRC.is_file():
        return f"跳过：找不到 {SHELL_SRC.name}"
    csc = next((p for p in CSC_CANDIDATES if p.is_file()), None)
    if csc is None:
        return "跳过：找不到 csc.exe（需 .NET Framework 4.x）"
    runtime = webview_runtime_dir()
    lib = runtime.parent if runtime is not None else None
    if lib is None or not (lib / "Microsoft.Web.WebView2.WinForms.dll").is_file():
        return "跳过：找不到 WebView2 托管装配（pip install pywebview 提供）"

    # 在 ASCII 临时目录里编：csc 对含中文的源文件路径会读不到。
    work = Path(tempfile.mkdtemp(prefix="hubneo-shell-"))
    try:
        shutil.copy2(SHELL_SRC, work / "Program.cs")
        done = subprocess.run([
            str(csc), "/nologo", "/target:winexe",
            "/out:HUBNEO-Window.exe",
            "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
            f"/r:{lib / 'Microsoft.Web.WebView2.Core.dll'}",
            f"/r:{lib / 'Microsoft.Web.WebView2.WinForms.dll'}",
            "Program.cs",
        ], cwd=str(work), capture_output=True, text=True, errors="replace")
        if done.returncode != 0:
            return f"跳过：csc 编译失败 {done.stdout.strip()[:200]}"
        shutil.copy2(work / "HUBNEO-Window.exe", out_dir / SHELL_NAME)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copy2(lib / name, out_dir / name)
    loader = runtime / "win-x64" / "native" / "WebView2Loader.dll"
    if loader.is_file():
        shutil.copy2(loader, out_dir / "WebView2Loader.dll")
    return f"已编译 {SHELL_NAME}"


def copy_configs(src: Path = CONFIG_SRC, dst: Path | None = None,
                 overwrite: bool = False) -> tuple[list[str], list[str]]:
    """把 *.json 配置复制到便携目录；返回 (已复制, 已跳过)。

    跳过规则：`*.bak*` 备份、目录；`overwrite=False` 时目标已存在的也跳过。
    """
    dst = Path(dst or (DEFAULT_OUT / "运维区" / "configs"))
    dst.mkdir(parents=True, exist_ok=True)
    copied, skipped = [], []
    for path in sorted(Path(src).glob("*.json")):
        if not path.is_file() or ".bak" in path.name:
            continue
        target = dst / path.name
        if target.exists() and not overwrite:
            skipped.append(path.name)
            continue
        shutil.copy2(path, target)
        copied.append(path.name)
    return copied, skipped


def _write_cmd(directory: Path, name: str, template: str) -> Path:
    """写批处理入口：GBK + CRLF（cmd.exe 按 OEM 代码页读，UTF-8 中文会乱码）。"""
    target = Path(directory) / name
    text = template.format(exe=APP_NAME + ".exe")
    target.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk"))
    return target


def write_launcher(directory: Path) -> Path:
    """写双击启动入口（GBK）；返回落盘路径。"""
    return _write_cmd(directory, LAUNCHER_NAME, LAUNCHER_TEXT)


def write_stopper(directory: Path) -> Path:
    """写双击停止入口：调 `HUBNEO.exe --stop`（只杀监听本端口的网关）。"""
    return _write_cmd(directory, STOPPER_NAME, STOPPER_TEXT)


def isolate_ledger(directory: Path, table: str = DEFAULT_LEDGER_TABLE) -> Path:
    """把便携包的账本表改成独立表，避免与源码版共用一份流水。

    只改交付目录里的 `运维区/configs/upstream.json`，源码仓配置保持 `usage`。
    表不存在时 MySQL 侧会在启动时自动建（`CREATE TABLE IF NOT EXISTS`）。
    """
    path = Path(directory) / "运维区" / "configs" / "upstream.json"
    if not path.is_file():
        raise RuntimeError(f"交付目录缺少 upstream.json: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    ledger = data.get("ledger")
    if not isinstance(ledger, dict):
        raise RuntimeError(f"upstream.json 里没有 ledger 段: {path}")
    ledger["table"] = table
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def copy_manual(directory: Path) -> Path:
    """把说明书放进交付目录（单一源头在仓库 运维区/说明书-便携exe.md）。"""
    if not MANUAL_SRC.is_file():
        raise RuntimeError(f"说明书原件缺失: {MANUAL_SRC}")
    target = Path(directory) / MANUAL_NAME
    shutil.copy2(MANUAL_SRC, target)
    return target


def check_no_secrets(bundle: Path) -> None:
    """打包件（`_internal`）里出现 `运维区/data` 就中止。

    只查包内：交付目录顶层那个 `运维区/data` 是给运行时空的壳，本来就要存在。
    """
    leaked = list(Path(bundle).rglob("运维区/data"))
    if leaked:
        raise RuntimeError(
            "打包目录里出现了 运维区/data（密钥仓/账本不得进包）: "
            + ", ".join(str(p) for p in leaked))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# 运行期残留：这些归 运维区/data，不进归档（归档是「分发态」，不是「运行态」）。
RUNTIME_SUFFIXES = {".pid", ".log", ".db", ".key", ".sqlite", ".jsonl"}

# 运行期缓存：应用窗口的浏览器配置目录会自己长到几百 MB（Edge 组件、字体模型、
# DRM 库），logs/test_runs 同理。它们只是运行痕迹、删掉由运行时空建，
# 留在交付目录里会让文件夹虚胖数倍，所以每次重建都清掉。
RUNTIME_CACHE_DIRS = ("edge-profile", "webview", "logs", "test_runs")


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def purge_runtime_cache(delivery: Path) -> tuple[list[str], list[str], float]:
    """清掉交付目录里的运行期缓存，返回 (已清目录, 没清干净的目录, 释放 MB)。

    只动 ``运维区/data`` 下的缓存目录：配置、账本、密钥仓一概不碰。
    判据看「还剩多少字节」而不是「目录还在不在」：应用正在跑时会立刻把空目录重建，
    那不是没清掉；反过来文件被占用、一个没删成的才算没清干净，报回调用方，不静默略过。
    """
    data_dir = Path(delivery) / "运维区" / "data"
    removed: list[str] = []
    stuck: list[str] = []
    freed = 0
    for name in RUNTIME_CACHE_DIRS:
        target = data_dir / name
        before = _tree_size(target)
        if not target.exists() and not before:
            continue
        shutil.rmtree(target, ignore_errors=True)
        left = _tree_size(target)
        freed += max(before - left, 0)
        (stuck if left else removed).append(name)
    return removed, stuck, freed / (1024 * 1024)


def archive_members(delivery: Path) -> list[Path]:
    """归档成员：交付目录下除 `运维区/data/` 与运行期残留以外的全部文件。"""
    # 不做 resolve()：Windows 临时目录会拿到 8.3 短名（ADMINI~1），
    # 一边解析一边不解析会让 relative_to 报 “not in the subpath”。
    delivery = Path(delivery)
    data_dir = delivery / "运维区" / "data"
    members = []
    for path in sorted(delivery.rglob("*")):
        if not path.is_file():
            continue
        if data_dir in path.parents:
            continue
        if path.suffix.lower() in RUNTIME_SUFFIXES:
            continue
        members.append(path)
    return members


def build_zip(delivery: Path, members: list[Path], target: Path) -> Path:
    """打 zip（解压后得到同名交付目录，不会把壳文件散一地）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    delivery = Path(delivery)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in members:
            archive.write(path, arcname=str(Path(delivery.name) / path.relative_to(delivery)))
    return target


def write_checksums(files: list[Path], target: Path, header: list[str],
                    base: Path | None = None) -> Path:
    """按仓库既有口径写 `sha256  相对路径` 清单。"""
    base = Path(base or ROOT).resolve()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {line}" for line in header]
    for path in files:
        path = Path(path).resolve()
        try:
            label = path.relative_to(base).as_posix()
        except ValueError:
            label = path.as_posix()
        lines.append(f"{sha256_file(path)}  {label}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def archive(delivery: Path, smoke_ok: bool | None = None,
            date: str | None = None,
            archive_root: Path | None = None,
            checksum_root: Path | None = None) -> dict[str, Path]:
    """把交付目录归档成「zip + 校验和 + 清单」，返回各落盘路径。

    ``archive_root`` / ``checksum_root`` 只为测试注入临时目录；生产路径另有默认值。
    """
    delivery = Path(delivery).resolve()
    archive_root = Path(archive_root or ARCHIVE_ROOT)
    checksum_root = Path(checksum_root or CHECKSUM_ROOT)
    date = date or dt.date.today().strftime("%Y%m%d")
    bundle_dir = archive_root / f"NEO_EXE_{date}"
    members = archive_members(delivery)
    total = sum(p.stat().st_size for p in members)

    zip_path = build_zip(delivery, members,
                         bundle_dir / f"HUBNEO_portable_{date}.zip")
    key_files = [zip_path] + [p for p in members
                              if p.parent == delivery
                              and p.suffix.lower() in (".exe", ".md", ".cmd")]
    key_files += sorted((delivery / "运维区" / "configs").glob("*.json"))
    checksums = write_checksums(
        key_files, checksum_root / f"CHECKSUMS_exe_{date}.sha256",
        [f"HUB NEO 便携 exe · 制品校验和（{date}）",
         "算法 sha256；路径相对仓库根"],
        base=ROOT if checksum_root == CHECKSUM_ROOT else archive_root)

    manifest = bundle_dir / "MANIFEST.md"
    smoke_line = {True: "PASS（smoke_exe.py 全项通过）",
                  False: "**FAIL**（冒烟未通过，不得分发）",
                  None: "未跑（构建时未加 --smoke）"}[smoke_ok]
    rows = "\n".join(
        f"| `{p.relative_to(delivery).as_posix()}` | {p.stat().st_size / 1024:.0f} KB |"
        f" {sha256_file(p)[:16]}… |"
        for p in members if p.parent == delivery)
    manifest.write_text(
        f"# HUB NEO 便携 exe 制品清单（{date}）\n\n"
        f"- 构建时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 交付目录：`{delivery}`\n"
        f"- 归档文件：{len(members)} 个 / {total / 1024 / 1024:.1f} MB（未压缩）\n"
        f"- zip：`{zip_path.name}`（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）\n"
        f"- 校验和：`{checksums.name}`\n"
        f"- 冒烟结论：{smoke_line}\n\n"
        "## 归档内容边界\n\n"
        "`运维区/data/`（账本 / 密钥仓 / 客户端 Key / PID / 日志）与运行期残留**不进归档**；\n"
        "解压后首次启动会自建空 `运维区/data/`。\n\n"
        "## 交付目录顶层文件\n\n"
        "| 文件 | 大小 | sha256 前缀 |\n|---|---|---|\n" + rows + "\n\n"
        "## 还原与验证\n\n"
        "```bash\n"
        f"unzip HUBNEO_portable_{date}.zip -d <目标目录>\n"
        "python 运维区/scripts/smoke_exe.py --dir \"<目标目录>/NEO EXE\"\n"
        "```\n",
        encoding="utf-8")
    return {"zip": zip_path, "checksums": checksums, "manifest": manifest,
            "dir": bundle_dir}


def run_smoke(delivery: Path) -> bool:
    """调 smoke_exe.py 跑一遍真机冒烟，返回是否 PASS。"""
    if not SMOKE_SCRIPT.is_file():
        print(f"[build] 冒烟脚本缺失: {SMOKE_SCRIPT}", file=sys.stderr)
        return False
    proc = subprocess.run([sys.executable, str(SMOKE_SCRIPT), "--dir", str(delivery)],
                          cwd=str(ROOT))
    return proc.returncode == 0


def _run(cmd: list[str]) -> None:
    print("[build] " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def _replace_tree(src: Path, dst: Path) -> None:
    """把 PyInstaller 产物搬进交付目录（先清掉同名旧 exe 与 _internal）。"""
    dst.mkdir(parents=True, exist_ok=True)
    for name in (APP_NAME + ".exe", "_internal"):
        old = dst / name
        if old.is_dir():
            shutil.rmtree(old)
        elif old.exists():
            old.unlink()
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HUB NEO 便携 exe 构建")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="交付目录（默认仓库根 NEO EXE/）")
    parser.add_argument("--no-clean", action="store_true",
                        help="保留旧产物与 PyInstaller 缓存")
    parser.add_argument("--force-configs", action="store_true",
                        help="覆盖交付目录里已存在的配置文件")
    parser.add_argument("--smoke", action="store_true",
                        help="构建后跑 smoke_exe.py 真启动冒烟")
    parser.add_argument("--archive", action="store_true",
                        help="归档到 仓库区/releases 并写校验和清单")
    parser.add_argument("--ledger-table", default=DEFAULT_LEDGER_TABLE,
                        help=f"便携包使用的账本表（默认 {DEFAULT_LEDGER_TABLE}，与源码版分表）")
    parser.add_argument("--share-ledger", action="store_true",
                        help="不隔离账本：与源码版共用同一张表")
    args = parser.parse_args(argv)

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("[build] 缺少 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 2
    if not ENTRY.is_file() or not UI_ASSETS.is_dir():
        print(f"[build] 入口或 UI 资源缺失: {ENTRY} / {UI_ASSETS}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    work_dir = ROOT / "build" / "exe"
    stage_dir = work_dir / "dist"
    if not args.no_clean:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(out_dir / "_internal", ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    _run(build_command(stage_dir, work_dir))

    built = stage_dir / APP_NAME
    if not (built / (APP_NAME + ".exe")).is_file():
        print(f"[build] 未找到产物: {built}", file=sys.stderr)
        return 3
    _replace_tree(built, out_dir)
    check_no_secrets(out_dir / "_internal")
    shell_note = build_window_shell(out_dir)

    # 必须显式给 dst：不给就落到 DEFAULT_OUT，`--out` 换目录时配置和产物会分身两处。
    copied, skipped = copy_configs(dst=out_dir / "运维区" / "configs",
                                   overwrite=args.force_configs)
    (out_dir / "运维区" / "data").mkdir(parents=True, exist_ok=True)
    purged, stuck, freed_mb = purge_runtime_cache(out_dir)
    if purged:
        print(f"[build] 已清运行期缓存: {', '.join(purged)}（释放 {freed_mb:.1f} MB）")
    if stuck:
        print(f"[build] 以下缓存被占用没清掉（关掉应用窗口后重跑）: {', '.join(stuck)}",
              file=sys.stderr)
    launcher = write_launcher(out_dir)
    stopper = write_stopper(out_dir)
    manual = copy_manual(out_dir)
    if args.share_ledger:
        ledger_note = "与源码版共用账本表（--share-ledger）"
    else:
        isolate_ledger(out_dir, args.ledger_table)
        ledger_note = f"独立账本表 {args.ledger_table}（与源码版分表）"

    size_mb = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file()) / (1024 * 1024)
    print(f"[build] 产物目录: {out_dir}（{size_mb:.1f} MB）")
    print(f"[build] 配置已复制 {len(copied)} 项 / 保留原文件 {len(skipped)} 项"
          + ("（--force-configs 覆盖）" if args.force_configs else ""))
    print(f"[build] 双击入口: {launcher.name} / {stopper.name}（GBK）；说明书: {manual.name}")
    print(f"[build] 账本: {ledger_note}")
    print(f"[build] 原生窗口宿主: {shell_note}")

    smoke_ok: bool | None = None
    if args.smoke:
        print("[build] 开始冒烟 …", flush=True)
        smoke_ok = run_smoke(out_dir)
        print(f"[build] 冒烟: {'PASS' if smoke_ok else 'FAIL'}")
        if not smoke_ok:
            print("[build] 冒烟未过，停止（加 --archive 也不会归档失败制品）",
                  file=sys.stderr)
            return 4

    if args.archive:
        paths = archive(out_dir, smoke_ok=smoke_ok)
        print(f"[build] 归档 zip: {paths['zip']}")
        print(f"[build] 校验和: {paths['checksums']}")
        print(f"[build] 清单:   {paths['manifest']}")

    print("[build] 启动: 双击 启动 HUB NEO.cmd → 弹应用窗口（关窗即停网关）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
