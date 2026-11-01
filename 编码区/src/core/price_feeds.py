#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""core/price_feeds.py —— 各上游**官方价目**抓取器（HUB NEO 自有实现，零外部依赖）。

来源与形状（2026-10 实测）：

| 上游 | 来源 | 形状 |
|---|---|---|
| Ollama | `https://ollama.com/pricing` | `<table>`：Model / Input / Cached input / Output，另有 `(Off-Peak)` 行 |
| OpenCode | `https://opencode.ai/v2/docs/console/go/` | 两张 37 行表（Go / Go Plus）+「显示名 → model id」映射表 |
| OpenDesign | `https://amr-api.open-design.ai/api/v1/public/model-prices` | 免登录 JSON：`{model, pricing:{input,output,cacheRead,cacheWrite}}` |
| Token Harbor | `{base}/v1/models` | JSON：`input_usd_per_1m` / `output_usd_per_1m` / `cache_read_usd_per_1m` |

纪律：
1. 只解析**公开页面 / 免登录端点**，不携带任何用户凭据；
2. 只用标准库（HTML 表格用正则切 `<tr>/<td>`，不引 bs4）；
3. 解析不到就**如实报错**，不拿上次的值冒充这次；
4. 每个 feed 返回 `{model: {in, out, cache?, off_in?, off_cache?, off_out?}}`，
   **本模块不写价目表**，落盘统一走 `price_sync.apply_prices()`；
5. `-` / 空表示该档不存在，**不写 0**（0 会被下游判成免费）。

口径：
- OpenCode 的 `Cached Read` 就是缓存价；`Cached Write` 本仓无此字段，不落；
- Ollama 的 `(Off-Peak)` 行是同一模型的谷价，落 `off_*` 三档；
- OpenDesign 的 `cacheRead` 对应 `cache`。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OLLAMA_PRICING_URL = "https://ollama.com/pricing"
OPENCODE_DOC_URL = "https://opencode.ai/v2/docs/console/go/"
OPENDESIGN_PRICES_URL = "https://amr-api.open-design.ai/api/v1/public/model-prices"
TOKENHARBOR_PRICES_PATH = "/v1/models"


