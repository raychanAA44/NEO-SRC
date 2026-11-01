# -*- coding: utf-8 -*-
"""KWSA lite 渠道密钥仓（纯标准库）。

目标：让面板可以收录渠道密钥，但磁盘上不出现明文、接口只回掩码。

设计边界：
- 密文文件与密钥文件分离：``kwsa_lite_creds.json`` + ``kwsa_lite_creds.key``。
- 使用 HMAC-SHA256 计数器流加密 + HMAC 标签；只依赖 hashlib / hmac / secrets。
- 标签不匹配即抛 :class:`VaultError`，绝不返回“半截明文”。
- 这不是硬件级保险库；它防的是误提交、随手 grep 和接口回显，不替代操作系统账户隔离。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

__all__ = ["SecretVault", "VaultError", "mask_token"]


class VaultError(RuntimeError):
    """密钥仓读写失败；调用方必须显式呈现，不能吞掉。"""


def mask_token(value: str) -> str:
    """只回可辨识掩码：保留前 2 后 2，长度不足一律实心块。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 6:
        return "●" * max(4, len(text))
    return text[:2] + "●" * 6 + text[-2:]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text or "").encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise VaultError("密钥仓字段不是合法 base64") from exc


class SecretVault:
    """渠道名 -> 明文密钥 的本地加密映射。"""

    def __init__(self, path, key_path=None):
        self.path = Path(path)
        self.key_path = Path(key_path) if key_path else self._default_key_path(self.path)
        self._lock = threading.RLock()
        self._key = self._load_key()
        self._items = self._load_items()

    @staticmethod
    def _default_key_path(path: Path) -> Path:
        return path.with_name(path.stem + ".key")

    # ── 密钥文件 ────────────────────────────────────────────
    def _load_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            try:
                key = base64.urlsafe_b64decode(raw)
            except Exception as exc:  # noqa: BLE001
                raise VaultError("密钥文件损坏，无法解出渠道凭据: %s" % self.key_path) from exc
            if len(key) != 32:
                raise VaultError("密钥文件长度异常（需 32 字节）: %s" % self.key_path)
            return key
        key = secrets.token_bytes(32)
        self._atomic_write(self.key_path, base64.urlsafe_b64encode(key))
        return key

    # ── 密文文件 ────────────────────────────────────────────
    def _load_items(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VaultError("密钥仓文件不可读: %s" % self.path) from exc
        items = doc.get("items") if isinstance(doc, dict) else None
        return items if isinstance(items, dict) else {}

    def _save_items(self) -> None:
        payload = {
            "version": 1,
            "cipher": "hmac-sha256-keystream",
            "items": self._items,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write(self.path, text.encode("utf-8"))

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    # ── 加解密 ──────────────────────────────────────────────
    def _keystream(self, nonce: bytes, length: int) -> bytes:
        chunks = []
        counter = 0
        while sum(len(chunk) for chunk in chunks) < length:
            chunks.append(hmac.new(
                self._key,
                b"kwsa-lite/creds/v1/" + nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest())
            counter += 1
        return b"".join(chunks)[:length]

    def _seal(self, plaintext: str) -> dict:
        nonce = secrets.token_bytes(16)
        raw = plaintext.encode("utf-8")
        stream = self._keystream(nonce, len(raw))
        cipher = bytes(a ^ b for a, b in zip(raw, stream))
        tag = hmac.new(self._key, b"kwsa-lite/tag/v1/" + nonce + cipher,
                       hashlib.sha256).digest()
        return {"nonce": _b64(nonce), "ct": _b64(cipher), "tag": _b64(tag)}

    def _open(self, blob: dict) -> str:
        if not isinstance(blob, dict):
            raise VaultError("密钥仓条目结构异常")
        nonce = _unb64(blob.get("nonce"))
        cipher = _unb64(blob.get("ct"))
        tag = _unb64(blob.get("tag"))
        expected = hmac.new(self._key, b"kwsa-lite/tag/v1/" + nonce + cipher,
                            hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise VaultError("密钥仓标签校验失败：密钥文件被换过或密文被改")
        return bytes(a ^ b for a, b in zip(cipher, self._keystream(nonce, len(cipher)))
                     ).decode("utf-8")

    # ── 对外接口 ────────────────────────────────────────────
    def get(self, name: str) -> str:
        key = str(name or "").strip()
        if not key:
            return ""
        with self._lock:
            blob = self._items.get(key)
            if not blob:
                return ""
            return self._open(blob)

    def has(self, name: str) -> bool:
        with self._lock:
            return str(name or "").strip() in self._items

    def set(self, name: str, value: str) -> None:
        key = str(name or "").strip()
        text = str(value or "").strip()
        if not key:
            raise VaultError("渠道名不能为空")
        if not text:
            raise VaultError("密钥不能为空")
        with self._lock:
            self._items[key] = self._seal(text)
            self._save_items()

    def delete(self, name: str) -> bool:
        key = str(name or "").strip()
        with self._lock:
            if key not in self._items:
                return False
            self._items.pop(key, None)
            self._save_items()
            return True

    def names(self) -> list:
        with self._lock:
            return sorted(self._items)

    def snapshot(self) -> dict:
        """只回掩码，供状态台复用；绝不回明文。"""
        rows = {}
        for name in self.names():
            try:
                rows[name] = {"present": True, "masked": mask_token(self.get(name))}
            except VaultError as exc:
                rows[name] = {"present": True, "masked": "", "error": str(exc)}
        return rows
