#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_adapter.py —— 统一的 SQLite/MySQL 数据库适配器

合并 accounts.py 和 local_ledger.py 中的重复数据库包装逻辑。
提供统一的占位符翻译、事务管理和连接生命周期。

设计原则：
- 零依赖新增（复用已装的 sqlite3 和 pymysql）
- 向后兼容（不破坏现有 accounts 和 ledger 调用）
- 线程安全（每次调用新建连接，用完立即关闭）
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any


def is_mysql_backend(con) -> bool:
    """判断连接是否为 MySQL 后端"""
    # pymysql.connections.Connection 类名包含 "pymysql"
    return "pymysql" in str(type(con)).lower()


class UnifiedConnection:
    """SQLite 与 MySQL 的统一连接包装器

    提供一致的接口：
    - 占位符自动翻译（? → %s for MySQL）
    - executescript 兼容（MySQL 自动拆分多语句）
    - 行格式统一（dict → tuple）
    """

    def __init__(self, con, mysql: bool):
        self._con = con
        self._mysql = mysql

    def executescript(self, sql: str) -> None:
        """执行多语句 SQL（SQLite 原生支持；MySQL 需手动拆分）"""
        if not self._mysql:
            self._con.executescript(sql)
            return
        # pymysql 没有 executescript，需手动拆分并执行
        for stmt in sql.split(";"):
            if stmt.strip():
                self.execute(stmt)

    def execute(self, sql: str, args=()):
        """执行单条 SQL（自动翻译占位符）"""
        if self._mysql:
            cur = self._con.cursor()
            # MySQL 使用 %s 占位符
            cur.execute(sql.replace("?", "%s"), args)
            return cur
        # SQLite 使用 ? 占位符
        return self._con.execute(sql, args)

    def commit(self):
        """提交事务"""
        self._con.commit()

    def rollback(self):
        """回滚事务"""
        self._con.rollback()

    def close(self):
        """关闭连接"""
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            # 关闭失败不应影响业务结果
            pass


@contextlib.contextmanager
def transact(raw_con):
    """统一的事务上下文管理器

    用法示例：
        with transact(ledger.connect()) as con:
            con.execute("INSERT INTO ...", (...))
            con.commit()
        # 自动关闭连接（即使发生异常）

    保证：
    - 异常时自动 rollback
    - 用完必定 close（Windows 上防止文件占用）
    """
    mysql = is_mysql_backend(raw_con)
    con = UnifiedConnection(raw_con, mysql)
    try:
        yield con
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def normalize_rows(rows, mysql: bool) -> list[tuple]:
    """统一行格式：pymysql DictCursor → tuple（与 sqlite3.Row 同形状）"""
    if not mysql:
        return [tuple(r) for r in rows]
    # pymysql 的 DictCursor 返回 dict，需转成 tuple（按 SELECT 列顺序）
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]


def create_sqlite_connection(db_path: str, **kwargs) -> sqlite3.Connection:
    """创建标准的 SQLite 连接（带推荐配置）"""
    return sqlite3.connect(
        db_path,
        check_same_thread=False,  # 多线程安全（每次新连接）
        timeout=30.0,  # 写冲突等待时间
        **kwargs
    )


def create_mysql_connection(
    host: str = "127.0.0.1",
    port: int = 3306,
    user: str = "root",
    password: str = "",
    database: str = "",
    **kwargs
):
    """创建标准的 MySQL 连接（带推荐配置）"""
    import pymysql  # 仅 MySQL 后端需要，延迟导入

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        **kwargs
    )