def _fetch(url: str, timeout: int = 30) -> tuple:
    """→ `(body_text, err)`；err 非空即失败，绝不 silence。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:
        return "", "HTTP %s" % exc.code
    except Exception as exc:                    # noqa: BLE001  网络异常如实上报
        return "", "%s: %s" % (type(exc).__name__, str(exc)[:100])


def _num(value):
    """`'$0.30'` → 0.3；`'-'` / 空 / 非数 → None（**不当 0**）。"""
    if value is None:
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text or text in ("-", "—", "N/A", "n/a"):
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return num if num == num else None


def _strip_tags(text: str) -> str:
    """去标签并解 HTML 实体（不解 `&gt;` 的话模型名对不上价目表键）。"""
    out = re.sub(r"<[^>]+>", " ", text)
    for src, dst in (("&gt;", ">"), ("&lt;", "<"), ("&amp;", "&"), ("&quot;", '"'),
                     ("&#39;", "'"), ("&nbsp;", " ")):
        out = out.replace(src, dst)
    return re.sub(r"\s+", " ", out).strip()


def _tables(html: str) -> list:
    """切出所有 `<table>` → `[[单元格文本...], ...]`。"""
    out = []
    for table in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.S):
        rows = []
        for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
            cells = [_strip_tags(c) for c in
                     re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row.group(1), re.S)]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


# ── Ollama ────────────────────────────────────────────────────────────────
_OFF = re.compile(r"\((?i:off[- ]?peak)\)\s*$")


def parse_ollama(html: str) -> dict:
    """`ollama.com/pricing` 页面 → `{model: {in, cache, out, off_*?}}`。"""
    models = {}
    for table in _tables(html):
        head = [cell.lower() for cell in table[0]]
        if not ("model" in head and any("input" in cell for cell in head)):
            continue

        def col(*keys):
            for index, name in enumerate(head):
                if all(key in name for key in keys):
                    return index
            return None

        ci = col("input")
        cc = col("cached", "input")
        if cc is None:
            cc = col("cache")
        co = col("output")
        if ci is None or co is None:
            continue
        for row in table[1:]:
            if len(row) <= max(ci, co):
                continue
            name = row[0].strip()
            if not name or name.lower() == "model":
                continue
            off = bool(_OFF.search(name))
            mid = _OFF.sub("", name).strip()
            val_in = _num(row[ci])
            val_out = _num(row[co])
            val_cache = _num(row[cc]) if (cc is not None and cc < len(row)) else None
            if off:
                pairs = (("off_in", val_in), ("off_cache", val_cache), ("off_out", val_out))
            else:
                pairs = (("in", val_in), ("cache", val_cache), ("out", val_out))
            picked = {key: val for key, val in pairs if val is not None}
            if not picked:
                continue                      # 整行都是 `-`：不进表，也不留空条目
            models.setdefault(mid, {}).update(picked)
    models = {mid: price for mid, price in models.items() if price}
    return {"ok": bool(models), "models": models, "src": OLLAMA_PRICING_URL,
            "error": "" if models else "页面里没解析出价目表"}


def fetch_ollama() -> dict:
    html, err = _fetch(OLLAMA_PRICING_URL)
    if err:
        return {"ok": False, "error": err, "models": {}}
    return parse_ollama(html)


# ── OpenCode ──────────────────────────────────────────────────────────────
def _norm_name(name: str) -> str:
    """显示名归一化成可比较键：`GLM-5.3-Flash` → `glm53flash`。"""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


TIER_RE = r"\(\s*(?:[≤＜]|&lt;|<|(?:[＞>]|&gt;))\s*[\d.]+\s*[KkMm]?\s*tokens?\s*\)"


def parse_opencode(html: str) -> dict:
    """Go 文档页 → `{model_id: {in, out, cache}}`（取 Go 档，Plus 差异如实上报）。

    第一列是**显示名**（`GLM-5.3-Flash`），必须先用映射表换成调用 ID；
    直接拿第 0 列当 ID 会把 `$0.15` 这种价格当模型名。
    """
    tables = _tables(html)
    alias = {}
    for table in tables:
        head = [cell.lower() for cell in table[0]]
        if "model" not in head or not any("model id" in cell for cell in head):
            continue
        name_i = head.index("model")
        id_i = next((i for i, cell in enumerate(head) if "model id" in cell), None)
        if id_i is None:
            continue
        for row in table[1:]:
            if len(row) <= max(name_i, id_i):
                continue
            display, mid = row[name_i].strip(), row[id_i].strip()
            if display and mid:
                alias[_norm_name(display)] = mid

    def parse_price_table(table):
        head = [cell.lower() for cell in table[0]]
        if not ("model" in head and "input" in head and "output" in head):
            return None
        if not any("cached read" in cell for cell in head):
            return None

        def col(*keys):
            for index, name in enumerate(head):
                if all(key in name for key in keys):
                    return index
            return None

        ci, co, cc = col("input"), col("output"), col("cached", "read")
        got = {}
        for row in table[1:]:
            if len(row) < 3:
                continue
            display = row[0].strip()
            if not display:
                continue
            mid = alias.get(_norm_name(display)) or display
            val_in = _num(row[ci]) if ci is not None and ci < len(row) else None
            val_out = _num(row[co]) if co is not None and co < len(row) else None
            val_cache = _num(row[cc]) if cc is not None and cc < len(row) else None
            if val_in is None and val_out is None:
                continue
            entry = {}
            if val_in is not None:
                entry["in"] = val_in
            if val_out is not None:
                entry["out"] = val_out
            if val_cache is not None:
                entry["cache"] = val_cache
            if entry:
                got[mid] = entry
        return got

    plans = [got for got in (parse_price_table(t) for t in tables) if got]
    if not plans:
        return {"ok": False, "models": {}, "src": OPENCODE_DOC_URL,
                "error": "文档页里没找到价格表（页面结构可能变了）"}
    models = dict(plans[0])
    plus_diff = []
    if len(plans) > 1:
        for mid, price in plans[1].items():
            base = models.get(mid)
            if base is not None and base != price:
                plus_diff.append(mid)

    # 峰谷 / 分档变体折叠：`(Off-Peak)`→off_*，`(Peak)`→基准，`(≤/> xxx tokens)`→取低档
    folded = {}
    for mid, price in list(models.items()):
        if re.search(r"\(\s*off[- ]?peak\s*\)", mid, re.I):
            base = re.sub(r"\(\s*off[- ]?peak\s*\)", "", mid, flags=re.I).strip()
            entry = folded.setdefault(base, {})
            for key, val in price.items():
                entry["off_" + key] = val
            models.pop(mid, None)
        elif re.search(r"\(\s*peak\s*\)", mid, re.I):
            base = re.sub(r"\(\s*peak\s*\)", "", mid, flags=re.I).strip()
            folded.setdefault(base, {}).update(price)
            models.pop(mid, None)
        elif re.search(TIER_RE, mid, re.I):
            base = re.sub(TIER_RE, "", mid, flags=re.I).strip()
            entry = folded.setdefault(base, {})
            low = bool(re.search(r"[≤＜]|&lt;|<", mid))
            if low:
                entry.update(price)
            else:
                for key, val in price.items():
                    entry.setdefault(key, val)
            models.pop(mid, None)
    for base, entry in folded.items():
        if base and entry:
            current = models.setdefault(base, {})
            for key, val in entry.items():
                current.setdefault(key, val)

    return {"ok": True, "models": models, "src": OPENCODE_DOC_URL,
            "plans": len(plans), "aliases": len(alias), "plus_diff": plus_diff,
            "folded": len(folded), "error": ""}


def fetch_opencode() -> dict:
    html, err = _fetch(OPENCODE_DOC_URL)
    if err:
        return {"ok": False, "error": err, "models": {}}
    return parse_opencode(html)


# ── OpenDesign（免登录 JSON）──────────────────────────────────────────────
def parse_opendesign(doc) -> dict:
    primary, long_ctx = {}, {}
    for item in (doc.get("data") if isinstance(doc, dict) else doc) or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("model") or item.get("id") or "").strip()
        price = item.get("pricing") or {}
        if not mid or not isinstance(price, dict):
            continue
        val_in = _num(price.get("input"))
        val_out = _num(price.get("output"))
        val_cache = _num(price.get("cacheRead"))
        entry = {}
        if val_in is not None:
            entry["in"] = val_in
        if val_out is not None:
            entry["out"] = val_out
        if val_cache is not None:
            entry["cache"] = val_cache
        if val_cache is None and _num(price.get("cacheWrite")) is not None:
            entry["cache_write"] = _num(price.get("cacheWrite"))
        if price.get("longContext"):
            entry["long_context"] = True
        if entry:
            primary[mid] = entry
        long_price = price.get("longContext") if isinstance(price.get("longContext"), dict) else None
        if long_price:
            long_ctx[mid] = {"in": _num(long_price.get("input")),
                             "out": _num(long_price.get("output")),
                             "cache": _num(long_price.get("cacheRead"))}
    return {"ok": bool(primary), "models": primary, "long_context": long_ctx,
            "src": OPENDESIGN_PRICES_URL,
            "error": "" if primary else "JSON 里没有价目"}


def fetch_opendesign() -> dict:
    body, err = _fetch(OPENDESIGN_PRICES_URL)
    if err:
        return {"ok": False, "error": err, "models": {}}
    try:
        doc = json.loads(body)
    except ValueError as exc:
        return {"ok": False, "error": "不是 JSON：%s" % str(exc)[:60], "models": {}}
    return parse_opendesign(doc)


# ── Token Harbor（自家 /v1/models 带价目字段）─────────────────────────────
def parse_tokenharbor(doc) -> dict:
    rows = doc.get("data") if isinstance(doc, dict) else doc
    models = {}
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("model") or "").strip()
        price = item.get("pricing") if isinstance(item.get("pricing"), dict) else item
        if not mid:
            continue
        val_in = _num(price.get("input_usd_per_1m") or price.get("input"))
        val_out = _num(price.get("output_usd_per_1m") or price.get("output"))
        val_cache = _num(price.get("cache_read_usd_per_1m") or price.get("cacheRead"))
        entry = {}
        if val_in is not None:
            entry["in"] = val_in
        if val_out is not None:
            entry["out"] = val_out
        if val_cache is not None:
            entry["cache"] = val_cache
        if entry.get("in") is not None and entry.get("out") is not None:
            models[mid] = entry
    return {"ok": bool(models), "models": models, "src": "", "error": ""}


def fetch_tokenharbor(base_url: str, timeout: int = 20) -> dict:
    base = str(base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "models": {}, "src": "",
                "error": "Token Harbor 需要上游地址（{base}/v1/models）"}
    url = base + TOKENHARBOR_PRICES_PATH
    body, err = _fetch(url, timeout=timeout)
    if err:
        return {"ok": False, "models": {}, "src": url, "error": err}
    try:
        doc = json.loads(body)
    except ValueError as exc:
        return {"ok": False, "models": {}, "src": url,
                "error": "不是 JSON：%s" % str(exc)[:60]}
    out = parse_tokenharbor(doc)
    out["src"] = url
    if not out["ok"]:
        out["error"] = "/v1/models 里没有逐模型价目字段"
    return out


# feed 名 → (抓取函数, 说明)。名字与渠道标识对齐，便于价目同步直接取用。
FEEDS = {
    "ollama": (fetch_ollama, "ollama.com/pricing 表格（含 Off-Peak）"),
    "opencode": (fetch_opencode, "OpenCode Go 文档价格表（Cached Read = 缓存价）"),
    "open-design": (fetch_opendesign, "OpenDesign 免登录 JSON 端点"),
}
# 需要上游地址才能抓的 feed
NEEDS_BASE = {"tokenharbor": (fetch_tokenharbor, "Token Harbor /v1/models 价目字段")}


def feeds_for(channel: str) -> list:
    """渠道标识 → 候选 feed 名（含别名）。"""
    name = str(channel or "").strip().lower()
    if not name:
        return []
    if name in FEEDS:
        return [name]
    if name in NEEDS_BASE:
        return [name]
    if name in ("opendesign", "open_design", "amr", "amr-link"):
        return ["open-design"]
    if name in ("ollama-v2", "ollama2"):
        return ["ollama"]
    return []


def fetch_feed(name: str, base_url: str = "", timeout: int = 30) -> dict:
    """跑指定 feed（纯解析入口给测试用 `parse_*`，不走网络）。"""
    name = str(name or "").strip().lower()
    if name in FEEDS:
        return FEEDS[name][0]()
    if name in NEEDS_BASE:
        return NEEDS_BASE[name][0](base_url, timeout=timeout)
    return {"ok": False, "models": {},
            "error": "没有该渠道的官方价目源：%s" % (name or "—")}


def fetch_all(only=None, base_url: str = "") -> dict:
    """跑全部（或指定）feed → `{name: {ok, models, src, error, count, desc}}`。"""
    out = {}
    for name, (_fn, desc) in FEEDS.items():
        if only and name not in only:
            continue
        result = fetch_feed(name)
        result["desc"] = desc
        result["count"] = len(result.get("models") or {})
        out[name] = result
    for name, (_fn, desc) in NEEDS_BASE.items():
        if only and name not in only:
            continue
        result = fetch_feed(name, base_url=base_url)
        result["desc"] = desc
        result["count"] = len(result.get("models") or {})
        out[name] = result
    return out


def rows_of(models: dict) -> list:
    """`{model: {...}}` → `[{model, in, out, cache, off_*}]`（`apply_prices` 的入参形状）。"""
    rows = []
    for model, price in (models or {}).items():
        if not isinstance(price, dict):
            continue
        row = {"model": model}
        for key in ("in", "out", "cache", "cache_write", "off_in", "off_out", "off_cache"):
            if price.get(key) is not None:
                row[key] = float(price[key])
        if price.get("long_context"):
            row["long_context"] = True
        rows.append(row)
    return sorted(rows, key=lambda item: item["model"])


__all__ = ["FEEDS", "NEEDS_BASE", "feeds_for", "fetch_feed", "fetch_all", "rows_of",
           "fetch_ollama", "fetch_opencode", "fetch_opendesign", "fetch_tokenharbor",
           "parse_ollama", "parse_opencode", "parse_opendesign", "parse_tokenharbor",
           "OLLAMA_PRICING_URL", "OPENCODE_DOC_URL", "OPENDESIGN_PRICES_URL"]
