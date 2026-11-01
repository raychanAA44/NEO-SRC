#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/custom_upstreams.py —— 第三方渠道在线接入存储（06 视图增删改的唯一写入口）。

设计铁律：
1. **零新依赖**：只用标准库；密钥复用 `vendor_creds.seal/unseal`（PBKDF2 + HMAC 流加密），
   落盘只有 salt / nonce / 密文 / 标签，明文只在内存与「签发响应」里出现一次。
2. **单一出处**：渠道元数据（base_url / models / headers / note）写进
   `运维区/configs/model_upstreams.json` 的 `channels`，路由表 reload 即热生效；
   密钥单独封存在 `运维区/data/custom_upstreams.json`，配置文件里只留 `token_env` 名。
3. **命名契约**：渠道名即 `name`（如 `acme`），路由表据此生成
   `acme/<model>` 与 `custom/acme/<model>` 两种别名；旧配置里带 `custom:`
   前缀的同名渠道在读写时按同一渠道归一，避免价目表口径打架。
4. **改而不毁**：更新时保留配置里未在表单中出现的字段（endpoint / aliases / catalog）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from .vendor_creds import VendorCreds, seal, unseal

from core.paths import DATA_DIR
DEFAULT_KEYS_PATH = DATA_DIR / "custom_upstreams.json"
CHANNEL_PREFIX = "custom:"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MASK_SENTINEL = "●"
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})


class CustomUpstreamError(ValueError):
    """自定义渠道配置非法（400 口径）。"""


def env_name_for(name: str) -> str:
    """渠道名 → 进程环境变量名（密钥只经环境注入路由表，不写进配置文件）。"""
    slug = re.sub(r"[^A-Z0-9]+", "_", str(name or "").upper()).strip("_")
    return "HUB_CUSTOM_%s_TOKEN" % (slug or "UPSTREAM")


def _mask(secret: str) -> str:
    text = str(secret or "")
    if not text:
        return ""
    if len(text) <= 10:
        return MASK_SENTINEL * 6
    return "%s…%s" % (text[:6], text[-4:])


def _clean_models(value) -> list:
    if isinstance(value, str):
        raw = re.split(r"[,\n\r;]+", value)
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    out = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_headers(value) -> dict:
    if isinstance(value, str):
        pairs = []
        for line in value.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            pairs.append((key, val))
        value = dict(pairs)
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, val in value.items():
        key = str(key or "").strip()
        if not key:
            continue
        out[key] = str(val if val is not None else "").strip()
    return out


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))


