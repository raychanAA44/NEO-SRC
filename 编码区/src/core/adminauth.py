#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/adminauth.py —— 管理面登录认证与防爆破熔断。

1. 密码哈希：scrypt (RFC 7914)，单次耗时约 50~100ms 抗暴力破解。
2. 防爆破熔断：同一 IP 连续失败 ≥5 次触发指数退避（60s ~ 3600s）。
3. 会话状态：内存管理 sid 映射，有效期限 7 天。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
DK_LEN = 32
SESSION_TTL = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_BASE = 60
LOCK_MAX = 3600
MIN_PASSWORD_LEN = 8

from core.paths import DATA_DIR
PASSWORD_PATH = Path(os.environ.get("HUB_ADMIN_AUTH") or (DATA_DIR / "admin_auth.json"))


def _load_persisted_hash(path=None) -> str:
    """本地面板改过的口令哈希：环境变量优先，其次落盘文件；都没有返回空串。"""
    target = Path(path or PASSWORD_PATH)
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    value = str(doc.get("password_hash") or "").strip()
    return value if value.startswith("scrypt$") else ""


ADMIN_USER = (os.environ.get("HUB_ADMIN_USER") or "admin").strip()
ADMIN_PASS_HASH = ((os.environ.get("HUB_ADMIN_PASS_HASH") or "").strip()
                   or _load_persisted_hash())
ADMIN_TOKEN = (os.environ.get("HUB_ADMIN_TOKEN") or "").strip()

_lock = threading.Lock()
_sessions: dict[str, float] = {}       # sid -> expire_ts
_fails: dict[str, list[float]] = {}    # ip -> [count, until_ts]


def hash_password(pw: str, salt: bytes | None = None) -> str:
    """生成 scrypt 哈希串：scrypt$<n>$<r>$<p>$<b64salt>$<b64dk>"""
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(
        pw.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=DK_LEN,
        maxmem=64 * 1024 * 1024,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(pw: str, stored: str) -> bool:
    """常数时间校验密码。"""
    if not stored or not pw:
        return False
    try:
        algo, n, r, p, b64s, b64d = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(b64s)
        want = base64.b64decode(b64d)
        got = hashlib.scrypt(
            pw.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(want),
            maxmem=64 * 1024 * 1024,
        )
        return secrets.compare_digest(got, want)
    except Exception:
        return False


def new_session() -> str:
    """生成新会话 ID 并记录过期时间。"""
    sid = secrets.token_urlsafe(32)
    with _lock:
        _sessions[sid] = time.time() + SESSION_TTL
    return sid


def verify_session(sid: str) -> bool:
    """校验会话是否有效。"""
    if not sid:
        return False
    now = time.time()
    with _lock:
        exp = _sessions.get(sid)
        if exp and exp >= now:
            return True
        if sid in _sessions:
            _sessions.pop(sid, None)
    return False


def drop_session(sid: str) -> None:
    """注销会话。"""
    with _lock:
        _sessions.pop(sid or "", None)


def session_count() -> int:
    """当前活跃会话数。"""
    now = time.time()
    with _lock:
        return sum(1 for v in _sessions.values() if v >= now)


def locked_for(ip: str) -> int:
    """查询 IP 锁定剩余秒数（0 = 未锁定）。"""
    with _lock:
        rec = _fails.get(ip)
        if not rec:
            return 0
        left = int(rec[1] - time.time())
        return max(0, left)


def note_fail(ip: str) -> int:
    """记录一次认证失败，返回当前锁定秒数。连续 ≥5 次触发指数退避。"""
    with _lock:
        rec = _fails.get(ip, [0, 0.0])
        cnt = int(rec[0]) + 1
        until = 0.0
        if cnt >= MAX_FAILS:
            delay = min(LOCK_BASE * (2 ** (cnt - MAX_FAILS)), LOCK_MAX)
            until = time.time() + delay
        _fails[ip] = [cnt, until]
        return max(0, int(until - time.time()))


def note_success(ip: str) -> None:
    """认证成功清除失败计数。"""
    with _lock:
        _fails.pop(ip, None)


def fail_stats() -> dict:
    """获取所有受限 IP 统计。"""
    now = time.time()
    with _lock:
        return {
            k: {"fails": int(v[0]), "locked": max(0, int(v[1] - now))}
            for k, v in _fails.items()
        }


def reset_fails(ip: str = "") -> int:
    """重置失败状态（用于测试或管理员解锁）。"""
    with _lock:
        if not ip:
            n = len(_fails)
            _fails.clear()
            return n
        return 1 if _fails.pop(ip, None) is not None else 0


def set_password(password: str, path=None) -> dict:
    """改管理员口令：scrypt 重算 + 原子落盘 0600，并立即切到新哈希。

    旧口令校验由调用方（server）先做；这里只负责强度与持久化。
    """
    global ADMIN_PASS_HASH
    text = str(password or "")
    if len(text) < MIN_PASSWORD_LEN:
        raise ValueError("新口令至少 %d 位" % MIN_PASSWORD_LEN)
    digest = hash_password(text)
    target = Path(path or PASSWORD_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    now = int(time.time())
    tmp.write_text(json.dumps({"password_hash": digest, "updated_at": now},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(target))
    ADMIN_PASS_HASH = digest
    with _lock:
        _sessions.clear()          # 改密即让旧会话失效
    return {"ok": True, "configured": True, "updated_at": now,
            "path": str(target)}


__all__ = [
    "ADMIN_PASS_HASH",
    "ADMIN_TOKEN",
    "ADMIN_USER",
    "MIN_PASSWORD_LEN",
    "PASSWORD_PATH",
    "fail_stats",
    "hash_password",
    "locked_for",
    "note_fail",
    "note_success",
    "reset_fails",
    "set_password",
    "verify_password",
]
