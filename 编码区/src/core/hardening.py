#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/hardening.py —— 安全加固响应头注入。

务实安全加固：
1. X-Frame-Options: DENY（防点击劫持）
2. X-Content-Type-Options: nosniff（禁 MIME 嗅探）
3. Referrer-Policy: no-referrer（限 Referrer 泄露）
4. Strict-Transport-Security: max-age=31536000; includeSubDomains（防协议降级）
5. Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()（禁用未用硬件与敏感权限）
6. Content-Security-Policy: default-src 'self' ...（与单屏重机视觉前端相容）
"""

from __future__ import annotations

import os

CSP_DIRECTIVE = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Content-Security-Policy", CSP_DIRECTIVE),
)


def inject_security_headers(handler, is_api: bool = False) -> None:
    """就地给 BaseHTTPRequestHandler 注入 6 大安全头。
    检查 _headers_buffer 避免重复发送同一 Header。
    """
    buf = getattr(handler, "_headers_buffer", None)
    existing = set()
    if buf:
        for item in buf:
            try:
                line = item.decode("latin-1", "ignore")
                if ":" in line:
                    existing.add(line.split(":", 1)[0].strip().lower())
            except Exception:
                pass

    for key, val in SECURITY_HEADERS:
        if key.lower() not in existing:
            handler.send_header(key, val)
            existing.add(key.lower())

    if is_api:
        if "cache-control" not in existing:
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, private")
        if "pragma" not in existing:
            handler.send_header("Pragma", "no-cache")
