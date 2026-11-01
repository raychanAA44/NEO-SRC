# -*- coding: utf-8 -*-
"""KWSA lite 数据面客户端 API Key（纯标准库）。

只解决一件事：让 ``/v1/*`` 不再是谁连上谁用，而是必须持有面板签发的 Key。
- 明文 Key 只在创建时返回一次；落盘只存 SHA-256 哈希、前缀与元数据；
- Key 文件与面板凭据分离，吊销/停用立即生效，不需要重启；
- 每个 Key 独立令牌桶（rps / burst），与全局按 IP 限流叠加；
- 文件变更按 mtime+size 热加载，CLI 或面板在另一个进程写入也能被服务端看到。

明确边界：这不是多租户账号系统；Key 不绑定模型/分组，仅做准入与速率隔离。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from core.rate_limiter import TokenBucket

__all__ = [
    "ClientKeyError",
    "ClientKeyStore",
    "KEY_PREFIX",
    "KEY_ID_RE",
]

KEY_PREFIX = "kwsl_"
KEY_ID_RE = re.compile(r"^[0-9a-f]{8}$")
KEY_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
MAX_KEYS = 50


class ClientKeyError(RuntimeError):
    """Key 仓不可用或参数非法；调用方必须显式呈现。"""


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


def _digest(secret: str) -> str:
    return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()


def _valid_name(value) -> str:
    name = str(value or "").strip()
    if not KEY_NAME_RE.match(name):
        raise ClientKeyError("Key 名称必须是 1-64 个可见字符")
    return name


def _valid_rate(value, default: float, label: str) -> float:
    try:
        number = float(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ClientKeyError("%s 必须是数字" % label) from exc
    if not (0.1 <= number <= 10000):
        raise ClientKeyError("%s 必须在 0.1-10000 之间" % label)
    return round(number, 2)


class ClientKeyStore:
    """``kwsa_lite_client_keys.json`` 的读写、校验与每 Key 限流。"""

    def __init__(self, path, default_rps: float = 20.0, default_burst: float = 40.0):
        self.path = Path(path)
        self.default_rps = _valid_rate(default_rps, 20.0, "default_rps")
        self.default_burst = _valid_rate(default_burst, 40.0, "default_burst")
        self._lock = threading.RLock()
        self._doc = {"version": 1, "keys": []}
        self._stamp = (0, 0)
        self._buckets = {}
        self._usage = {}
        self._load()

    # ── 文件与热加载 ────────────────────────────────────────
    def _stat(self) -> tuple:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

    def _load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        stamp = self._stat()
        if not self.path.exists():
            self._doc = {"version": 1, "keys": []}
            self._stamp = (0, 0)
            self._buckets.clear()
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClientKeyError("数据面 Key 文件不可读: %s" % self.path) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise ClientKeyError("数据面 Key 文件格式异常: %s" % self.path)
        rows, seen_ids, seen_names = [], set(), set()
        for raw in doc["keys"]:
            if not isinstance(raw, dict):
                raise ClientKeyError("数据面 Key 条目必须是对象")
            key_id = str(raw.get("id") or "").strip()
            if not KEY_ID_RE.match(key_id) or key_id in seen_ids:
                raise ClientKeyError("数据面 Key 条目 id 非法或重复")
            name = _valid_name(raw.get("name"))
            folded = name.casefold()
            if folded in seen_names:
                raise ClientKeyError("数据面 Key 名称重复: %s" % name)
            seen_ids.add(key_id)
            seen_names.add(folded)
            digest = str(raw.get("hash") or "").strip().lower()
            if not re.match(r"^[0-9a-f]{64}$", digest):
                raise ClientKeyError("数据面 Key 条目缺少 SHA-256 哈希")
            rows.append({
                "id": key_id,
                "name": name,
                "prefix": str(raw.get("prefix") or KEY_PREFIX)[:16],
                "hash": digest,
                "enabled": bool(raw.get("enabled", True)),
                "rps": _valid_rate(raw.get("rps"), self.default_rps, "rps"),
                "burst": _valid_rate(raw.get("burst"), self.default_burst, "burst"),
                "created_at": int(raw.get("created_at") or 0),
            })
        self._doc = {"version": 1, "keys": rows}
        self._stamp = stamp
        # 文件被外部改过：旧桶与旧用量不再可信，清掉重来。
        self._buckets.clear()
        self._usage = {key: value for key, value in self._usage.items()
                       if key in seen_ids}

    def _ensure_loaded(self) -> None:
        if self._stat() != self._stamp:
            with self._lock:
                if self._stat() != self._stamp:
                    self._load_locked()

    def _save_locked(self) -> None:
        payload = {"version": 1, "keys": [
            {key: row[key] for key in ("id", "name", "prefix", "hash", "enabled",
                                       "rps", "burst", "created_at")}
            for row in self._doc["keys"]
        ]}
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self._stamp = self._stat()

    # ── 查询 ────────────────────────────────────────────────
    def _usage_of(self, key_id: str) -> dict:
        return self._usage.get(key_id) or {"last_used_at": 0, "requests": 0}

    def _public(self, row: dict) -> dict:
        usage = self._usage_of(row["id"])
        return {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["prefix"],
            "enabled": bool(row["enabled"]),
            "rps": row["rps"],
            "burst": row["burst"],
            "created_at": row["created_at"],
            "last_used_at": int(usage["last_used_at"] or 0) or None,
            "requests": int(usage["requests"] or 0),
        }

    def snapshot(self) -> list:
        self._ensure_loaded()
        with self._lock:
            return [self._public(row) for row in self._doc["keys"]]

    def get(self, key_id: str) -> dict | None:
        self._ensure_loaded()
        wanted = str(key_id or "").strip()
        with self._lock:
            for row in self._doc["keys"]:
                if row["id"] == wanted:
                    return self._public(row)
        return None

    def active_count(self) -> int:
        self._ensure_loaded()
        with self._lock:
            return sum(1 for row in self._doc["keys"] if row["enabled"])

    # ── 签发 / 停用 / 吊销 ─────────────────────────────────
    def create(self, name: str, rps=None, burst=None) -> tuple:
        label = _valid_name(name)
        rate = _valid_rate(rps, self.default_rps, "rps")
        capacity = _valid_rate(burst, self.default_burst, "burst")
        self._ensure_loaded()
        secret = KEY_PREFIX + secrets.token_urlsafe(30)
        with self._lock:
            rows = self._doc["keys"]
            if len(rows) >= MAX_KEYS:
                raise ClientKeyError("数据面 Key 最多 %d 个" % MAX_KEYS)
            if any(row["name"].casefold() == label.casefold() for row in rows):
                raise ClientKeyError("Key 名称已存在: %s" % label)
            key_id = secrets.token_hex(4)
            while any(row["id"] == key_id for row in rows):
                key_id = secrets.token_hex(4)
            row = {
                "id": key_id,
                "name": label,
                "prefix": secret[:12],
                "hash": _digest(secret),
                "enabled": True,
                "rps": rate,
                "burst": capacity,
                "created_at": int(time.time()),
            }
            rows.append(row)
            self._save_locked()
            return secret, self._public(row)

    def set_enabled(self, key_id: str, enabled: bool) -> dict | None:
        self._ensure_loaded()
        wanted = str(key_id or "").strip()
        with self._lock:
            for row in self._doc["keys"]:
                if row["id"] == wanted:
                    row["enabled"] = bool(enabled)
                    self._save_locked()
                    self._buckets.pop(wanted, None)
                    return self._public(row)
        return None

    def revoke(self, key_id: str) -> dict | None:
        self._ensure_loaded()
        wanted = str(key_id or "").strip()
        with self._lock:
            for index, row in enumerate(self._doc["keys"]):
                if row["id"] == wanted:
                    removed = self._public(row)
                    self._doc["keys"].pop(index)
                    self._usage.pop(wanted, None)
                    self._buckets.pop(wanted, None)
                    self._save_locked()
                    return removed
        return None

    # ── 鉴权与限流 ─────────────────────────────────────────
    def authenticate(self, secret: str) -> dict | None:
        text = str(secret or "").strip()
        if len(text) < 20 or len(text) > 200:
            return None
        digest = _digest(text)
        self._ensure_loaded()
        found = None
        with self._lock:
            for row in self._doc["keys"]:
                if not row["enabled"]:
                    continue
                if hmac.compare_digest(row["hash"], digest) and found is None:
                    found = row
        return self._public(found) if found else None

    def allow(self, key: dict, path: str = "") -> tuple:
        """按 Key 独立令牌桶放行；返回 ``(allowed, retry_after)``。"""
        if not key or not key.get("enabled"):
            return False, 1.0
        key_id = str(key.get("id") or "")
        with self._lock:
            bucket = self._buckets.get(key_id)
            if bucket is None:
                bucket = TokenBucket(rate=float(key["rps"]), capacity=float(key["burst"]))
                self._buckets[key_id] = bucket
        return bucket.acquire(1.0)

    def record_use(self, key: dict) -> None:
        if not key:
            return
        key_id = str(key.get("id") or "")
        now = int(time.time())
        with self._lock:
            usage = self._usage.setdefault(key_id, {"last_used_at": 0, "requests": 0})
            usage["last_used_at"] = now
            usage["requests"] += 1
