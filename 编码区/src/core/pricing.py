#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pricing.py —— 渠道化模型价目表（单一事实源）。

设计依据（旧 HUB 仓的既有格式，不在 HUB NEO 另造一套）：

  examples/custom_prices.json
  examples/model-pricing-reference.md §3.3

标准形状::

    {
      "schema": 1,
      "unit": "USD_per_mtok",
      "usd_cny": 6.72,
      "_meta": {"updated_at": "...", "updated_by": "..."},
      "upstreams": {
        "<渠道>": {
          "billing_kind": "usage" | "subscription" | null,
          "discount": 1.0,
          "peak_rule": {"kind": "utc_weekday_window",
                         "start_hour": 12, "end_hour": 18},
          "models": {
            "<模型>": {
              "in": 0.44, "cache": 0.014, "out": 1.32,
              "off_in": 0.22, "off_cache": 0.007, "off_out": 0.66,
              "source": "...", "source_url": "...", "checked_at": "..."
            }
          }
        }
      }
    }

口径：

* ``in/cache/out`` 是**峰价**，单位由 ``unit`` 指定；旧仓标准单位是
  ``USD_per_mtok``，按 ``usd_cny`` 折成人民币入账。
* ``off_*`` 是谷价；没有峰谷规则或当前不在峰窗时使用，缺省回落到峰价。
* ``discount`` 是渠道折扣（0 < d <= 1）；价目保持牌价，折扣单独存，
  绝不把折后价写回单价表，避免二次折扣。
* 无价目就返回 None / 空，不补 0——这是账本“未计价”的唯一来源。

兼容：旧的 ``{"models": {"<model>": {"in", "cache", "out"}}}``（人民币）
仍可读；但新录入统一写上面的渠道格式。文件 mtime 失效，改完免重启。
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import threading
import time
from pathlib import Path

from core.paths import APP_ROOT, CONFIG_DIR

ROOT = APP_ROOT
DEFAULT_PATH = CONFIG_DIR / "pricing.json"
DEFAULT_USD_CNY = 6.72
UNITS = ("USD_per_mtok", "CNY_per_mtok")
GLOBAL_CHANNEL = "__default__"
AUDIT_CAP = 200
BACKUP_KEEP = 3
# 行内审计字段：来源、降级口径与人工复核时间原样透出，不参与计价。
AUDIT_ROW_FIELDS = (
    "source", "source_url", "source_api", "source_doc", "checked_at",
    "price_note", "cache_note", "out_note", "cache_source", "out_source",
    "billing_basis",
)

_LOCK = threading.RLock()
_CACHE = {}
_CACHE_LOCK = threading.Lock()


class PricingError(ValueError):
    """价目表结构/数值不合法；调用方应返回 400，不得半写入。"""


def _empty_doc() -> dict:
    return {"schema": 1, "unit": "USD_per_mtok",
            "usd_cny": DEFAULT_USD_CNY, "_meta": {}, "upstreams": {}}


