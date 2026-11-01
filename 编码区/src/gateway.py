#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gateway.py - HUB NEO 核心反代与契约校验模块
"""

import uuid
from typing import Dict, Optional, Tuple

REQUIRED_FORWARD_HEADERS = [
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-real-ip",
    "host"
]

FORBIDDEN_PORTS = {8792}

def validate_proxy_headers(headers: Dict[str, str]) -> Tuple[bool, list]:
    """验证反向代理请求头是否满足规范"""
    lowered = {k.lower(): v for k, v in headers.items()}
    missing = [h for h in REQUIRED_FORWARD_HEADERS if h not in lowered or not lowered[h]]
    return (len(missing) == 0, missing)

def inject_trace_context(headers: Dict[str, str]) -> Dict[str, str]:
    """确保透传 traceparent 或自动注入 X-Request-Id"""
    out = dict(headers)
    lowered = {k.lower(): v for k, v in headers.items()}
    if "traceparent" not in lowered and "x-request-id" not in lowered:
        req_id = str(uuid.uuid4())
        out["X-Request-Id"] = req_id
    return out

def is_port_allowed(port: int) -> bool:
    """检验端口是否安全且未落入禁区"""
    return port not in FORBIDDEN_PORTS and 1024 <= port <= 65535