class CustomUpstreamStore:
    """`model_upstreams.json` 的受控写门面：只碰 `channels` 下 `custom:true` 的条目。"""

    def __init__(self, config_path, keys_path=None, *, environ=None, passphrase=None):
        self.config_path = Path(config_path)
        self.keys_path = Path(keys_path or DEFAULT_KEYS_PATH)
        self._environ = environ if environ is not None else os.environ
        self._passphrase = str(passphrase or "")
        self._lock = threading.RLock()

    # ── 密钥封存 ────────────────────────────────────────────
    @property
    def passphrase(self) -> str:
        if self._passphrase:
            return self._passphrase
        return VendorCreds().passphrase

    def _read_keys(self) -> dict:
        try:
            doc = json.loads(self.keys_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": 1, "keys": {}}
        if not isinstance(doc, dict):
            return {"schema": 1, "keys": {}}
        doc.setdefault("keys", {})
        return doc

    def _write_keys(self, doc: dict) -> None:
        _atomic_write(self.keys_path, doc)

    def _plain_key(self, name: str) -> str:
        rec = (self._read_keys().get("keys") or {}).get(str(name or ""))
        blob = (rec or {}).get("blob") if isinstance(rec, dict) else None
        if not isinstance(blob, dict):
            return ""
        try:
            plain = unseal(blob, self.passphrase)
        except Exception:                       # noqa: BLE001 密钥文件丢失不等于配置全废
            return ""
        return str((plain or {}).get("api_key") or "")

    def _store_key(self, name: str, api_key: str) -> None:
        with self._lock:
            doc = self._read_keys()
            doc["keys"][name] = {
                "updated_at": int(time.time()),
                "mask": _mask(api_key),
                "blob": seal({"api_key": api_key}, self.passphrase),
            }
            self._write_keys(doc)

    def _drop_key(self, name: str) -> bool:
        with self._lock:
            doc = self._read_keys()
            if name not in (doc.get("keys") or {}):
                return False
            doc["keys"].pop(name, None)
            self._write_keys(doc)
            return True

    def load_into_environ(self) -> int:
        """启动时把已封存密钥注入进程环境，供路由表按 token_env 读取。"""
        count = 0
        for name in list((self._read_keys().get("keys") or {})):
            plain = self._plain_key(name)
            if not plain:
                continue
            self._environ[env_name_for(name)] = plain
            count += 1
        return count

    # ── 配置读写 ────────────────────────────────────────────
    def _read_config(self) -> dict:
        try:
            doc = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        if not isinstance(doc.get("channels"), dict):
            doc["channels"] = {}
        return doc

    def _write_config(self, doc: dict) -> None:
        _atomic_write(self.config_path, doc)

    def _row(self, name: str) -> dict:
        return (self._read_config()["channels"].get(CHANNEL_PREFIX + name) or {})

    # ── 对外 ────────────────────────────────────────────────
    def list_channels(self) -> list:
        """全部在线接入渠道（掩码口径，绝不出密钥明文）。"""
        doc = self._read_config()
        keys_doc = self._read_keys().get("keys") or {}
        rows = []
        for channel, entry in doc["channels"].items():
            if not isinstance(entry, dict) or not entry.get("custom"):
                continue
            name = str(channel)
            if name.startswith(CHANNEL_PREFIX):
                name = name[len(CHANNEL_PREFIX):]
            headers = _clean_headers(entry.get("headers"))
            rec = keys_doc.get(name) or {}
            token_env = str(entry.get("token_env") or "")
            has_key = bool((token_env and self._environ.get(token_env))
                           or rec.get("mask") or rec.get("blob"))
            rows.append({
                "name": name,
                "channel": channel,
                "label": str(entry.get("note") or name),
                "note": str(entry.get("note") or ""),
                "base_url": str(entry.get("base_url") or ""),
                "endpoint": str(entry.get("endpoint") or "/v1/chat/completions"),
                "models": list(entry.get("models") or []),
                "headers": {key: MASK_SENTINEL * 6 for key in headers},
                "header_names": sorted(headers),
                "has_key": has_key,
                "key_masked": str(rec.get("mask") or ""),
                "updated_at": int(rec.get("updated_at") or 0),
                "custom": True,
            })
        rows.sort(key=lambda row: row["name"])
        return rows

    def payload(self) -> dict:
        rows = self.list_channels()
        return {
            "ok": True,
            "data": rows,
            "items": rows,
            "count": len(rows),
            "generated_at": int(time.time()),
            "source": "local-custom-upstreams",
        }

    def probe_auth(self, name: str, api_key: str = "", headers=None):
        """探测用凭证：密钥留空 / 头是掩码时回退到已落盘真值。

        只在内存里给探针用，返回值绝不进日志、不回前端。
        """
        name = str(name or "").strip().lower()
        if name.startswith(CHANNEL_PREFIX):
            name = name[len(CHANNEL_PREFIX):]
        saved = self._read_config()["channels"].get(name) or {}
        old_headers = _clean_headers((saved or {}).get("headers"))
        token = (str(api_key or "").strip() or self._plain_key(name)
                 or str(self._environ.get(env_name_for(name)) or ""))
        cleaned = _clean_headers(headers)
        for key, value in list(cleaned.items()):
            text = str(value or "")
            # 编辑态回显的是 ●●●●●●：还原成真值，别把掩码发到上游去。
            if text and set(text) <= {MASK_SENTINEL}:
                cleaned[key] = old_headers.get(key, "")
        cleaned = {k: v for k, v in cleaned.items()
                   if v and k.lower() not in RESERVED_HEADERS}
        return token, cleaned

    def save(self, payload: dict) -> dict:
        """新增或覆盖一个自定义渠道；`api_key` 留空 = 沿用旧密钥。"""
        if not isinstance(payload, dict):
            raise CustomUpstreamError("请求体必须是 JSON 对象")
        name = str(payload.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise CustomUpstreamError(
                "渠道标识只允许小写字母/数字/下划线/连字符，长度 1-32")
        base_url = str(payload.get("base_url") or "").strip().rstrip("/")
        if not base_url.lower().startswith(("http://", "https://")):
            raise CustomUpstreamError("上游地址必须以 http:// 或 https:// 开头")
        if any(ch.isspace() for ch in base_url):
            raise CustomUpstreamError("上游地址不可包含空白")
        models = _clean_models(payload.get("models"))
        if not models:
            raise CustomUpstreamError("至少填写一个模型 ID")
        headers = _clean_headers(payload.get("headers"))
        for key in list(headers):
            if key.lower() in RESERVED_HEADERS:
                raise CustomUpstreamError("出站头 %s 由网关托管，不可自定义" % key)
        note_value = payload.get("note")
        api_key = str(payload.get("api_key") or "").strip()

        channel = name
        with self._lock:
            doc = self._read_config()
            old = doc["channels"].get(channel)
            old = copy.deepcopy(old) if isinstance(old, dict) else {}
            if old and not old.get("custom") and not payload.get("overwrite"):
                # 内置渠道（open-design / ollama 等）绝不因同名表单被覆盖。
                raise CustomUpstreamError("渠道 %s 已被内置渠道占用，换一个标识" % channel)
            # 旧版可能用 custom:name 落过盘：迁移到不带前缀的键，避免两处分叉。
            legacy = doc["channels"].pop(CHANNEL_PREFIX + name, None)
            if not old and isinstance(legacy, dict):
                if not legacy.get("custom") and not payload.get("overwrite"):
                    raise CustomUpstreamError("渠道 %s 已被内置渠道占用，换一个标识" % channel)
                old = copy.deepcopy(legacy)
            # 表单没带 note = 保持原备注；绝不因为「没填」把已有备注清空。
            note = str((old.get("note") or "") if note_value is None
                       else note_value).strip()[:120]
            old_headers = _clean_headers(old.get("headers"))
            # 前端回显的是掩码：未改动时保留原值，避免把 ●●●●●● 写进真实配置。
            for hkey, hval in headers.items():
                if hval and set(hval) == {MASK_SENTINEL}:
                    headers[hkey] = old_headers.get(hkey, "")
            headers = {k: v for k, v in headers.items() if v or k in old_headers}

            env_name = env_name_for(name)
            merged = {
                "base_url": base_url,
                "token_env": env_name,
                "pricing_channel": str(old.get("pricing_channel") or channel),
                "endpoint": str(old.get("endpoint") or "/v1/chat/completions"),
                "custom": True,
                "note": note,
                "models": models,
                "headers": headers,
            }
            # 探测回来的模型元数据（上下文/输出上限/视觉）落进 catalog，
            # 否则新接渠道在模型目录里一律显示 0 CTX。只收表单里确实勾选了的模型。
            catalog = payload.get("catalog")
            if isinstance(catalog, list) and catalog:
                rows = []
                for item in catalog[:600]:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or "").strip()
                    if not mid or mid not in models:
                        continue
                    row = {"id": mid, "name": mid, "owned_by": channel}
                    for field in ("context", "max_out"):
                        value = item.get(field)
                        if isinstance(value, (int, float)) and value > 0:
                            row[field] = int(value)
                    if isinstance(item.get("vision"), bool):
                        row["vision"] = item["vision"]
                    rows.append(row)
                if rows:
                    merged["catalog"] = rows
            for key, value in old.items():          # 保留 aliases/catalog 等手写扩展
                merged.setdefault(key, value)
            doc["channels"][channel] = merged
            self._write_config(doc)
            if api_key:
                self._store_key(name, api_key)
                self._environ[env_name] = api_key
            else:
                # 表单留空 = 沿用旧密钥；绝不用空串盖掉 secrets.json 注入的环境变量。
                plain = self._plain_key(name)
                if plain:
                    self._environ[env_name] = plain
                elif not self._environ.get(env_name):
                    self._environ.pop(env_name, None)

        return {"ok": True, "saved": name, "channel": channel,
                "models": len(models), "headers": len(headers),
                "has_key": bool(self._environ.get(env_name)),
                "config": str(self.config_path)}

    def delete(self, name: str) -> dict:
        name = str(name or "").strip().lower()
        if name.startswith(CHANNEL_PREFIX):
            name = name[len(CHANNEL_PREFIX):]
        channel = name
        with self._lock:
            doc = self._read_config()
            existed = doc["channels"].get(channel)
            if existed is None:
                existed = doc["channels"].get(CHANNEL_PREFIX + name)
            if existed is None:
                raise CustomUpstreamError("渠道不存在: %s" % channel)
            if isinstance(existed, dict) and not existed.get("custom"):
                # 内置渠道只读：面板只允许删除自己接入的渠道。
                raise CustomUpstreamError("只允许删除通过面板接入的自定义渠道: %s" % channel)
            doc["channels"].pop(channel, None)
            doc["channels"].pop(CHANNEL_PREFIX + name, None)
            self._write_config(doc)
            self._drop_key(name)
            self._environ.pop(env_name_for(name), None)
        return {"ok": True, "deleted": channel, "changed": 1,
                "config": str(self.config_path)}


__all__ = [
    "CHANNEL_PREFIX",
    "CustomUpstreamError",
    "CustomUpstreamStore",
    "DEFAULT_KEYS_PATH",
    "env_name_for",
]
