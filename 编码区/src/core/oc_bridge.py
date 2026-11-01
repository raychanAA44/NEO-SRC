#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/oc_bridge.py —— OC 桥令牌单表（在线查看/更新，落盘即密文）。

铁律与 `custom_upstreams` 一致：
1. 明文只在「写入响应」里以掩码形式回显，落盘用 vendor_creds 的 PBKDF2+HMAC 流加密；
2. 列表/状态接口只出掩码与更新时间，绝不回显完整令牌；
3. 空令牌 = 清除；更新走临时文件 + os.replace，文件 0600。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .vendor_creds import VendorCreds, seal, unseal

from core.paths import DATA_DIR
DEFAULT_PATH = DATA_DIR / "oc_bridge.json"


def _mask(token: str) -> str:
    text = str(token or "")
    if not text:
        return ""
    if len(text) <= 10:
        return "●" * 8
    return "%s…%s" % (text[:6], text[-4:])


class OcBridgeStore:
    """`运维区/data/oc_bridge.json` 的读写门面：一条密文记录 + 掩码。"""

    def __init__(self, path=None, *, passphrase=None, environ=None):
        self.path = Path(path or os.environ.get("HUB_OC_BRIDGE") or DEFAULT_PATH)
        self._passphrase = str(passphrase or "")
        self._environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()

    @property
    def passphrase(self) -> str:
        return self._passphrase or VendorCreds().passphrase

    def _read(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(self.path))

    def get_token(self) -> str:
        blob = self._read().get("blob")
        if not isinstance(blob, dict):
            return ""
        try:
            plain = unseal(blob, self.passphrase)
        except Exception:                   # noqa: BLE001 密钥文件丢了按未配置呈现
            return ""
        return str((plain or {}).get("token") or "")

    def public(self) -> dict:
        doc = self._read()
        token = self.get_token()
        return {
            "ok": True,
            "configured": bool(token),
            "has_token": bool(token),
            "token_masked": _mask(token),
            "note": str(doc.get("note") or ""),
            "updated_at": int(doc.get("updated_at") or 0),
            "source": "local-oc-bridge",
        }

    def set_token(self, token: str, note: str = "") -> dict:
        value = str(token or "").strip()
        with self._lock:
            doc = self._read()
            if not value:
                self._write({"schema": 1, "updated_at": int(time.time()),
                             "note": str(note or "")})
                self._environ.pop("HUB_OC_BRIDGE_TOKEN", None)
                return {"ok": True, "configured": False, "cleared": True,
                        "token_masked": "", "updated_at": int(time.time())}
            if len(value) < 8:
                raise ValueError("Bridge Token 至少 8 位")
            doc = {"schema": 1, "updated_at": int(time.time()),
                   "note": str(note or doc.get("note") or "")[:120],
                   "blob": seal({"token": value}, self.passphrase)}
            self._write(doc)
            self._environ["HUB_OC_BRIDGE_TOKEN"] = value
        return {"ok": True, "configured": True, "cleared": False,
                "token_masked": _mask(value),
                "updated_at": int(doc["updated_at"]),
                "note": doc.get("note") or ""}


__all__ = ["DEFAULT_PATH", "OcBridgeStore"]