def _num(value, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise PricingError("%s 必须是数字，不是布尔值" % field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PricingError("%s 必须是数字" % field)
    if not math.isfinite(number) or number < 0:
        raise PricingError("%s 必须是非负有限数字" % field)
    if positive and number <= 0:
        raise PricingError("%s 必须大于 0" % field)
    return number


def _iso(ts=None) -> str:
    """价目表的 `updated_at` 时间戳：CST，与面板其余时标一致。"""
    from core import clock          # 延迟导入：pricing 被多个入口最先加载，避免循环
    return clock.iso_t(ts)


def _channel_key(name: str) -> str:
    value = str(name or "").strip()
    return value[7:] if value.startswith("custom:") else value


def _channel_matches(stored: str, wanted: str) -> bool:
    if not wanted:
        return True
    a, b = _channel_key(stored), _channel_key(wanted)
    return a == b or (not b and a == GLOBAL_CHANNEL)


def _norm_row(row: dict, where: str) -> dict:
    if not isinstance(row, dict):
        raise PricingError("%s 必须是对象" % where)
    out = dict(row)
    for key in ("in", "cache", "out"):
        if key not in row:
            raise PricingError("%s 缺 %s（in/cache/out 三档必须齐全）" % (where, key))
        out[key] = _num(row[key], "%s.%s" % (where, key))
    for key in ("off_in", "off_cache", "off_out"):
        if key in row and row[key] is not None:
            out[key] = _num(row[key], "%s.%s" % (where, key))
    return out


def _norm_peak(rule, where: str):
    if rule in (None, {}, ""):
        return None
    if not isinstance(rule, dict):
        raise PricingError("%s.peak_rule 必须是对象或 null" % where)
    kind = str(rule.get("kind") or "").strip()
    if kind not in ("utc_weekday_window", "utc_weekday_windows"):
        raise PricingError("%s.peak_rule.kind 不支持: %s" % (where, kind or "空"))
    if kind == "utc_weekday_window":
        start = int(rule.get("start_hour", 12))
        end = int(rule.get("end_hour", 18))
        if not (0 <= start < end <= 24):
            raise PricingError("%s.peak_rule 时间窗必须满足 0 <= start < end <= 24" % where)
        return {"kind": kind, "start_hour": start, "end_hour": end}
    windows = []
    for pair in rule.get("windows") or []:
        try:
            start, end = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise PricingError("%s.peak_rule.windows 每项必须是 [start, end]" % where)
        if not (0 <= start < end <= 24):
            raise PricingError("%s.peak_rule.windows 时间窗非法" % where)
        windows.append([start, end])
    if not windows:
        raise PricingError("%s.peak_rule.windows 不能为空" % where)
    return {"kind": kind, "windows": windows}


def _norm_block(name: str, block: dict) -> dict:
    if not isinstance(block, dict):
        raise PricingError("upstreams.%s 必须是对象" % name)
    clean = {key: copy.deepcopy(value) for key, value in block.items()
             if key != "models"}
    models = block.get("models")
    if not isinstance(models, dict):
        raise PricingError("upstreams.%s.models 必须是对象" % name)
    clean["models"] = {}
    for model, row in models.items():
        mid = str(model or "").strip()
        if not mid:
            raise PricingError("upstreams.%s.models 的模型名不可为空" % name)
        clean["models"][mid] = _norm_row(row, "upstreams.%s.models.%s" % (name, mid))
    if "discount" in clean and clean["discount"] is not None:
        discount = _num(clean["discount"], "upstreams.%s.discount" % name)
        if not 0 < discount <= 1:
            raise PricingError("upstreams.%s.discount 必须在 (0, 1] 内" % name)
        clean["discount"] = discount
    if "peak_rule" in clean:
        clean["peak_rule"] = _norm_peak(clean["peak_rule"], "upstreams.%s" % name)
    if "billing_kind" in clean and clean["billing_kind"] not in (None, "usage", "subscription"):
        raise PricingError("upstreams.%s.billing_kind 只能是 usage/subscription/null" % name)
    return clean


def canonical_doc(doc: dict) -> dict:
    """把标准/旧版/行式录入统一成标准 doc；不合法直接抛 PricingError。"""
    if not isinstance(doc, dict):
        raise PricingError("价目表必须是 JSON 对象")
    if "upstreams" in doc:
        return _from_upstreams(doc)
    if "rows" in doc:
        return _from_rows(doc)
    if "models" in doc:
        return _from_legacy(doc)
    raise PricingError("缺少 upstreams / rows / models 之一")


def _from_upstreams(doc: dict) -> dict:
    unit = str(doc.get("unit") or "USD_per_mtok").strip()
    if unit not in UNITS:
        raise PricingError("unit 只能是 %s" % " / ".join(UNITS))
    fx = _num(doc.get("usd_cny", DEFAULT_USD_CNY), "usd_cny", positive=True)
    upstreams = doc.get("upstreams")
    if not isinstance(upstreams, dict):
        raise PricingError("upstreams 必须是对象")
    out = {
        "schema": int(doc.get("schema") or 1),
        "unit": unit,
        "usd_cny": fx,
        "_meta": copy.deepcopy(doc.get("_meta") or {}),
        "upstreams": {},
    }
    for name, block in upstreams.items():
        # `custom:foo` 与 `foo` 是同一个渠道：落盘统一去前缀，避免账本里
        # channel="custom:foo" 查不到 upstreams["custom:foo"] 而变成未计价。
        key = _channel_key(name)
        if not key:
            raise PricingError("upstreams 的渠道名不可为空")
        if key in out["upstreams"]:
            raise PricingError("渠道重复（custom: 前缀视为同一渠道）: %s" % key)
        out["upstreams"][key] = _norm_block(key, block)
    return out


def _from_legacy(doc: dict) -> dict:
    """旧平表：人民币、无渠道；包成 __default__，读侧仍按同一个价。"""
    unit = str(doc.get("unit") or "CNY_per_mtok").strip()
    if unit not in UNITS:
        raise PricingError("unit 只能是 %s" % " / ".join(UNITS))
    fx = _num(doc.get("usd_cny", DEFAULT_USD_CNY), "usd_cny", positive=True)
    models = doc.get("models")
    if not isinstance(models, dict):
        raise PricingError("models 必须是对象")
    name = str(doc.get("channel") or GLOBAL_CHANNEL)
    meta = copy.deepcopy(doc.get("_meta") or {})
    if doc.get("_note"):
        meta.setdefault("note", doc["_note"])
    if doc.get("_source"):
        meta.setdefault("source", copy.deepcopy(doc["_source"]))
    return _from_upstreams({
        "schema": int(doc.get("schema") or 1),
        "unit": unit,
        "usd_cny": fx,
        "_meta": meta,
        "upstreams": {name: {"models": models}},
    })


def _from_rows(doc: dict) -> dict:
    """行式录入：{"channel": "...", "rows": [{"model","in","cache","out"}]}。

    没有顶层 channel 时，允许每行自带 channel；这样一次 POST 可以录多个渠道。
    """
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows:
        raise PricingError("rows 必须是非空数组")
    default_channel = str(doc.get("channel") or "").strip()
    grouped = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PricingError("rows[%d] 必须是对象" % index)
        model = str(row.get("model") or row.get("id") or "").strip()
        if not model:
            raise PricingError("rows[%d].model 不可为空" % index)
        channel = str(row.get("channel") or default_channel).strip()
        if not channel:
            raise PricingError("rows[%d] 缺 channel（或在外层给 channel）" % index)
        clean = {key: value for key, value in row.items()
                 if key not in ("model", "id", "channel")}
        bucket = grouped.setdefault(channel, {})
        if model in bucket:
            raise PricingError("同一渠道模型重复: %s / %s" % (channel, model))
        bucket[model] = _norm_row(clean, "rows[%d]" % index)
    upstreams = {channel: {"models": models} for channel, models in grouped.items()}
    meta = copy.deepcopy(doc.get("_meta") or {})
    if doc.get("_note"):
        meta.setdefault("note", doc["_note"])
    return _from_upstreams({
        "schema": int(doc.get("schema") or 1),
        "unit": doc.get("unit") or "USD_per_mtok",
        "usd_cny": doc.get("usd_cny", DEFAULT_USD_CNY),
        "_meta": meta,
        "upstreams": upstreams,
    })


def _salvage_doc(raw, error: str) -> tuple:
    """整表校验失败时逐渠道降级：坏渠道剔除、好渠道保留，原因如实写进 error。

    背景：`apply_prices` 曾写入 `billing_kind="payg"`，整表校验当即失败，
    read_doc 退回空表 —— 结果是**全渠道**的金额变 None（计费静默归零）。
    一个渠道的字段写错，不应该让别的渠道也停摆。
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("upstreams"), dict):
        return _empty_doc(), error
    shell = {key: value for key, value in raw.items() if key != "upstreams"}
    kept, dropped = {}, []
    for name, block in raw["upstreams"].items():
        try:
            one = canonical_doc(dict(shell, upstreams={name: block}))
        except Exception as exc:          # noqa: BLE001
            dropped.append("%s(%s)" % (name, exc))
            continue
        kept.update(one["upstreams"])
    if not kept:
        return _empty_doc(), error
    try:
        doc = canonical_doc(dict(shell, upstreams={}))
    except Exception:                     # noqa: BLE001
        return _empty_doc(), error
    doc["upstreams"] = kept
    note = error + ("；已剔除坏渠道 %s" % "、".join(dropped) if dropped else "")
    return doc, note


def read_doc(path=None):
    """读标准 doc → ``(doc, error)``。缺失/坏文件返回空表 + 原因，不抛。"""
    path = Path(path or DEFAULT_PATH)
    try:
        stat = path.stat()
        stamp = stat.st_mtime_ns
    except OSError:
        return _empty_doc(), ""
    with _CACHE_LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == stamp:
            return copy.deepcopy(cached[1]), cached[2]
    raw = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        doc = canonical_doc(raw)
        error = ""
    except Exception as exc:              # noqa: BLE001
        # 一个渠道写错不该把整张表判死：逐渠道降级，能用的照旧能用。
        doc, error = _salvage_doc(raw, "%s: %s" % (type(exc).__name__, exc))
    with _CACHE_LOCK:
        _CACHE[str(path)] = (stamp, copy.deepcopy(doc), error)
    return doc, error


def load_doc(path=None) -> dict:
    return read_doc(path)[0]


def _factor(doc: dict) -> float:
    return 1.0 if doc.get("unit") == "CNY_per_mtok" else float(doc.get("usd_cny") or DEFAULT_USD_CNY)


def _block_models(doc: dict, channel: str):
    wanted = _channel_key(channel)
    upstreams = doc.get("upstreams") or {}
    if wanted:
        block = upstreams.get(wanted)
        if isinstance(block, dict):
            return wanted, block
        # custom: 前缀已剥；大小写不强行猜。
        if wanted.endswith("-free"):
            block = upstreams.get(wanted[:-5])
            if isinstance(block, dict):
                return wanted[:-5], block
        block = upstreams.get(GLOBAL_CHANNEL)
        return (GLOBAL_CHANNEL, block) if isinstance(block, dict) else ("", {})
    block = upstreams.get(GLOBAL_CHANNEL)
    if not isinstance(block, dict) and upstreams:
        first = next(iter(upstreams))
        return first, upstreams.get(first) or {}
    return (GLOBAL_CHANNEL, block) if isinstance(block, dict) else ("", {})


def _find_model(block: dict, model: str):
    models = block.get("models") or {}
    name = str(model or "")
    hit = models.get(name)
    if hit:
        return name, hit
    if "/" in name:
        bare = name.rsplit("/", 1)[-1]
        hit = models.get(bare)
        if hit:
            return bare, hit
    if ":" in name:
        base = name.split(":", 1)[0]
        hit = models.get(base)
        if hit:
            return base, hit
    return "", None


def _is_peak(block: dict, ts=None) -> bool:
    rule = block.get("peak_rule")
    if not isinstance(rule, dict):
        return False
    windows = []
    if rule.get("kind") == "utc_weekday_window":
        windows = [(int(rule.get("start_hour", 12)), int(rule.get("end_hour", 18)))]
    elif rule.get("kind") == "utc_weekday_windows":
        windows = [(int(a), int(b)) for a, b in (rule.get("windows") or [])]
    if not windows:
        return False
    lt = time.gmtime(time.time() if ts is None else ts)
    weekday = (lt.tm_wday + 1) % 7  # 周日=0，周六=6
    if weekday in (0, 6):
        return False
    return any(start <= lt.tm_hour < end for start, end in windows)


def rate_of(doc: dict, channel: str, model: str, ts=None):
    """取某渠道模型的人民币有效单价；无价 → None。"""
    name, block = _block_models(doc, channel)
    if not block:
        return None
    model_key, row = _find_model(block, model)
    if not row:
        return None
    peak = _is_peak(block, ts)

    def pick(key):
        if not peak:
            value = row.get("off_" + key)
            if value is not None:
                return float(value)
        return float(row.get(key) or 0.0)

    factor = _factor(doc)
    discount = block.get("discount", 1.0)
    try:
        discount = float(discount)
        if not 0 < discount <= 1:
            discount = 1.0
    except (TypeError, ValueError):
        discount = 1.0
    factor *= discount
    return {
        "channel": name,
        "model": model_key,
        "in": round(pick("in") * factor, 12),
        "cache": round(pick("cache") * factor, 12),
        "out": round(pick("out") * factor, 12),
        "currency": "CNY_per_mtok",
        "peak": bool(peak),
        "discount": discount,
        "source": row.get("source") or "",
        "checked_at": row.get("checked_at") or "",
        "cache_source": row.get("cache_source") or "",
        "out_source": row.get("out_source") or "",
        "billing_basis": row.get("billing_basis") or "",
        "raw_unit": doc.get("unit") or "USD_per_mtok",
        "raw": {key: row.get(key) for key in
                ("in", "cache", "out", "off_in", "off_cache", "off_out")
                if row.get(key) is not None},
    }


def flat_rates(path=None, channel: str = "") -> dict:
    """给 UI/旧账本用的平表：模型 → 人民币三档牌价（渠道可选）。

    这里刻意**不按当前峰谷切换**：目录价是稳定展示值；真正记账由
    `rate_of(ts=...)` 按峰谷取价。渠道折扣计入牌价（实付）。
    """
    doc, _ = read_doc(path)
    out = {}
    factor = _factor(doc)
    blocks = list((doc.get("upstreams") or {}).items())
    if channel:
        matched = [(name, block) for name, block in blocks
                   if _channel_matches(name, channel)]
        if not matched:
            # 旧平表/全局兜底：只在没有同名渠道块时启用。
            matched = [(name, block) for name, block in blocks if name == GLOBAL_CHANNEL]
        blocks = matched
    for name, block in blocks:
        discount = block.get("discount", 1.0)
        try:
            discount = float(discount)
            if not 0 < discount <= 1:
                discount = 1.0
        except (TypeError, ValueError):
            discount = 1.0
        for model, row in (block.get("models") or {}).items():
            out[model] = {
                "in": round(float(row.get("in") or 0) * factor * discount, 12),
                "cache": round(float(row.get("cache") or 0) * factor * discount, 12),
                "out": round(float(row.get("out") or 0) * factor * discount, 12),
                "channel": name,
                "source": row.get("source") or "",
                "checked_at": row.get("checked_at") or "",
                "cache_source": row.get("cache_source") or "",
            }
    return out


def cost_parts(doc: dict, channel: str, model: str,
               prompt: int = 0, cached: int = 0, completion: int = 0,
               ts=None):
    """人民币三档实付；无价返回 None。"""
    rate = rate_of(doc, channel, model, ts=ts)
    if not rate:
        return None
    billed_in = max(0, int(prompt or 0) - int(cached or 0))
    cached = max(0, int(cached or 0))
    completion = max(0, int(completion or 0))
    return {
        "in": rate["in"] * billed_in / 1_000_000,
        "cache": rate["cache"] * cached / 1_000_000,
        "out": rate["out"] * completion / 1_000_000,
        "rate": rate,
    }


def rows(path=None, channel: str = "") -> list:
    """API/导入预览用的逐行展开（保留原单位与折后人民币价）。"""
    doc, _ = read_doc(path)
    out = []
    factor = _factor(doc)
    for name, block in (doc.get("upstreams") or {}).items():
        if channel and not _channel_matches(name, channel):
            continue
        discount = block.get("discount", 1.0)
        try:
            discount = float(discount)
            if not 0 < discount <= 1:
                discount = 1.0
        except (TypeError, ValueError):
            discount = 1.0
        for model, row in (block.get("models") or {}).items():
            item = {"channel": name, "model": model,
                    "unit": doc.get("unit"), "usd_cny": doc.get("usd_cny"),
                    "discount": discount,
                    "in": row.get("in"), "cache": row.get("cache"), "out": row.get("out")}
            for key in ("off_in", "off_cache", "off_out") + AUDIT_ROW_FIELDS:
                if row.get(key) is not None:
                    item[key] = row.get(key)
            item["cny_in"] = round(float(row.get("in") or 0) * factor * discount, 12)
            item["cny_cache"] = round(float(row.get("cache") or 0) * factor * discount, 12)
            item["cny_out"] = round(float(row.get("out") or 0) * factor * discount, 12)
            # 旧 UI 的 custom 价目行读的是 `in_cny`；同值时给别名，避免价格格空成“—”。
            item["in_cny"] = item["cny_in"]
            item["cache_cny"] = item["cny_cache"]
            item["out_cny"] = item["cny_out"]
            out.append(item)
    return out


def summarize_doc(doc: dict, error: str = "") -> dict:
    """对内存中的 doc 出摘要；dry-run 用它展示「写入后会是什么样」。"""
    models = 0
    fallback_rows = 0
    for block in (doc.get("upstreams") or {}).values():
        for row in (block.get("models") or {}).values():
            models += 1
            if row.get("cache_source") or row.get("out_source"):
                fallback_rows += 1
    meta = doc.get("_meta") if isinstance(doc.get("_meta"), dict) else {}
    return {
        "schema": doc.get("schema"),
        "unit": doc.get("unit"),
        "usd_cny": doc.get("usd_cny"),
        "channels": list((doc.get("upstreams") or {}).keys()),
        "models": models,
        "fallback_rows": fallback_rows,
        "pricing_policy": copy.deepcopy(meta.get("pricing_policy") or {}),
        "updated_at": meta.get("updated_at", ""),
        "updated_by": meta.get("updated_by", ""),
        "error": error,
    }


def summary(path=None) -> dict:
    doc, error = read_doc(path)
    return summarize_doc(doc, error)


def validate_doc(doc: dict) -> list:
    try:
        canonical_doc(doc)
        return []
    except PricingError as exc:
        return [str(exc)]


def template() -> dict:
    return {
        "schema": 1,
        "unit": "USD_per_mtok",
        "usd_cny": DEFAULT_USD_CNY,
        "_meta": {"note": "渠道化模型价目表；in/cache/out 为官方牌价，discount 为渠道折扣；"
                           "改完调用 POST /api/pricing/import 或直接覆盖本文件。"},
        "upstreams": {
            "open-design": {
                "billing_kind": "usage",
                "discount": 1.0,
                "peak_rule": None,
                "models": {
                    "example-model": {
                        "in": 0.44,
                        "cache": 0.014,
                        "out": 1.32,
                        "source": "官方价目页",
                        "source_url": "https://example.com/pricing",
                        "checked_at": "2026-10-24",
                    }
                },
            }
        },
    }


def _diff(before: dict, after: dict) -> dict:
    added, updated, removed, unchanged = [], [], [], 0
    before_models = {}
    after_models = {}
    for name, block in (before.get("upstreams") or {}).items():
        for model, row in (block.get("models") or {}).items():
            before_models["%s/%s" % (name, model)] = row
    for name, block in (after.get("upstreams") or {}).items():
        for model, row in (block.get("models") or {}).items():
            after_models["%s/%s" % (name, model)] = row
    for key, row in after_models.items():
        old = before_models.get(key)
        if old is None:
            added.append(key)
        elif any(old.get(k) != row.get(k) for k in ("in", "cache", "out",
                                                     "off_in", "off_cache", "off_out")):
            updated.append(key)
        else:
            unchanged += 1
    for key in before_models:
        if key not in after_models:
            removed.append(key)
    return {"added": added, "updated": updated, "removed": removed,
            "unchanged": unchanged,
            "added_count": len(added), "updated_count": len(updated),
            "removed_count": len(removed)}


def _merge_docs(current: dict, incoming: dict, keep_fx: bool) -> dict:
    doc = copy.deepcopy(current)
    if not keep_fx:
        doc["usd_cny"] = incoming["usd_cny"]
    doc["unit"] = incoming.get("unit") or doc.get("unit") or "USD_per_mtok"
    doc.setdefault("_meta", {})
    doc["_meta"].update(copy.deepcopy(incoming.get("_meta") or {}))
    for name, block in (incoming.get("upstreams") or {}).items():
        target = doc.setdefault("upstreams", {}).setdefault(name, {"models": {}})
        for key, value in block.items():
            if key == "models":
                target.setdefault("models", {}).update(copy.deepcopy(value))
            else:
                target[key] = copy.deepcopy(value)
    return doc


def _backup(path: Path):
    if not path.exists():
        return ""
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = path.with_name("%s.bak-%s" % (path.name, stamp))
    try:
        shutil.copy2(str(path), str(backup))
    except OSError:
        return ""
    old = sorted(path.parent.glob("%s.bak-*" % path.name),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    for item in old[BACKUP_KEEP:]:
        try:
            item.unlink()
        except OSError:
            pass
    return str(backup)


def _write(path: Path, doc: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _audit(path: Path, entry: dict):
    audit = path.with_name(path.stem + ".audit.json")
    try:
        rows = json.loads(audit.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            rows = []
    except Exception:  # noqa: BLE001
        rows = []
    rows.append(entry)
    del rows[:-AUDIT_CAP]
    try:
        tmp = audit.with_name(audit.name + ".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(audit))
    except OSError:
        pass


def import_payload(payload: dict, path=None, *, mode: str = "merge",
                   dry_run: bool = False, actor: str = "") -> dict:
    """校验并原子写入价目表；返回差异与写入后的摘要。

    支持三种输入：
      1. 标准 doc（``upstreams``）
      2. 行式 ``{"channel": ..., "rows": [...]}``
      3. 旧平表 ``{"models": {...}}``（只读兼容，导入时按 CNY 处理）
    """
    target = Path(path or DEFAULT_PATH)
    if not isinstance(payload, dict):
        raise PricingError("请求体必须是 JSON 对象")
    mode = str(mode or "merge").lower()
    if mode not in ("merge", "replace"):
        raise PricingError("mode 只能是 merge 或 replace")
    raw = payload.get("doc") if isinstance(payload.get("doc"), dict) else payload
    current, current_error = read_doc(target)
    if current_error and mode == "merge":
        raise PricingError("现有价目表不可解析，先修复或改用 mode=replace: %s" % current_error)
    fx_given = isinstance(raw, dict) and "usd_cny" in raw
    # 没写 unit/usd_cny 的增量，沿用现有值，避免把人民币表误当美元表。
    if mode == "merge" and isinstance(raw, dict):
        raw = copy.deepcopy(raw)
        if "unit" not in raw:
            raw["unit"] = current.get("unit") or "USD_per_mtok"
        if not fx_given:
            raw["usd_cny"] = current.get("usd_cny") or DEFAULT_USD_CNY
    incoming = canonical_doc(raw)
    if mode == "replace":
        candidate = incoming
    else:
        candidate = _merge_docs(current, incoming, keep_fx=not fx_given)
    candidate = canonical_doc(candidate)
    candidate.setdefault("_meta", {})
    candidate["_meta"]["updated_at"] = _iso()
    candidate["_meta"]["updated_by"] = str(actor or "local")
    diff = _diff(current, candidate)
    try:
        shown_path = str(target.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        shown_path = str(target)
    result = {
        "ok": True,
        "path": shown_path,
        "mode": mode,
        "dry_run": bool(dry_run),
        "summary": summarize_doc(candidate),
        "diff": diff,
    }
    if dry_run:
        return result
    _backup(target)
    _write(target, candidate)
    _audit(target, {"at": int(time.time()), "iso": _iso(), "actor": str(actor or "local"),
                    "action": "pricing.import", "mode": mode,
                    "added": diff["added_count"], "updated": diff["updated_count"],
                    "removed": diff["removed_count"], "unchanged": diff["unchanged"],
                    "channels": list((candidate.get("upstreams") or {}).keys())})
    result["summary"] = summary(target)
    return result


def append_row(path=None, channel: str = "", model: str = "",
               in_price=None, cache=None, out_price=None, actor: str = "") -> dict:
    """单行便捷录入（CLI/脚本用）；等价于 rows 导入 merge。"""
    if not channel or not model:
        raise PricingError("channel 与 model 均不可为空")
    return import_payload({"channel": channel, "rows": [{
        "model": model, "in": in_price, "cache": cache, "out": out_price,
    }]}, path=path, mode="merge", dry_run=False, actor=actor)
