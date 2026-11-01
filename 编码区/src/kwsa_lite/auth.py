# -*- coding: utf-8 -*-
"""KWSA lite 面板登录（纯标准库）。

只解决一件事：把状态台和 `/api/*` 从“谁连上谁看”变成“登录后才行”。
- 口令只以 scrypt 哈希落盘（复用 ``core.adminauth`` 的哈希格式，不另造算法）；
- 会话是内存里的随机 sid，重启即失效，不做跨进程共享；
- 登录失败按客户端 IP 指数退避，避免把 8798 当口令爆破靶子；
- Cookie 由本模块拼装，HttpOnly / SameSite=Strict / 可选 Secure。

明确边界：这不是多租户账号系统，只有单一管理员；数据面 `/v1/*` 不走面板会话。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from core.adminauth import hash_password, verify_password

__all__ = [
    "AuthError",
    "PanelAuth",
    "parse_cookies",
    "COOKIE_NAME",
    "PANEL_MIN_PASSWORD",
]

COOKIE_NAME = "kwsa_lite_session"
PANEL_MIN_PASSWORD = 10
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 300


class AuthError(RuntimeError):
    """认证配置或凭据库不可用；调用方必须显式呈现。"""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _valid_username(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64 or any(ord(ch) < 32 for ch in text):
        raise AuthError("面板账号必须是 1-64 个可见字符")
    return text


class AuthStore:
    """`kwsa_lite_auth.json`：账号 + scrypt 口令哈希，不保存明文。"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def stamp(self) -> tuple:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

    def load(self) -> dict | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AuthError("面板凭据文件不可读: %s" % self.path) from exc
            if not isinstance(doc, dict):
                raise AuthError("面板凭据文件格式异常: %s" % self.path)
            stored = str(doc.get("password_hash") or "").strip()
            if not stored.startswith("scrypt$"):
                raise AuthError("面板凭据文件缺少 scrypt 哈希: %s" % self.path)
            username = _valid_username(doc.get("username") or "admin")
            return {"username": username, "password_hash": stored,
                    "updated_at": int(doc.get("updated_at") or 0)}

    def save(self, username: str, password: str) -> dict:
        text = str(password or "")
        if len(text) < PANEL_MIN_PASSWORD:
            raise AuthError("面板密码至少 %d 位" % PANEL_MIN_PASSWORD)
        if len(text) > 256:
            raise AuthError("面板密码不得超过 256 位")
        user = _valid_username(username)
        now = int(time.time())
        doc = {
            "version": 1,
            "username": user,
            "password_hash": hash_password(text),
            "updated_at": now,
        }
        with self._lock:
            _atomic_write(self.path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        return {"username": user, "password_hash": doc["password_hash"],
                "updated_at": now}


class PanelAuth:
    """单管理员会话与登录熔断。"""

    def __init__(self, path, username="admin", ttl=SESSION_TTL,
                 cookie_secure=False, max_failures=LOGIN_MAX_FAILURES,
                 window_s=LOGIN_WINDOW, environ=None):
        self.store = AuthStore(path)
        self.username = _valid_username(username or "admin")
        self.ttl = max(300, int(ttl or SESSION_TTL))
        self.cookie_secure = bool(cookie_secure)
        self.max_failures = max(1, int(max_failures or LOGIN_MAX_FAILURES))
        self.window_s = max(30, int(window_s or LOGIN_WINDOW))
        self._environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()
        self._doc = None
        self._stamp = self.store.stamp()
        self._sessions = {}
        self._fails = defaultdict(deque)
        self.generated_password = ""

    # ── 凭据库 ──────────────────────────────────────────────
    def load(self) -> bool:
        doc = self.store.load()
        with self._lock:
            self._stamp = self.store.stamp()
            if doc is None:
                self._doc = None
                return False
            self._doc = doc
            self.username = doc["username"]
            return True

    def _refresh_if_changed(self) -> None:
        """`--set-password` 在另一个进程改文件时，服务端要立即失效旧会话。"""
        stamp = self.store.stamp()
        with self._lock:
            if stamp == self._stamp:
                return
            self._stamp = stamp
            try:
                doc = self.store.load()
            except AuthError:
                doc = None
            self._doc = doc
            if doc:
                self.username = doc["username"]
            self._sessions.clear()

    def ensure_credentials(self, allow_generate=False) -> str:
        """加载凭据；首次启动可生成一次性随机密码。

        返回生成的口令（仅本次调用者可用于打印一次）；已有凭据返回空串。
        `KWSA_LITE_PASSWORD` 存在时视为部署侧下发，每次启动对齐哈希。
        """
        env_password = str(self._environ.get("KWSA_LITE_PASSWORD") or "")
        with self._lock:
            doc = self.store.load()
            if doc is not None:
                self._doc = doc
                self.username = doc["username"]
                if env_password and not verify_password(env_password, doc["password_hash"]):
                    self._doc = self.store.save(self.username, env_password)
                self._stamp = self.store.stamp()
                return ""
            if env_password:
                self._doc = self.store.save(self.username, env_password)
                self._stamp = self.store.stamp()
                return ""
            if not allow_generate:
                raise AuthError(
                    "面板尚未设置密码；请先运行 --set-password，"
                    "或用 KWSA_LITE_PASSWORD 下发初始密码"
                )
            generated = secrets.token_urlsafe(15)
            self._doc = self.store.save(self.username, generated)
            self._stamp = self.store.stamp()
            self.generated_password = generated
            return generated

    def set_password(self, password: str, username: str | None = None) -> dict:
        with self._lock:
            user = _valid_username(username or self.username)
            self._doc = self.store.save(user, password)
            self._stamp = self.store.stamp()
            self.username = user
            self._sessions.clear()
            return {"username": user, "path": str(self.store.path)}

    @property
    def configured(self) -> bool:
        return self._doc is not None

    def verify(self, username: str, password: str) -> bool:
        self._refresh_if_changed()
        doc = self._doc
        if not doc:
            return False
        user_ok = hmac.compare_digest(
            str(username or "").strip().encode("utf-8"),
            doc["username"].encode("utf-8"),
        )
        # 用户名错误也走一次 scrypt，避免用耗时差异枚举账号。
        pass_ok = verify_password(str(password or ""), doc["password_hash"])
        return bool(user_ok and pass_ok)

    # ── 会话 ────────────────────────────────────────────────
    def issue(self, user: str) -> dict:
        sid = secrets.token_urlsafe(32)
        expires_at = time.time() + self.ttl
        with self._lock:
            self._prune_sessions_locked()
            self._sessions[sid] = {"user": str(user or ""), "expires_at": expires_at}
        return {"sid": sid, "user": str(user or ""), "expires_at": expires_at}

    def session(self, sid: str) -> dict | None:
        if not sid:
            return None
        self._refresh_if_changed()
        now = time.time()
        with self._lock:
            row = self._sessions.get(sid)
            if not row:
                return None
            if float(row["expires_at"]) < now:
                self._sessions.pop(sid, None)
                return None
            return dict(row)

    def revoke(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(sid or ""), None) is not None

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def sessions_count(self) -> int:
        with self._lock:
            self._prune_sessions_locked()
            return len(self._sessions)

    def _prune_sessions_locked(self) -> None:
        now = time.time()
        for sid, row in list(self._sessions.items()):
            if float(row["expires_at"]) < now:
                self._sessions.pop(sid, None)

    # ── 登录熔断 ────────────────────────────────────────────
    def retry_after(self, ip: str) -> int:
        key = str(ip or "")
        now = time.time()
        with self._lock:
            record = self._fails.get(key)
            if not record:
                return 0
            while record and now - record[0] > self.window_s:
                record.popleft()
            if not record or len(record) < self.max_failures:
                return 0
            return max(1, int(self.window_s - (now - record[0])))

    def note_failure(self, ip: str) -> None:
        key = str(ip or "")
        now = time.time()
        with self._lock:
            record = self._fails[key]
            while record and now - record[0] > self.window_s:
                record.popleft()
            record.append(now)
            while len(record) > self.max_failures * 4:
                record.popleft()

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(str(ip or ""), None)

    # ── Cookie ──────────────────────────────────────────────
    def cookie_header(self, sid: str, secure=None) -> str:
        parts = [
            "%s=%s" % (COOKIE_NAME, sid),
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=%d" % self.ttl,
        ]
        if (self.cookie_secure if secure is None else bool(secure)):
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self, secure=None) -> str:
        parts = [
            "%s=" % COOKIE_NAME,
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
            "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        ]
        if (self.cookie_secure if secure is None else bool(secure)):
            parts.append("Secure")
        return "; ".join(parts)


def parse_cookies(header: str) -> dict:
    out = {}
    for part in str(header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out
