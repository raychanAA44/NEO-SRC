# -*- coding: utf-8 -*-
"""OC 免费档桥适配：直连桥门禁配方与流式收口。

接法以 ``01-OC桥/两座桥-做法与接线-20261026.md`` 为准（2026-10-20 实测）：

- 两种模式
  - ``bridge``：把请求转给已在跑的外部桥（默认 ``http://127.0.0.1:8898/v1``），
    门禁由桥自己满足，lite 不改请求、不碰 session 头。**推荐**，也最不容易接错。
  - ``direct``：lite 自己打 ``opencode.ai``。必须逐条满足门禁，否则 403 FreeTierError：
    1. ``Authorization: Bearer public``（永不带 ``oc_sk_`` / 订阅 key）
    2. ``User-Agent: opencode/<channel>/<version>/<client>``
    3. session 头族四兄弟同值：``x-opencode-session`` / ``x-opencode-session-id`` /
       ``x-session-affinity`` / ``X-Session-Id``，格式 ``ses_`` + 12 hex + 14 base62
    4. ``tools`` 里必须有一个名为 ``read`` 的工具（客户端自带就复用，没有才注入）
    5. 上游必须 ``stream: true``
    6. 不能发 ``tool_choice: "none"``
    7. ``role:"tool"`` 的历史消息不能带 ``name``

注意：免费档按出口 IP 记账，被判定滥用的 IP 会永久 403；``192.0.2.10`` 已确认报废，
所以云端开启 direct 模式前先确认出口是干净 IP。
"""

from __future__ import annotations

import json
import secrets
import string
import time

DEFAULT_UA = "opencode/latest/2.0.32/cli"
DEFAULT_UPSTREAM = "https://opencode.ai/inference/v1"
PUBLIC_TOKEN = "public"
HEX = "0123456789abcdef"
BASE62 = string.ascii_letters + string.digits

READ_ANCHOR = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file from the workspace by path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute file path"}},
            "required": ["path"],
        },
    },
}

SESSION_HEADERS = (
    "x-opencode-session",
    "x-opencode-session-id",
    "x-session-affinity",
    "X-Session-Id",
)


def session_id() -> str:
    """``ses_`` + 12 hex + 14 base62（上游校验格式，不是随便的 uuid）。"""
    return "ses_" + "".join(secrets.choice(HEX) for _ in range(12)) + \
        "".join(secrets.choice(BASE62) for _ in range(14))


def gate_headers(session: str = "", user_agent: str = "") -> dict:
    """直连模式下必须补齐的请求头；``Authorization`` 由调用方统一写成 Bearer public。"""
    sid = session or session_id()
    ua = (user_agent or DEFAULT_UA).strip() or DEFAULT_UA
    headers = {"User-Agent": ua}
    for name in SESSION_HEADERS:
        headers[name] = sid
    return headers


def _tool_names(tools) -> list:
    names = []
    if isinstance(tools, list):
        for item in tools:
            fn = item.get("function") if isinstance(item, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
    return names


def apply_gate(payload: dict) -> tuple:
    """按门禁改写请求体；返回 ``(payload, gate_reused)``。

    ``gate_reused=True`` 表示客户端自己已经带了 ``read``，桥没有凭空加锚点——
    这时模型调用 ``read`` 是客户端合法行为，不该被判成 tool_leak。
    """
    if not isinstance(payload, dict):
        return payload, False
    payload["stream"] = True
    if str(payload.get("tool_choice") or "").strip().lower() == "none":
        payload.pop("tool_choice", None)
    names = _tool_names(payload.get("tools"))
    reused = "read" in names
    if not reused:
        tools = payload.get("tools")
        payload["tools"] = (list(tools) if isinstance(tools, list) else []) + [READ_ANCHOR]
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "tool":
            message.pop("name", None)
    return payload, reused


def _delta_text(chunk: dict) -> str:
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        piece = delta.get("content")
        if isinstance(piece, str):
            return piece
        if isinstance(piece, list):
            return "".join(p.get("text") or "" for p in piece if isinstance(p, dict))
    return ""


def collect_sse(raw: bytes, model: str = "") -> dict:
    """把上游 SSE 聚合成一个非流式 OpenAI 响应（客户端要 stream=false 时用）。"""
    text, finish, usage, tool_calls, chunk_model, created = [], "", {}, [], model, int(time.time())
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            chunk = json.loads(body)
        except ValueError:
            continue
        chunk_model = chunk.get("model") or chunk_model
        created = chunk.get("created") or created
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = str(choice["finish_reason"])
            piece = _delta_text({"choices": [choice]})
            if piece:
                text.append(piece)
            for call in ((choice.get("delta") or {}).get("tool_calls") or []):
                index = int(call.get("index") or 0)
                while len(tool_calls) <= index:
                    tool_calls.append({"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
                slot = tool_calls[index]
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])
    message = {"role": "assistant", "content": "".join(text)}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish = finish or "tool_calls"
    out = {
        "id": "chatcmpl-oc-" + secrets.token_hex(8),
        "object": "chat.completion",
        "created": int(created or time.time()),
        "model": chunk_model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish or "stop"}],
    }
    if usage:
        out["usage"] = usage
    return out


def describe(config: dict) -> dict:
    """给面板用的桥配置摘要（token 只报是否存在）。"""
    config = config or {}
    mode = str(config.get("mode") or "bridge")
    return {
        "enabled": bool(config.get("enabled")),
        "mode": mode,
        "channel": str(config.get("channel") or "oc-bridge"),
        "bridge_url": str(config.get("bridge_url") or "http://127.0.0.1:8898/v1"),
        "upstream": str(config.get("upstream") or DEFAULT_UPSTREAM),
        "user_agent": str(config.get("user_agent") or DEFAULT_UA),
        "token_present": bool(config.get("token")),
        "models": list(config.get("models") or []),
        "gate": ("外部桥代过" if mode == "bridge" else "lite 自补门禁头"),
        "note": ("桥模式：门禁与 session 由外部直连桥负责；lite 只转请求。"
                 if mode == "bridge" else
                 "直连模式：lite 逐条补门禁头；出口 IP 被烧会永久 403，云端默认不要开。"),
    }


__all__ = ["DEFAULT_UA", "DEFAULT_UPSTREAM", "PUBLIC_TOKEN", "READ_ANCHOR",
           "SESSION_HEADERS", "session_id", "gate_headers", "apply_gate",
           "collect_sse", "describe"]
