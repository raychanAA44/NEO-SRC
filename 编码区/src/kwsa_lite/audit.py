# -*- coding: utf-8 -*-
"""KWSA lite 安全审计（纯标准库）。

只记录面板与数据面凭据的安全事件，不记录口令、会话 sid、API Key 明文：
- 登录成功 / 失败 / 熔断、退出、改密成功 / 失败；
- 数据面 Key 的签发 / 停用 / 吊销。

落盘为追加式 JSONL，单文件超过阈值时滚动为 ``.1``；读取只取文件尾部，
不会把大日志全量倒灌进内存。审计写入失败不阻断登录，只在 stderr 留痕。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from core import clock

__all__ = ["AuditError", "AuditLog", "AUDIT_EVENTS"]

AUDIT_EVENTS = (
    "login_success",
    "login_failed",
    "login_throttled",
    "logout",
    "password_changed",
    "password_change_failed",
    "client_key_created",
    "client_key_toggled",
    "client_key_revoked",
)
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


class AuditError(RuntimeError):
    """审计文件不可读；调用方必须显式呈现，不能当作空审计。"""


def _clean(value, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


class AuditLog:
    def __init__(self, path, max_bytes: int = 2 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max(64 * 1024, int(max_bytes or 2 * 1024 * 1024))
        self._lock = threading.RLock()

    def _rotate_locked(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        backup = self.path.with_name(self.path.name + ".1")
        try:
            os.replace(self.path, backup)
        except OSError as exc:
            sys.stderr.write("[KWSA lite] audit rotate failed: %s\n" % exc)
            sys.stderr.flush()

    def record(self, event: str, *, username: str = "", ip: str = "",
               peer_ip: str = "", detail: str = "", user_agent: str = "") -> bool:
        name = _clean(event, 48).lower()
        if not _EVENT_RE.match(name):
            return False
        row = {
            "ts": int(time.time()),
            "iso": clock.iso_t(),
            "event": name,
            "user": _clean(username, 64),
            "ip": _clean(ip, 64),
            "peer_ip": _clean(peer_ip, 64),
            "detail": _clean(detail, 240),
            "ua": _clean(user_agent, 240),
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_locked()
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            return True
        except OSError as exc:
            # 审计失败不拦登录；但必须在服务端留下痕迹，不能静默。
            sys.stderr.write("[KWSA lite] audit write failed: %s\n" % exc)
            sys.stderr.flush()
            return False

    def tail(self, limit: int = 100, event: str = "") -> list:
        wanted = _clean(event, 48).lower()
        if wanted and wanted not in AUDIT_EVENTS:
            return []
        try:
            limit = max(1, min(int(limit or 100), 200))
        except (TypeError, ValueError):
            limit = 100
        with self._lock:
            try:
                size = self.path.stat().st_size
                window = min(size, 256 * 1024)
                with open(self.path, "rb") as handle:
                    handle.seek(size - window)
                    raw = handle.read(window)
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise AuditError("审计文件不可读: %s" % exc) from exc
        lines = raw.splitlines()
        if window < size and lines:
            lines = lines[1:]  # 丢掉被截断的首行
        rows = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if wanted and str(row.get("event") or "") != wanted:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows
