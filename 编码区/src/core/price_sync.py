"""price_sync.py —— 官方价目同步：只认上游自己公布的价目端点。

三条纪律：
1. **只用公开价目端点**，不打 /v1/models 猜价，也不按「输出=输入×N」推算；
2. 端点不可达 / 没有该模型 = 如实返回 missing，绝不补 0、绝不写假价；
3. 落盘只动目标渠道的 models 块，其他渠道与 usd_cny 原样保留，每行带 source/source_url/checked_at。

OpenDesign 口径（已验证 2026-10-26）：
    GET {origin}/api/v1/public/model-prices
    {"data": [{"model": "deepseek-v4-flash",
               "pricing": {"unit": "1m_tokens", "currency": "USD",
                           "input": 0.44, "output": 1.32, "cacheRead": 0.014}}]}
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# 上游公开价目端点（按顺序尝试），目前只有 OpenDesign 口径。
PRICE_PATHS = ("/api/v1/public/model-prices",)
# 接入域名与价目 API 域名不一致的上游：按已知映射补一个候选 origin，
# 不去猜其余主机名（猜错会把请求打到无关服务上）。
PRICE_HOST_MAP = {"models.example.invalid": "amr-api.open-design.ai"}
USER_AGENT = "HUB-NEO-price-sync/1.0"
SOURCE_NOTE = ("上游公开价目端点直出；单位 USD/百万 token，与本地价目表口径一致。"
               "缓存价取 cacheRead（读缓存价），cacheWrite 不计费故不落字段。")

_PLACEHOLDER_KEYS = ("usd_cny", "unit", "upstreams")


def origin_of(base_url: str) -> str:
    """`https://host/v1` → `https://host`；解析失败返回空串。"""
    parts = urlsplit(str(base_url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return "%s://%s" % (parts.scheme, parts.netloc)


def _number(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num >= 0 and num == num else None


def parse_payload(doc) -> dict:
    """官方响应 → `{unit, currency, rows, skipped}`；没有价目字段的行进 skipped。"""
    rows_in = doc.get("data") if isinstance(doc, dict) else doc
    rows, skipped = [], []
    for item in rows_in if isinstance(rows_in, list) else []:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or item.get("id") or "").strip()
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else item
        price_in = _number(pricing.get("input"))
        price_out = _number(pricing.get("output"))
        price_cache = _number(pricing.get("cacheRead"))
        if not model or price_in is None or price_out is None:
            if model:
                skipped.append(model)
            continue
        row = {"model": model, "in": price_in, "out": price_out}
        # 官方没公布缓存价 = 不打折（缺字段时价目表按输入价计缓存）。
        if price_cache is not None:
            row["cache"] = price_cache
        if pricing.get("cacheWrite") is not None:
            row["cache_write"] = _number(pricing.get("cacheWrite"))
        if isinstance(pricing.get("longContext"), dict):
            row["long_context"] = True
        rows.append(row)
    return {
        "unit": str((doc.get("unit") if isinstance(doc, dict) else "") or "USD_per_mtok"),
        "currency": str((rows_in[0].get("pricing", {}).get("currency")
                         if rows and isinstance(rows_in, list) and isinstance(rows_in[0], dict)
                         and isinstance(rows_in[0].get("pricing"), dict) else "") or "USD"),
        "rows": rows,
        "skipped": skipped,
    }


def fetch_official(base_url: str, timeout: float = 10.0, opener=None) -> dict:
    """按 base_url 的 origin 探公开价目端点。

    返回 `{ok, source_url, rows, skipped, unit, error}`；失败时 `ok=False` 且 `rows=[]`，
    调用方据此如实告警，不得回退到任何推算值。
    """
    origin = origin_of(base_url)
    if not origin:
        return {"ok": False, "source_url": "", "rows": [], "skipped": [],
                "unit": "", "error": "上游地址必须以 http:// 或 https:// 开头"}
    send = opener or urllib.request.urlopen
    last_error = ""
    hosts = [origin]
    mapped_host = PRICE_HOST_MAP.get(urlsplit(origin).netloc.lower())
    if mapped_host:
        hosts.append("%s://%s" % (urlsplit(origin).scheme, mapped_host))
    candidates = [host + path for host in hosts for path in PRICE_PATHS]
    for url in candidates:
        try:
            # 官方端点会拦 python-urllib 默认 UA（403），必须自带可识别 UA。
            req = urllib.request.Request(url, headers={
                "accept": "application/json",
                "user-agent": USER_AGENT,
            })
            with send(req, timeout=timeout) as res:
                status = getattr(res, "status", None) or 200
                if int(status) != 200:
                    last_error = "HTTP %s" % status
                    continue
                doc = json.loads(res.read(4 * 1024 * 1024).decode("utf-8", "replace"))
        except Exception as exc:               # noqa: BLE001 探测失败如实上报，不掐连接
            last_error = str(exc) or exc.__class__.__name__
            continue
        parsed = parse_payload(doc)
        if not parsed["rows"]:
            last_error = "价目端点返回 %d 行但无可解析价目" % len(parsed["skipped"])
            continue
        parsed.update({"ok": True, "source_url": url, "error": ""})
        return parsed
    return {"ok": False, "source_url": candidates[0] if candidates else "",
            "rows": [], "skipped": [], "unit": "",
            "error": last_error or "价目端点不可用"}


def apply_prices(path, channel: str, rows: list, source_url: str,
                 checked_at: str = None, source_label: str = None) -> dict:
    """把官方价目写进 `upstreams[channel].models`，原子落盘。

    只增改官方列出的模型；官方没列的旧行原样保留（可能是人工维护的），
    返回值分开报 written / kept，便于页面如实显示覆盖了多少。
    """
    channel = str(channel or "").strip()
    if not channel:
        raise ValueError("渠道标识必填")
    target = Path(path)
    doc = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(doc, dict):
        raise ValueError("价目表不是 JSON 对象")
    upstreams = doc.setdefault("upstreams", {})
    block = upstreams.get(channel)
    if not isinstance(block, dict):
        block = {}
        upstreams[channel] = block
    models = block.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        block["models"] = models

    stamp = checked_at or datetime.date.today().isoformat()
    written = 0
    for row in rows:
        model = str(row.get("model") or "").strip()
        if not model or row.get("in") is None or row.get("out") is None:
            continue
        entry = {
            "source": source_label or ("上游公开价目端点 %s" % source_url),
            "source_url": source_url,
            "checked_at": stamp,
            "price_note": SOURCE_NOTE,
            "in": float(row["in"]),
            "out": float(row["out"]),
        }
        # 三档必须齐全（价目表校验会拒绝缺 cache 的行，整渠道会被剔除）：
        # 官方没公布缓存价时就按输入价计，并标明 cache=in 降级，不编数字。
        if row.get("cache") is not None:
            entry["cache"] = float(row["cache"])
        else:
            entry["cache"] = float(row["in"])
            entry["cache_source"] = "in"
            entry["cache_note"] = "官方未公布缓存价，按输入价计（cache=in 降级）"
        # 谷价（Ollama / OpenCode 的 Off-Peak 行）：有就落，没有不补
        for key in ("off_in", "off_cache", "off_out", "cache_write"):
            if row.get(key) is not None:
                entry[key] = float(row[key])
        if row.get("long_context"):
            entry["price_note"] += "　该模型官方另设长上下文阶梯价，本表按基础档计价。"
        existed = isinstance(models.get(model), dict)
        models[model] = entry
        written += 1
    kept = [name for name in models if name not in
            {str(r.get("model") or "") for r in rows}]
    # billing_kind 只接受 usage / subscription / null：写成别的值会让整张价目表
    # 校验失败（read_doc 退回空表），结果是全渠道都变成「价目未配置」。
    if block.get("billing_kind") not in ("usage", "subscription"):
        block["billing_kind"] = "usage"
    block.setdefault("discount", 1.0)

    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pricing-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "channel": channel, "written": written,
            "kept": kept, "path": str(target), "checked_at": stamp}


def official_block(base_url: str, channel: str, path, timeout: float = 10.0,
                   opener=None) -> dict:
    """探测 + 落盘的组合入口（面板一键同步用）。"""
    found = fetch_official(base_url, timeout=timeout, opener=opener)
    if not found["ok"]:
        return {"ok": False, "source_url": found["source_url"], "error": found["error"],
                "rows": [], "count": 0}
    applied = apply_prices(path, channel, found["rows"], found["source_url"])
    return {"ok": True, "source_url": found["source_url"], "error": "",
            "rows": found["rows"], "count": len(found["rows"]),
            "applied": applied}


# ── 登录态抓取（OpenDesign 官方账单 unitPrices）────────────────────
#
# 公开价目端点只看得到「目录价」；实际结算价在登录态后的账单项里：
#   GET {api}/api/v1/workspaces/current                      → workspaceId
#   GET {api}/api/v1/workspaces/{ws}/billing/usage?limit&from&cursor
#       Cookie: __Secure-better-auth.session_token=<session>
#       x-vela-workspace-id: <ws>
#   entries[].model + entries[].unitPrices{input,output,cacheRead,cacheWrite}
# Bearer API Key 打这些端点只会得到 401，必须是会话 Cookie（浏览器登录态）。
OD_API = "https://amr-api.open-design.ai"
OD_ORIGIN = "https://open-design.ai"
SESSION_COOKIE = "__Secure-better-auth.session_token"
ACCOUNT_PAGE_LIMIT = 100
ACCOUNT_MAX_PAGES = 5
ACCOUNT_WINDOW_DAYS = 30


def _session_value(raw: str) -> str:
    """从一段「裸值 / `name=value` / 整串 cookie」里取出会话值。

    面板里用户很可能直接把浏览器里那行 cookie 贴进来，所以两种形态都要认：
    其余 cookie（非 better-auth）不猜，返回空串。
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    for part in raw.replace("\n", ";").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip() == SESSION_COOKIE and value.strip():
            return value.strip()
    if "=" not in raw and len(raw) > 16:
        return raw
    return ""


