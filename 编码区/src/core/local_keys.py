#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/local_keys.py —— 本地模式 API KEY 单表（签发 / 启停 / 吊销）。

铁律：
1. **明文只出现一次**：`issue()` 返回值里带完整令牌，落盘只有 SHA-256 哈希，
   列表接口只回前缀 + 后四位的掩码。
2. **零外部依赖**：随机数用 `secrets`，哈希用 `hashlib`，没有第三方库。
3. **原子落盘**：临时文件 + `os.replace`，文件 0600；损坏时退回空表而不是崩服务。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from core.paths import DATA_DIR
DEFAULT_PATH = DATA_DIR / "local_keys.json"
KEY_PREFIX = "sk-hub-neo-"
NAME_MAX = 48


def _mask(token: str) -> str:
    text = str(token or "")
    if len(text) <= 8:
        return "●" * 8
    return "%s●●●●%s" % (text[:len(KEY_PREFIX) + 2], text[-4:])


def _hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


class LocalKeyStore:
    """`运维区/data/local_keys.json` 的读写门面。"""

    def __init__(self, path=None, *, clock=time.time):
        self.path = Path(path or os.environ.get("HUB_LOCAL_KEYS") or DEFAULT_PATH)
        self._clock = clock
        self._lock = threading.RLock()

    # ── 存储 ────────────────────────────────────────────────
    def _read(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": 1, "keys": {}}
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict):
            return {"schema": 1, "keys": {}}
        return doc

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(self.path))

    @staticmethod
    def _public(key_id: str, rec: dict) -> dict:
        return {
            "id": key_id,
            "name": str(rec.get("name") or key_id),
            "realm": str(rec.get("realm") or "all"),
            "scope": str(rec.get("scope") or ""),
            "note": str(rec.get("note") or ""),
            "enabled": bool(rec.get("enabled", True)),
            "key_masked": str(rec.get("key_masked") or ""),
            "created_at": int(rec.get("created_at") or 0),
            "last_used_at": int(rec.get("last_used_at") or 0),
            "uses": int(rec.get("uses") or 0),
        }

    # ── 读 ──────────────────────────────────────────────────
    def list_public(self) -> list:
        doc = self._read()
        rows = [self._public(key_id, rec) for key_id, rec in doc["keys"].items()
                if isinstance(rec, dict)]
        rows.sort(key=lambda row: (-row["created_at"], row["name"]))
        return rows

    def exists(self, key_id: str) -> bool:
        return str(key_id or "") in (self._read()["keys"])

    def verify(self, token: str) -> dict | None:
        """按明文校验（本地自建下游鉴权将来接这里）；命中回公开条目。"""
        digest = _hash(token)
        for key_id, rec in (self._read()["keys"] or {}).items():
            if isinstance(rec, dict) and secrets.compare_digest(str(rec.get("hash") or ""), digest):
                return self._public(key_id, rec) if rec.get("enabled", True) else None
        return None

    # ── 写 ──────────────────────────────────────────────────
    def issue(self, name: str, realm: str = "all", scope: str = "",
              note: str = "") -> dict:
        label = str(name or "").strip()
        if not label:
            raise ValueError("KEY 名称必填")
        if len(label) > NAME_MAX:
            raise ValueError("KEY 名称过长（≤%d）" % NAME_MAX)
        token = KEY_PREFIX + secrets.token_urlsafe(24)
        key_id = "key_" + secrets.token_hex(4)
        now = int(self._clock())
        rec = {
            "name": label,
            "realm": str(realm or "all").strip() or "all",
            "scope": str(scope or "").strip(),
            "note": str(note or "").strip()[:120],
            "hash": _hash(token),
            "key_masked": _mask(token),
            "created_at": now,
            "last_used_at": 0,
            "uses": 0,
            "enabled": True,
        }
        with self._lock:
            doc = self._read()
            doc["keys"][key_id] = rec
            self._write(doc)
        return {"ok": True, "key": token, "issued": key_id,
                "item": self._public(key_id, rec),
                "note": "明文仅本次响应返回一次，落盘只有哈希"}

    def toggle(self, key_id: str, enabled=None) -> dict:
        key_id = str(key_id or "").strip()
        with self._lock:
            doc = self._read()
            rec = doc["keys"].get(key_id)
            if not isinstance(rec, dict):
                raise ValueError("KEY 不存在: %s" % key_id)
            before = bool(rec.get("enabled", True))
            rec["enabled"] = (not before) if enabled is None else bool(enabled)
            self._write(doc)
        return {"ok": True, "id": key_id, "enabled": rec["enabled"],
                "changed": 1 if rec["enabled"] != before else 0}

    def revoke(self, key_id: str) -> dict:
        key_id = str(key_id or "").strip()
        with self._lock:
            doc = self._read()
            existed = doc["keys"].pop(key_id, None)
            if existed is None:
                raise ValueError("KEY 不存在: %s" % key_id)
            self._write(doc)
        return {"ok": True, "revoked": key_id, "changed": 1}

    def set_realm(self, key_id: str, realm: str) -> dict:
        key_id = str(key_id or "").strip()
        value = str(realm or "all").strip() or "all"
        with self._lock:
            doc = self._read()
            rec = doc["keys"].get(key_id)
            if not isinstance(rec, dict):
                raise ValueError("KEY 不存在: %s" % key_id)
            before = str(rec.get("realm") or "all")
            rec["realm"] = value
            self._write(doc)
        return {"ok": True, "id": key_id, "realm": value,
                "changed": 1 if value != before else 0}


__all__ = ["DEFAULT_PATH", "KEY_PREFIX", "LocalKeyStore"]
