#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_lite.py —— 生成 KWSA lite（轻量版）的独立交付文件夹 `NEO LITE/`。

产物布局（整目录拷走即可用，不引用主工作区任何文件）：

    NEO LITE/
      启动 NEO lite.cmd        双击入口（默认 8798）
      使用说明.md
      编码区/src/core/          轻量版用到的公共模块（独立副本）
      编码区/src/kwsa_lite/     轻量版自身模块 + web/ 面板
      运维区/configs/*.json     可编辑配置（与重机版各一份）
      运维区/data/              运行时数据（账本 / 密钥仓 / 审计 / 日志）
      运维区/scripts/           start_kwsa_lite.py + lite_secrets.py + TLS 反代

隔离纪律：
* 只读本文件夹内的配置与数据，**不 import 主工作区**（`lite_secrets.py` 顶替了
  原先对 `start_gateway.load_upstream_config` 的依赖）；
* `__pycache__` 不进交付件；配置里只有密钥文件路径，不含明文令牌；
* 与重机版 `NEO EXE/` 并列，各自独立运行（各自端口：轻量版 8798）。

用法：
    python 运维区/scripts/build_lite.py
    python 运维区/scripts/build_lite.py --out "NEO LITE" --force-configs
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 源码在 NEO SRC/ 下，交付件仍落工作区根：ROOT=<工作区>/NEO SRC。
WORKSPACE = ROOT.parent
SRC = ROOT / "编码区" / "src"
SCRIPTS = ROOT / "运维区" / "scripts"
CONFIG_SRC = ROOT / "运维区" / "configs"
DEFAULT_OUT = WORKSPACE / "NEO LITE"
LAUNCHER_NAME = "启动 NEO lite.cmd"
MANUAL_NAME = "使用说明.md"

# 轻量版运行需要的脚本（lite_secrets 顶替主网关的 secrets 注入）
SCRIPT_FILES = ["start_kwsa_lite.py", "kwsa_lite_tls_proxy.py", "lite_secrets.py"]
CONFIG_FILES = ["kwsa_lite.json", "upstream.json", "model_upstreams.json"]

LAUNCHER_TEXT = """@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title NEO lite 8798
echo ============================================
echo   NEO lite · 轻量网关（8798）
echo   \u9762\u677f: http://127.0.0.1:8798/  (\u9996\u6b21\u542f\u52a8\u4f1a\u6253\u5370\u4e00\u6b21\u6027\u5bc6\u7801)
echo   \u5173\u95ed\u672c\u7a97\u53e3\u5373\u505c\u6b62\u8f7b\u91cf\u7248
echo ============================================
echo.
if not exist "%~dp0\u8fd0\u7ef4\u533a\\scripts\\start_kwsa_lite.py" (
  echo [\u9519\u8bef] \u540c\u76ee\u5f55\u627e\u4e0d\u5230 \u8fd0\u7ef4\u533a\\scripts\\start_kwsa_lite.py\uff0c\u8bf7\u628a\u6574\u4e2a\u6587\u4ef6\u5939\u4e00\u8d77\u590d\u5236\u3002
  pause
  exit /b 2
)
python "%~dp0\u8fd0\u7ef4\u533a\\scripts\\start_kwsa_lite.py" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo [NEO lite] \u9000\u51fa\uff0c\u7801 %RC%\u3002\u4e0a\u65b9\u4fe1\u606f\u5c31\u662f\u539f\u56e0\u3002
  pause
)
exit /b %RC%
"""

MANUAL_TEXT = """# NEO lite（轻量版）· 使用说明

> 与重机版 `NEO EXE/` 完全独立的另一个交付件：自己的模块、配置、账本与日志，
> 不引用主工作区任何文件。默认端口 **8798**，可与 8888 的重机版并行运行。

## 一、启动

1. 双击 `启动 NEO lite.cmd`。
2. 控制台会打印面板地址与**一次性登录密码**（首次启动生成，只打印一次）。
3. 浏览器打开 `http://127.0.0.1:8798/`，登录后到「安全」里改密。
4. **关闭控制台窗口即停止轻量版。**

需要本机装有 Python 3.9+（轻量版是源码形态交付；重机版才是打包 exe）。

## 二、目录

```text
NEO LITE/
├── 启动 NEO lite.cmd
├── 使用说明.md
├── 编码区/src/core/          公共模块（独立副本）
├── 编码区/src/kwsa_lite/     轻量版模块 + web/ 面板
├── 运维区/configs/           可编辑配置
└── 运维区/data/              账本 / 密钥仓 / 审计 / 日志（首次启动自建）
```

## 三、与重机版的关系

| 项 | 重机版 `NEO EXE/` | 轻量版 `NEO LITE/` |
|---|---|---|
| 端口 | 8888 | 8798 |
| 形态 | PyInstaller 打包，目标机免装 Python | 源码目录，需要 Python |
| 配置 | `运维区/configs/` | `运维区/configs/`（各自一份） |
| 数据 | `运维区/data/` | `运维区/data/`（各自一份） |

两者互不读取对方文件；同时运行互不影响。

## 四、排障

- `端口 8798 已被占用`：已有一个轻量版在跑，先关掉旧窗口。
- 面板打不开：确认控制台没报错；首次启动的密码只在控制台打印一次，丢了就删除
  `运维区/data/kwsa_lite_auth.json` 后重启重新生成。
"""


def _copy_tree(src: Path, dst: Path) -> int:
    """复制目录，跳过 __pycache__ 与 .pyc；返回复制文件数。"""
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def _write_cmd(path: Path, text: str) -> None:
    """批处理按 GBK 落盘：cmd.exe 用 OEM 代码页读，UTF-8 中文会乱码。"""
    path.write_bytes(text.replace("\n", "\r\n").encode("gbk", errors="replace"))


def build(out_dir: Path, force_configs: bool = False) -> int:
    if not (SRC / "kwsa_lite").is_dir():
        print(f"[lite] 找不到轻量版源码: {SRC / 'kwsa_lite'}", file=sys.stderr)
        return 2

    shutil.rmtree(out_dir, ignore_errors=True)
    (out_dir / "编码区" / "src").mkdir(parents=True)
    (out_dir / "运维区" / "configs").mkdir(parents=True)
    (out_dir / "运维区" / "data").mkdir(parents=True)
    (out_dir / "运维区" / "scripts").mkdir(parents=True)

    files = 0
    for package in ("core", "kwsa_lite"):
        files += _copy_tree(SRC / package, out_dir / "编码区" / "src" / package)

    for name in SCRIPT_FILES:
        shutil.copy2(SCRIPTS / name, out_dir / "运维区" / "scripts" / name)
        files += 1

    # 独立化的关键一步：不再去 import 主工作区的 start_gateway。
    launcher = out_dir / "运维区" / "scripts" / "start_kwsa_lite.py"
    text = launcher.read_text(encoding="utf-8")
    patched = text.replace("from start_gateway import load_upstream_config",
                           "from lite_secrets import load_upstream_config")
    if patched == text:
        print("[lite] 警告：未找到 start_gateway 引用，独立化替换跳过", file=sys.stderr)
    launcher.write_text(patched, encoding="utf-8")

    copied = 0
    for name in CONFIG_FILES:
        src_file = CONFIG_SRC / name
        if not src_file.is_file():
            continue
        dst_file = out_dir / "运维区" / "configs" / name
        if dst_file.exists() and not force_configs:
            continue
        shutil.copy2(src_file, dst_file)
        copied += 1

    _write_cmd(out_dir / LAUNCHER_NAME, LAUNCHER_TEXT)
    (out_dir / MANUAL_NAME).write_text(MANUAL_TEXT, encoding="utf-8")

    size_mb = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file()) / (1024 * 1024)
    print(f"[lite] 产物目录: {out_dir}（{size_mb:.1f} MB，{files} 个模块文件）")
    print(f"[lite] 配置已复制 {copied} 项；数据目录留空，首次启动自建")
    print(f"[lite] 双击入口: {LAUNCHER_NAME}（端口 8798，关窗即停）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 KWSA lite 独立交付文件夹")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 NEO LITE/）")
    parser.add_argument("--force-configs", action="store_true", help="覆盖已存在的配置")
    args = parser.parse_args(argv)
    return build(Path(args.out).resolve(), args.force_configs)


if __name__ == "__main__":
    sys.exit(main())