def session_from_creds(creds: dict) -> str:
    """从凭据仓条目里找会话串。

    依次看 `cookie` / `cookies` / `session`；`token` / `access_token` 也认，
    但 `sk-` 开头的 API Key 不算会话（它对官方账单端点只会 401）。
    """
    creds = creds if isinstance(creds, dict) else {}
    for key in ("cookie", "cookies", "session", "token", "access_token"):
        raw = str(creds.get(key) or "").strip()
        if not raw or raw.lower().startswith("sk-"):
            continue
        value = _session_value(raw)
        if value:
            return value
    return ""


def _cookie_value(set_cookies) -> str:
    """从 Set-Cookie 头列表里取 better-auth 会话值（含非 Secure 变体）。"""
    names = (SESSION_COOKIE, "better-auth.session_token")
    for header in set_cookies or []:
        part = str(header or "").split(";", 1)[0]
        name, sep, value = part.partition("=")
        if sep and name.strip() in names and value.strip():
            return value.strip()
    return ""


def _post_json(url: str, payload: dict, timeout: float, opener=None, referer: str = ""):
    """→ `(status, doc, set_cookies, error)`；HTTPError 也当数据读，不抛。"""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "origin": origin_of(url) or OD_ORIGIN,
        "user-agent": USER_AGENT,
    }
    if referer:
        headers["referer"] = referer
    send = opener or urllib.request.urlopen
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with send(req, timeout=timeout) as res:
            raw = res.read(2 * 1024 * 1024)
            cookies = res.headers.get_all("Set-Cookie") or []
            doc = None
            try:
                doc = json.loads(raw.decode("utf-8", "replace")) if raw else None
            except ValueError:
                doc = None
            return int(getattr(res, "status", None) or 200), doc, cookies, ""
    except urllib.error.HTTPError as exc:
        cookies = (exc.headers.get_all("Set-Cookie") if exc.headers else None) or []
        doc = None
        try:
            raw = exc.read() or b""
            doc = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except (OSError, ValueError):
            doc = None
        return int(exc.code or 0), doc, cookies, str(exc)
    except Exception as exc:                 # noqa: BLE001
        return 0, None, [], str(exc) or exc.__class__.__name__


