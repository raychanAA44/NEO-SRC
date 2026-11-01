# -*- coding: utf-8 -*-
"""paths.py —— 目录口径的唯一出处，源码运行与 PyInstaller 冻结包共用。

两套根：

* ``ASSETS_ROOT``：只读资产根（前端静态资源 ``编码区/src/ui`` 在这里）。
* ``APP_ROOT``：可写数据与可编辑配置根（``运维区/configs``、``运维区/data``）。

源码运行：两者都等于仓库根（``HUB NEO/``）。
冻结运行（``HUBNEO.exe``）：

* ``ASSETS_ROOT`` = ``sys._MEIPASS``，即 ``_internal/``，打包时带入 UI 静态资源；
* ``APP_ROOT``   = exe 所在目录，便携文件夹里的 ``运维区/`` 与 exe 并排。

**为什么不能用 ``Path(__file__).resolve()`` 推根**：PyInstaller 把模块的
``co_filename`` 记成构建时的相对路径（``server.py`` / ``core\\local_ledger.py``），
冻结后 ``resolve()`` 会按**当前工作目录**落点。2026-10-26 首次封装实测：
UI 目录被解析成 ``NEO EXE/ui``（不存在，控制台全站 404），sqlite 账本被写回源码仓。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结包内。"""
    return bool(getattr(sys, "frozen", False))


def assets_root() -> Path:
    """只读资产根：源码=仓库根；冻结=_MEIPASS（`_internal`）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # 理论上走不到；留一条与 onedir 布局一致的兜底，别退回 __file__。
        return Path(sys.executable).resolve().parent / "_internal"
    return Path(__file__).resolve().parents[3]


def app_root() -> Path:
    """可写根：源码=仓库根；冻结=exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


ASSETS_ROOT = assets_root()
APP_ROOT = app_root()
# 可写数据（账本 / 密钥仓 / 轨迹 / PID / 日志）
# ``HUB_DATA_DIR`` 可整体改道：冒烟/回归跑真实 exe 时用临时目录，绝不污染交付件里的账本与密钥仓。
DATA_DIR = Path(os.environ["HUB_DATA_DIR"]).expanduser().resolve() \
    if os.environ.get("HUB_DATA_DIR") else APP_ROOT / "运维区" / "data"
# 可编辑配置（control / pricing / 路由 / 模型分组…）
# ``HUB_CONFIG_DIR`` 同样可改道：功能测验要用一份临时配置跑写接口，不能改交付件里的真配置。
CONFIG_DIR = Path(os.environ["HUB_CONFIG_DIR"]).expanduser().resolve() \
    if os.environ.get("HUB_CONFIG_DIR") else APP_ROOT / "运维区" / "configs"
# 前端实体：只有这一个出口，别在各处再拼 "ui"。
UI_DIR = ASSETS_ROOT / "编码区" / "src" / "ui"


def ensure_runtime_dirs() -> list[Path]:
    """确保可写目录存在；返回本次新建的目录，启动时打一行日志用。"""
    created = []
    for path in (DATA_DIR, CONFIG_DIR):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created
