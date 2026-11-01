#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把旧 HUB 的渠道价目迁入 HUB NEO，默认只做 dry-run。

旧仓事实源：``examples/custom_prices.json``
目标事实源：``运维区/configs/pricing.json``

边界：
  * 只迁入选定渠道；默认不碰 NEO 已有的 ``open-design``。
  * ``in/cache/out`` 任一档缺失的行不补 0、不猜价，整行跳过并写进报告。
  * ``off_*``、峰窗、折扣、billing_kind、plan_book、来源审计字段原样带入。
  * dry-run 用 ``pricing.import_payload`` 的差异回执，不写文件；``--apply`` 才落盘。
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "编码区" / "src"))

from core import pricing  # noqa: E402

DEFAULT_SOURCE = Path(os.environ.get(
    "HUB_LEGACY_PRICES",
    r"examples/custom_prices.json",
))
DEFAULT_TARGET = ROOT / "运维区" / "configs" / "pricing.json"
DEFAULT_CHANNELS = ("ollama", "ollama-v2", "tokenharbor", "opencode")
REQUIRED = ("in", "cache", "out")
NUMERIC = ("in", "cache", "out", "off_in", "off_cache", "off_out")
AUDIT_FIELDS = (
    "source", "source_url", "source_api", "checked_at", "price_note",
    "cache_note", "peak_rule_note", "display_discount",
    "display_discount_note", "billing_kind", "plan_book", "updated_at",
)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit("不是 JSON 对象: %s" % path)
    return data


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def build_payload(source: dict, source_path: Path,
                  channels: tuple[str, ...]) -> tuple[dict, dict, dict]:
    """返回 (待导入 doc, 跳过明细, 渠道统计)。"""
    upstreams = source.get("upstreams")
    if not isinstance(upstreams, dict):
        raise SystemExit("旧价目缺少 upstreams")
    selected = {}
    skipped = {}
    stats = {}
    for channel in channels:
        block = upstreams.get(channel)
        if not isinstance(block, dict):
            skipped.setdefault("missing_channels", []).append(channel)
            continue
        clean = {key: json.loads(json.dumps(value, ensure_ascii=False))
                 for key, value in block.items() if key != "models"}
        if not clean.get("discount"):
            # 旧仓 tokenharbor/opencode 省略 discount，语义就是不打折。
            clean["discount"] = 1.0
        clean["models"] = {}
        rows = block.get("models") or {}
        partial = []
        for model, row in rows.items():
            if not isinstance(row, dict):
                partial.append({"model": model, "missing": ["<row>"]})
                continue
            missing = [key for key in REQUIRED
                       if key not in row or row.get(key) is None]
            if missing:
                partial.append({
                    "model": model,
                    "missing": missing,
                    "available": {key: row.get(key) for key in NUMERIC
                                  if row.get(key) is not None},
                })
                continue
            clean["models"][model] = json.loads(json.dumps(row, ensure_ascii=False))
        selected[channel] = clean
        stats[channel] = {
            "source_rows": len(rows),
            "imported_rows": len(clean["models"]),
            "skipped_rows": len(partial),
        }
        if partial:
            skipped[channel] = partial

    if not selected:
        raise SystemExit("没有可导入的渠道: %s" % ", ".join(channels))
    payload = {
        "schema": 1,
        "unit": source.get("unit") or "USD_per_mtok",
        "usd_cny": source.get("usd_cny", pricing.DEFAULT_USD_CNY),
        "_meta": {
            "note": "旧 HUB custom_prices.json 迁移；只导入 in/cache/out 三档齐全的行，缺失档位不补 0。",
            "legacy_migration": {
                "source": str(source_path).replace("\\", "/"),
                "source_sha256": digest(source_path),
                "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "channels": list(selected),
                "skipped_rows": skipped,
            },
        },
        "upstreams": selected,
    }
    return payload, skipped, stats


def compare_rows(candidate: dict, source: dict, channels: tuple[str, ...]) -> dict:
    """逐行对照旧仓原始数值与审计字段，确认迁移没有改价。"""
    mismatches = []
    checked = 0
    for channel in channels:
        block = (candidate.get("upstreams") or {}).get(channel) or {}
        src_block = (source.get("upstreams") or {}).get(channel) or {}
        for model, row in (block.get("models") or {}).items():
            src = (src_block.get("models") or {}).get(model)
            if not isinstance(src, dict):
                mismatches.append({"channel": channel, "model": model,
                                   "field": "<row>", "old": None, "new": "missing"})
                continue
            checked += 1
            for key in NUMERIC:
                if src.get(key) is None:
                    continue
                if row.get(key) != src.get(key):
                    mismatches.append({"channel": channel, "model": model,
                                       "field": key, "old": src.get(key),
                                       "new": row.get(key)})
            for key in AUDIT_FIELDS:
                if key not in src:
                    continue
                old_value = src.get(key)
                new_value = row.get(key)
                if isinstance(old_value, (dict, list)):
                    same = json.dumps(old_value, sort_keys=True, ensure_ascii=False) == \
                        json.dumps(new_value, sort_keys=True, ensure_ascii=False)
                else:
                    same = old_value == new_value
                if not same:
                    mismatches.append({"channel": channel, "model": model,
                                       "field": key, "old": old_value,
                                       "new": new_value})
        # 峰谷/折扣/计费口径等块级字段
        for key in ("peak_rule", "billing_kind", "plan_book", "discount",
                    "display_discount", "display_discount_note", "updated_at"):
            if key not in src_block:
                continue
            old_value = src_block.get(key)
            new_value = block.get(key)
            if isinstance(old_value, (dict, list)):
                same = json.dumps(old_value, sort_keys=True, ensure_ascii=False) == \
                    json.dumps(new_value, sort_keys=True, ensure_ascii=False)
            else:
                same = old_value == new_value
            if not same and not (key == "discount" and old_value is None and new_value == 1.0):
                mismatches.append({"channel": channel, "model": "<block>",
                                   "field": key, "old": old_value, "new": new_value})
    return {"checked_rows": checked, "mismatches": mismatches,
            "ok": not mismatches}