# better-auth 的邮箱登录端点；按顺序试，404 才换下一个，401/403 立即判定凭据错。
LOGIN_PATHS = ("/api/auth/sign-in/email", "/api/auth/sign-in/username")


def login_session(base_url: str, email: str, password: str, timeout: float = 15.0,
                  opener=None) -> dict:
    """用账号密码换登录态会话（better-auth `sign-in/email` → Set-Cookie）。

    只认真响应里的会话 cookie：没有 Set-Cookie 就是失败，绝不伪造、绝不用 API Key 冒充。
    `code` 取值：`no_credentials` / `auth_error`（账号密码不对或被拦）/ `no_endpoint` /
    `no_cookie`（端点通了但没给会话）/ `rate_limited` / `network`。
    """
    out = {"ok": False, "session": "", "code": "", "error": "", "endpoint": ""}
    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        out.update(code="no_credentials", error="登录需要账号与密码")
        return out
    origins = []
    first = origin_of(base_url) if base_url else ""
    mapped = ""
    if first:
        parts = urlsplit(first)
        api_host = PRICE_HOST_MAP.get(parts.netloc)
        if api_host:
            mapped = "%s://%s" % (parts.scheme, api_host)
    for cand in (first, mapped, OD_ORIGIN):
        if cand and cand not in origins:
            origins.append(cand)
    last = ("no_endpoint", "没有可用的登录端点")
    for origin in origins:
        for path in LOGIN_PATHS:
            url = origin + path
            payload = {"email": email, "password": password}
            if path.endswith("username"):
                payload = {"username": email, "password": password}
            status, doc, cookies, err = _post_json(
                url, payload, timeout, opener, referer=origin + "/cloud/sign-in")
            token = _cookie_value(cookies)
            if token:
                out.update(ok=True, session=token, code="", error="", endpoint=url)
                return out
            vendor_code = str((doc or {}).get("code") or "") if isinstance(doc, dict) else ""
            if vendor_code == "TURNSTILE_REQUIRED":
                # 官方登录带人机校验：纯接口账密登录必被拦，只能走浏览器会话。
                out.update(code="turnstile_required", endpoint=url,
                           error="官方登录要求人机校验（Turnstile），接口登录拿不到会话："
                                 "请在浏览器登录后在「官方登录」粘会话 cookie，或关掉浏览器用本机会话探针")
                return out
            if status in (401, 403):
                out.update(code="auth_error", endpoint=url,
                           error="账号或密码不对，或被风控拦住（HTTP %s）" % status)
                return out
            if status == 429:
                out.update(code="rate_limited", endpoint=url,
                           error="登录端点限流（HTTP 429），稍后再试")
                return out
            if status == 0:
                out.update(code="network", endpoint=url, error="登录端点不可达：%s" % (err or ""))
                return out
            if status in (200, 201, 204):
                last = ("no_cookie", "%s 返回 HTTP %s 但没有下发会话 cookie" % (url, status))
            else:
                last = ("no_endpoint", "%s 返回 HTTP %s" % (url, status))
    out.update(code=last[0], error=last[1])
    return out


