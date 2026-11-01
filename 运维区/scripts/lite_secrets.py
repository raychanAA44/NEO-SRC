# -*- coding: utf-8 -*-
"""lite_secrets.py —— KWSA lite 独立版的密钥注入器（不依赖 HUB NEO 主网关）。

独立文件夹原则：轻量版只读自己文件夹内的 `运维区/configs/`，不去 import 主网关的
`start_gateway.py`。注入范围与主网关一致——只把值塞进进程环境变量，不落盘、不打印。

读取顺序：
1. `upstream.json`：上游地址、账本口径、`token_source`（管理令牌）；
2. `model_upstreams.json`：各渠道的 `token_env` / `token_key`，按同一份 secrets 文件取值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 本文件位于 <独立文件夹>/运维区/scripts/ → 上两级就是独立版根目录
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "运维区" / "configs"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                            # 缺文件 / 坏 JSON 都不拦启动
        return {}


def _token_file(config: dict, config_dir: Path) -> dict:
    """按 `token_source.file` 读密钥文件；相对路径以独立版根目录为基准。"""
    source = config.get("token_source") or {}
    raw = str(source.get("file") or "").strip()
    if not raw:
        return {}
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = config_dir.parent.parent / path
    return _read_json(path)


def load_upstream_config(path: Path | None = None) -> None:
    """把配置里的上游与令牌注入进程环境；已有环境变量优先，绝不覆盖。"""
    config_dir = path.parent if path else CONFIG_DIR
    config_path = Path(path) if path else config_dir / "upstream.json"
    config = _read_json(config_path)

    for cfg_key, env_key in (("base_url", "HUB_UPSTREAM"),
                             ("fallback_url", "HUB_UPSTREAM_LOCAL"),
                             ("model_upstream", "HUB_MODEL_UPSTREAM"),
                             ("model_channel", "HUB_MODEL_CHANNEL"),
                             ("ledger_mode", "HUB_LEDGER_MODE")):
        value = str(config.get(cfg_key) or "").strip()
        if value and not os.environ.get(env_key):
            os.environ[env_key] = value

    cloud = str(config.get("base_url") or "").strip()
    if cloud and not os.environ.get("HUB_UPSTREAM_CLOUD"):
        os.environ["HUB_UPSTREAM_CLOUD"] = cloud

    ledger = config.get("ledger") or {}
    for cfg_key, env_key in (("backend", "HUB_LEDGER_BACKEND"),
                             ("host", "HUB_LEDGER_MYSQL_HOST"),
                             ("port", "HUB_LEDGER_MYSQL_PORT"),
                             ("user", "HUB_LEDGER_MYSQL_USER"),
                             ("database", "HUB_LEDGER_MYSQL_DB"),
                             ("table", "HUB_LEDGER_MYSQL_TABLE")):
        value = ledger.get(cfg_key)
        if value not in (None, "") and not os.environ.get(env_key):
            os.environ[env_key] = str(value)

    secrets = _token_file(config, config_dir)
    source = config.get("token_source") or {}
    admin_key = str(source.get("key") or "").strip()
    if admin_key and not os.environ.get("HUB_ADMIN_TOKEN"):
        token = str(secrets.get(admin_key) or "").strip()
        if token:
            os.environ["HUB_ADMIN_TOKEN"] = token

    # 渠道令牌：model_upstreams.json 的每个 channel 各自声明 token_env / token_key。
    upstreams = _read_json(config_dir / "model_upstreams.json")
    for channel in (upstreams.get("channels") or {}).values():
        if not isinstance(channel, dict):
            continue
        env_key = str(channel.get("token_env") or "").strip()
        token_key = str(channel.get("token_key") or "").strip()
        if not env_key or not token_key or os.environ.get(env_key):
            continue
        value = str(secrets.get(token_key) or "").strip()
        if value:
            os.environ[env_key] = value