def rate_samples(candidate: dict) -> list:
    """用固定 UTC 时刻验证峰/谷取价方向，避免只对 JSON 不对账。"""
    samples = [
        ("ollama", "deepseek-v4.1-flash", (2026, 10, 19, 13, 0, 0), 0.30, "peak"),
        ("ollama", "deepseek-v4.1-flash", (2026, 10, 18, 13, 0, 0), 0.15, "off"),
        ("opencode", "deepseek-v4-pro", (2026, 10, 19, 2, 0, 0), 1.32, "peak"),
        ("opencode", "deepseek-v4-pro", (2026, 10, 19, 5, 0, 0), 0.66, "off"),
        ("tokenharbor", "claude-sonnet-4.6", (2026, 10, 19, 2, 0, 0), 3.0, "flat"),
    ]
    out = []
    fx = float(candidate.get("usd_cny") or pricing.DEFAULT_USD_CNY)
    for channel, model, stamp, usd_in, label in samples:
        block = (candidate.get("upstreams") or {}).get(channel) or {}
        if model not in (block.get("models") or {}):
            out.append({"channel": channel, "model": model, "label": label,
                        "skipped": True})
            continue
        ts = calendar.timegm(stamp)
        actual = pricing.rate_of(candidate, channel, model, ts=ts)
        expected = round(usd_in * fx, 12)
        out.append({
            "channel": channel, "model": model, "label": label,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "expected_cny_in": expected,
            "actual_cny_in": actual["in"] if actual else None,
            "peak": actual["peak"] if actual else None,
            "ok": bool(actual and abs(actual["in"] - expected) < 1e-9),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="旧 HUB custom_prices.json")
    parser.add_argument("--pricing", default=str(DEFAULT_TARGET),
                        help="NEO pricing.json")
    parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS),
                        help="逗号分隔；默认 ollama,ollama-v2,tokenharbor,opencode")
    parser.add_argument("--apply", action="store_true",
                        help="确认 dry-run 后落盘；默认只预览")
    parser.add_argument("--report", default="", help="可选：把 JSON 回执写入文件")
    parser.add_argument("--actor", default="legacy-migration")
    args = parser.parse_args()

    source_path = Path(args.source)
    target_path = Path(args.pricing)
    channels = tuple(item.strip() for item in args.channels.split(",") if item.strip())
    source = read_json(source_path)
    payload, skipped, stats = build_payload(source, source_path, channels)
    candidate = pricing.canonical_doc(payload)
    current = pricing.load_doc(target_path)

    existing = {
        "channels": list((current.get("upstreams") or {}).keys()),
        "selected_overlap": sorted(set(channels) & set((current.get("upstreams") or {}).keys())),
    }
    overlap_rows = []
    for channel in existing["selected_overlap"]:
        old_models = set(((current.get("upstreams") or {}).get(channel) or {}).get("models") or {})
        new_models = set(((candidate.get("upstreams") or {}).get(channel) or {}).get("models") or {})
        overlap_rows.append({"channel": channel,
                             "overlap": len(old_models & new_models),
                             "new_only": len(new_models - old_models)})
    existing["overlap_rows"] = overlap_rows

    dry = pricing.import_payload(payload, path=target_path, mode="merge",
                                 dry_run=True, actor=args.actor)
    parity = compare_rows(candidate, source, channels)
    samples = rate_samples(candidate)
    ok = parity["ok"] and all(item.get("ok") for item in samples if not item.get("skipped"))
    report = {
        "ok": ok,
        "dry_run": not args.apply,
        "source": {
            "path": str(source_path).replace("\\", "/"),
            "sha256": digest(source_path),
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.localtime(source_path.stat().st_mtime)),
        },
        "target": str(target_path).replace("\\", "/"),
        "channels": list(channels),
        "stats": stats,
        "skipped": skipped,
        "existing": existing,
        "import": dry,
        "candidate_summary": pricing.summarize_doc(candidate),
        "parity": parity,
        "rate_samples": samples,
    }
    if args.apply and ok:
        applied = pricing.import_payload(payload, path=target_path, mode="merge",
                                         dry_run=False, actor=args.actor)
        after = pricing.load_doc(target_path)
        after_parity = compare_rows(after, source, channels)
        idempotent = pricing.import_payload(payload, path=target_path, mode="merge",
                                            dry_run=True, actor=args.actor)
        report.update({
            "dry_run": False,
            "applied": applied,
            "after_summary": pricing.summarize_doc(after),
            "after_parity": after_parity,
            "idempotent": {
                "added": idempotent["diff"]["added_count"],
                "updated": idempotent["diff"]["updated_count"],
                "removed": idempotent["diff"]["removed_count"],
                "unchanged": idempotent["diff"]["unchanged"],
            },
            "ok": ok and after_parity["ok"],
        })
    elif args.apply:
        report["applied"] = None
        report["error"] = "dry-run parity failed; refused to write"

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
