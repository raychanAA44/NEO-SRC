#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_kwsa_lite.py - KWSA lite 一键启动（默认 8798）。

启动前复用 HUB NEO 的 secrets 注入链：读取 ``运维区/configs/upstream.json``
的 ``token_source`` 与 ``model_upstreams.json`` 的 ``token_env``，把
``HUB_MODEL_TOKEN`` / ``HUB_OLLAMA_TOKEN`` 等变量注入进程。仓库内不落明文。

渠道密钥的优先级：
1. 进程环境变量（``token_env``）；
2. ``运维区/data/kwsa_lite_creds.json`` 加密密钥仓（面板「收录渠道」写入）。

用法：
    python 运维区/scripts/start_kwsa_lite.py
    python 运维区/scripts/start_kwsa_lite.py --set-password
    python 运维区/scripts/start_kwsa_lite.py --create-client-key lan-console --key-rps 20 --key-burst 40
    python 运维区/scripts/start_kwsa_lite.py --port 8798 --host 0.0.0.0
    python 运维区/scripts/start_kwsa_lite.py --config 运维区/configs/kwsa_lite.json

默认只绑 127.0.0.1；绑定内网地址前必须已有面板密码。非回环 + 关闭认证会拒绝启动。
内网推荐保持应用回环，只暴露 运维区/scripts/kwsa_lite_tls_proxy.py 的 HTTPS 8443。
数据面 /v1/* 默认强制客户端 Key；无活动 Key 时回 503，不会退化成开放。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "编码区" / "src"
SCRIPTS = ROOT / "运维区" / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_neo_secrets() -> None:
    """复用 NEO 的启动注入，不复制密钥、不打印密钥。"""
    try:
        from start_gateway import load_upstream_config
    except Exception as exc:  # noqa: BLE001 缺脚本不拦启动
        print("[KWSA lite] NEO secrets 注入器不可用: %s" % exc, file=sys.stderr)
        return
    try:
        load_upstream_config()
    except Exception as exc:  # noqa: BLE001 缺 secrets 不拦启动
        print("[KWSA lite] NEO secrets 注入失败: %s" % exc, file=sys.stderr)


_load_neo_secrets()

from kwsa_lite.app import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
