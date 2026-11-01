#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config_io.py —— 统一的配置文件 JSON I/O 工具

消除全站 15+ 处重复的 JSON 读写模式，提供：
- 安全读取（错误时返回默认值，不崩溃）
- 原子写入（临时文件 + os.replace，防止损坏）
- 自动备份（写入前保存 .bak）

设计原则：
- 零新增依赖（只用标准库）
- 向后兼容（返回值与现有代码一致）
- 错误透明（读失败返回默认值，写失败抛异常）
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def load_json_safe(path: Path | str, default: Any = None) -> dict:
    """安全读取 JSON 文件（错误时返回默认值）

    Args:
        path: JSON 文件路径
        default: 读取失败时的默认返回值（默认 {}）

    Returns:
        解析后的 dict，或失败时的 default

    示例：
        config = load_json_safe("config.json", default={"port": 8888})
    """
    if default is None:
        default = {}

    path = Path(path)
    if not path.exists():
        return default

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError, UnicodeDecodeError):
        # OSError: 文件读取失败
        # ValueError: JSON 格式错误
        # UnicodeDecodeError: 编码问题
        return default


def save_json_atomic(
    path: Path | str,
    data: dict,
    *,
    backup: bool = True,
    indent: int = 2,
    ensure_ascii: bool = False
) -> None:
    """原子写入 JSON 文件（防止写入一半时程序崩溃导致文件损坏）

    原理：
    1. 先写到临时文件 .tmp
    2. 成功后用 os.replace 原子替换（操作系统保证）
    3. 可选：写入前备份旧文件为 .bak

    Args:
        path: 目标文件路径
        data: 要保存的字典数据
        backup: 是否在写入前备份旧文件（默认 True）
        indent: JSON 缩进空格数（默认 2）
        ensure_ascii: 是否转义非 ASCII 字符（默认 False，保留中文）

    Raises:
        OSError: 写入失败时抛出

    示例：
        save_json_atomic("config.json", {"port": 8888, "host": "0.0.0.0"})
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 可选：备份旧文件
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            # 备份失败不应阻止写入
            pass

    # 写入临时文件
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent) + "\n"
        tmp_path.write_text(text, encoding="utf-8")
        # 原子替换（操作系统保证不会出现"写了一半"的文件）
        tmp_path.replace(path)
    except Exception:
        # 清理临时文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def load_json_or_fail(path: Path | str) -> dict:
    """严格模式读取 JSON（错误时抛异常而非返回默认值）

    用于必须存在且格式正确的配置文件。

    Args:
        path: JSON 文件路径

    Returns:
        解析后的 dict

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: JSON 格式错误

    示例：
        try:
            config = load_json_or_fail("必备配置.json")
        except FileNotFoundError:
            print("配置文件缺失，无法启动")
            sys.exit(1)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"配置文件格式错误: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是对象，实际是 {type(data).__name__}: {path}")

    return data


def update_json_field(path: Path | str, updates: dict) -> None:
    """增量更新 JSON 文件中的字段（保留其他字段）

    用于修改配置文件的部分字段，不影响其他配置。

    Args:
        path: JSON 文件路径
        updates: 要更新的字段（浅合并）

    示例：
        # 只修改 port，保留其他配置
        update_json_field("config.json", {"port": 9999})
    """
    path = Path(path)
    current = load_json_safe(path, default={})
    current.update(updates)
    save_json_atomic(path, current)
