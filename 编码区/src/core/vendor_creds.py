#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/vendor_creds.py —— 第三方渠道凭据保险仓（只用标准库，对外只出掩码）。

设计铁律：
1. **零外部依赖**：PBKDF2-HMAC-SHA256 派生密钥 + HMAC-SHA256 计数器流加密 + HMAC 认证标签，
   全部来自 `hashlib` / `hmac` / `secrets`，不引任何第三方加密包。
2. **落盘即密文**：`运维区/data/vendor_creds.json` 里只有 salt / nonce / 密文 / 标签，
   明文凭据只在进程内存里出现，日志与接口一律走 `mask_*`。
3. **密钥不落仓**：口令优先取环境变量 `HUB_VENDOR_KEY`；没有就自动生成本地密钥文件
   （0600），文件本身不进版本库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

ITERATIONS = 120_000
NONCE_BYTES = 16
SALT_BYTES = 16
BLOCK = hashlib.sha256().digest_size
SCHEMA = 1

from core.paths import DATA_DIR
DEFAULT_PATH = DATA_DIR / "vendor_creds.json"
DEFAULT_KEY_PATH = DATA_DIR / "vendor_creds.key"
KEY_ENV = "HUB_VENDOR_KEY"

# 这些字段一律只出掩码；其余字段（如 username / realm）原样回显便于使用者确认身份。
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "token", "access_token", "refresh_token", "id_token",
    "secret", "client_secret", "cookie", "cookies", "session", "api_key",
    "apikey", "key", "authorization",
})


class VendorCredsError(RuntimeError):
    """凭据仓不可用（密钥缺失 / 密文损坏 / 口令不对）。"""


# ── 加解密基元 ──────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(str(text or "").encode("ascii"))


def _derive(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    raw = hashlib.pbkdf2_hmac("sha256", str(passphrase).encode("utf-8"), salt, ITERATIONS, 64)
    return raw[:32], raw[32:]


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:size])


def seal(payload: dict, passphrase: str) -> dict:
    """加密一个 dict，返回可直接落盘的密文记录（含 salt/nonce/tag）。"""
    plain = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    enc_key, mac_key = _derive(passphrase, salt)
    stream = _keystream(enc_key, nonce, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, stream))
    tag = hmac.new(mac_key, b"v1" + salt + nonce + cipher, hashlib.sha256).digest()
    return {"v": SCHEMA, "kdf": "pbkdf2-sha256", "iter": ITERATIONS,
            "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(cipher), "tag": _b64(tag)}


def unseal(blob: dict, passphrase: str) -> dict:
    """解密一条密文记录；口令错或密文被改一律抛 VendorCredsError。"""
    try:
        salt, nonce = _unb64(blob["salt"]), _unb64(blob["nonce"])
        cipher, tag = _unb64(blob["ct"]), _unb64(blob["tag"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VendorCredsError("密文记录不完整: %s" % exc) from exc
    enc_key, mac_key = _derive(passphrase, salt)
    want = hmac.new(mac_key, b"v1" + salt + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(want, tag):
        raise VendorCredsError("口令不对或密文被篡改")
    stream = _keystream(enc_key, nonce, len(cipher))
    try:
        return json.loads(bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8"))
    except ValueError as exc:
        raise VendorCredsError("密文解密失败: %s" % exc) from exc


# ── 脱敏 ────────────────────────────────────────────────────
def mask_value(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "●" * 6 if len(text) <= 8 else "%s●●●%s" % (text[:2], text[-2:])


def mask_creds(creds: dict) -> dict:
    """对外展示：敏感字段掩码，非敏感字段（username/realm/scope）原样保留。"""
    out = {}
    for key, value in (creds or {}).items():
        out[key] = mask_value(value) if str(key).lower() in SENSITIVE_KEYS else value
    return out


# ── 凭据仓 ──────────────────────────────────────────────────
class VendorCreds:
    """`运维区/data/vendor_creds.json` 的读写门面：一条渠道一份密文记录。"""

    def __init__(self, path=None, passphrase: str = "", key_path=None):
        self.path = Path(path or os.environ.get("HUB_VENDOR_CREDS") or DEFAULT_PATH)
        self.key_path = Path(key_path or (self.path.parent / "vendor_creds.key"))
        self._passphrase = str(passphrase or "")

    # 口令解析：环境变量 → 构造参数 → 本地密钥文件（不存在则生成 0600）
    @property
    def passphrase(self) -> str:
        if self._passphrase:
            return self._passphrase
        env = (os.environ.get(KEY_ENV) or "").strip()
        if env:
            return env
        try:
            if self.key_path.is_file():
                return self.key_path.read_text(encoding="utf-8").strip()
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_hex(32)
            self.key_path.write_text(secret + "\n", encoding="utf-8")
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            return secret
        except OSError as exc:
            raise VendorCredsError("无法取得本地密钥: %s" % exc) from exc

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"schema": SCHEMA, "channels": {}}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": SCHEMA, "channels": {}}
        if not isinstance(doc, dict):
            return {"schema": SCHEMA, "channels": {}}
        doc.setdefault("channels", {})
        return doc

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def save(self, channel: str, creds: dict, scope: str = "default") -> dict:
        """加密落盘，返回**掩码**记录（明文绝不回给调用方之外的人）。"""
        name = str(channel or "").strip()
        if not name:
            raise VendorCredsError("channel 必填")
        if not isinstance(creds, dict) or not creds:
            raise VendorCredsError("creds 必须是非空对象")
        doc = self._read()
        now = int(time.time())
        doc["channels"][name] = {
            "scope": str(scope or "default"),
            "updated_at": now,
            "blob": seal(dict(creds), self.passphrase),
        }
        self._write(doc)
        return {"channel": name, "scope": str(scope or "default"), "updated_at": now,
                "masked": mask_creds(creds), "stored": True}

    def get(self, channel: str) -> dict | None:
        """取明文（仅内部用于登录/抓取）；不存在返回 None。"""
        rec = self._read()["channels"].get(str(channel or "").strip())
        if not isinstance(rec, dict) or not isinstance(rec.get("blob"), dict):
            return None
        return unseal(rec["blob"], self.passphrase)

    def masked(self, channel: str) -> dict:
        rec = self._read()["channels"].get(str(channel or "").strip())
        if not isinstance(rec, dict):
            return {}
        plain = self.get(channel) or {}
        return {"channel": channel, "scope": rec.get("scope", "default"),
                "updated_at": rec.get("updated_at", 0), "masked": mask_creds(plain)}

    def drop(self, channel: str) -> bool:
        doc = self._read()
        name = str(channel or "").strip()
        if name not in doc["channels"]:
            return False
        doc["channels"].pop(name, None)
        self._write(doc)
        return True

    def channels(self) -> list[dict]:
        """所有已存凭据的掩码视图（供 06 视图展示「登录即同步」状态）。"""
        doc = self._read()
        out = []
        for name, rec in (doc.get("channels") or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                plain = self.get(name) or {}
            except VendorCredsError:
                plain = {}
            out.append({"channel": name, "scope": rec.get("scope", "default"),
                        "updated_at": rec.get("updated_at", 0),
                        "masked": mask_creds(plain)})
        return out


__all__ = ["VendorCreds", "VendorCredsError", "seal", "unseal", "mask_value",
           "mask_creds", "SENSITIVE_KEYS", "DEFAULT_PATH", "DEFAULT_KEY_PATH",
           "KEY_ENV", "SCHEMA"]