def _account_headers(session: str, workspace: str = "") -> dict:
    headers = {
        "Cookie": "%s=%s" % (SESSION_COOKIE, session),
        "Origin": OD_ORIGIN,
        "Referer": OD_ORIGIN + "/cloud/usage-records",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }
    if workspace:
        headers["x-vela-workspace-id"] = workspace
    return headers


def _get_json(url: str, headers: dict, timeout: float, opener=None) -> tuple:
    """→ (status, doc, error)。不抛：调用方要把失败原因原样上报。"""
    send = opener or urllib.request.urlopen
    try:
        with send(urllib.request.Request(url, headers=headers), timeout=timeout) as res:
            status = int(getattr(res, "status", None) or 200)
            return status, json.loads(res.read(4 * 1024 * 1024).decode("utf-8", "replace")), ""
    except Exception as exc:                 # noqa: BLE001
        return 0, None, str(exc) or exc.__class__.__name__


def fetch_account_prices(session: str, workspace: str = "", timeout: float = 20.0,
                         opener=None, days: int = ACCOUNT_WINDOW_DAYS,
                         pages: int = 3) -> dict:
    """从登录态账单里取逐模型 unitPrices（最近 days 天，最多 pages 页）。

    同一模型多条记录取**最新一条**（价可能调整过）；没有 `unitPrices` 的条目不进表。
    失败时 `ok=False` 且给出 `code`（`no_session` / `auth_error` / `no_workspace` /
    `no_entries` / `http_<n>`），**绝不回退到公开端点冒充登录态结果**。
    """
    out = {"ok": False, "code": "", "source_url": "", "ws": "", "rows": [],
           "skipped": [], "error": "", "scanned": 0}
    session = str(session or "").strip()
    if not session:
        out.update(code="no_session", error="没有 OpenDesign 登录态（缺会话 cookie）")
        return out
    ws = str(workspace or "").strip()
    if not ws:
        status, doc, err = _get_json(OD_API + "/api/v1/workspaces/current",
                                     _account_headers(session), timeout, opener)
        if status == 401:
            out.update(code="auth_error", error="登录态已失效（401），重新登录后再同步")
            return out
        if status != 200 or not isinstance(doc, dict):
            out.update(code="no_workspace",
                       error="workspaces/current 未给 workspaceId：HTTP %s %s" % (status, err or ""))
            return out
        ws = str(doc.get("workspaceId") or doc.get("id") or "").strip()
        if not ws:
            out.update(code="no_workspace", error="workspaces/current 响应里没有 workspaceId")
            return out
    out["ws"] = ws
    base = "%s/api/v1/workspaces/%s/billing/usage" % (OD_API, urllib.parse.quote(ws))
    since = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - days * 86400))
    latest: dict = {}
    cursor = ""
    pages = max(1, min(int(pages or 1), ACCOUNT_MAX_PAGES))
    for _ in range(pages):
        query = {"limit": str(ACCOUNT_PAGE_LIMIT), "from": since}
        if cursor:
            query["cursor"] = cursor
        url = base + "?" + urllib.parse.urlencode(query)
        status, doc, err = _get_json(url, _account_headers(session, ws), timeout, opener)
        if status == 401:
            out.update(code="auth_error", error="登录态已失效（401），重新登录后再同步")
            return out
        if status != 200 or not isinstance(doc, dict):
            out.update(code="http_%s" % status,
                       error="billing/usage 取数失败：HTTP %s %s" % (status, err or ""))
            return out
        entries = doc.get("entries") if isinstance(doc.get("entries"), list) else []
        out["scanned"] += len(entries)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or "").strip()
            prices = entry.get("unitPrices") if isinstance(entry.get("unitPrices"), dict) else {}
            if not model or not prices:
                if model:
                    out["skipped"].append(model)
                continue
            stamp = str(entry.get("createdAt") or "")
            row = {"model": model, "in": _number(prices.get("input")),
                   "out": _number(prices.get("output")),
                   "cache": _number(prices.get("cacheRead")),
                   "cache_write": _number(prices.get("cacheWrite")),
                   "at": stamp}
            if row["in"] is None or row["out"] is None:
                continue
            kept = latest.get(model)
            if kept is None or stamp >= str(kept.get("at") or ""):
                latest[model] = row
        cursor = str(doc.get("nextCursor") or "")
        if not cursor or not doc.get("hasMore"):
            break
    out["source_url"] = base + "?limit=%d&from=%s" % (ACCOUNT_PAGE_LIMIT, since)
    if not latest:
        out.update(code="no_entries",
                   error="登录态可读但窗口内 %d 条记录都没有 unitPrices" % out["scanned"])
        return out
    for row in latest.values():
        row.pop("at", None)
    out.update(ok=True, code="", error="",
               rows=sorted(latest.values(), key=lambda r: r["model"]), source="account")
    return out


def preview(path) -> dict:
    """只读：价目表来源覆盖情况（多少行带 provenance，多少行没有）。"""
    target = Path(path)
    if not target.exists():
        return {"ok": False, "error": "价目表不存在", "channels": {}}
    doc = json.loads(target.read_text(encoding="utf-8"))
    out = {}
    for name, block in (doc.get("upstreams") or {}).items():
        models = (block or {}).get("models") or {}
        sourced = [m for m, row in models.items()
                   if isinstance(row, dict) and row.get("source_url")]
        out[name] = {"models": len(models), "sourced": len(sourced),
                     "unsourced": sorted(set(models) - set(sourced))}
    return {"ok": True, "channels": copy.deepcopy(out)}
