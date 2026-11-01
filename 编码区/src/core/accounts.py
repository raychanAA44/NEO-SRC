#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/accounts.py —— 极简账号池与单向只读签到引擎。

恪守三大纪律：
1. 只保留「签到」+ 预留「原文导入接口」，绝不引入常驻自动续期、挑槽与对冲。
2. 绝对防互踩铁律：本地签到遇到 token 失效应返回 NEED_REFRESH，
   严禁自动发起 refresh_token 换新请求，避免把云端 KWSA 账号挤下线。
3. 严格成功判据：业务码非 0 严禁记录 last_checkin。
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from core import clock, vendor_creds

# 账号凭据落库格式：与保险仓同一套 seal/unseal 与同一把口令（HUB_VENDOR_KEY 或本地密钥文件）。
# 旧行是明文 JSON，读取时仍兼容；新写入一律密文，列名 creds_encrypted 从此名前相符。
CREDS_FORMAT = "vendor_creds.v1"
_PASS_LOCK = threading.Lock()
_PASS_CACHE = {"value": ""}

WB_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
WB_CHECKIN_STATUS_PATH = "/v2/billing/meter/daily-checkin-status"
WB_UPSTREAMS = {
    "cn": "https://www.codebuddy.cn",
    "intl": "https://www.workbuddy.ai",
}

TRAE_HOST_DEFAULT = "https://api-work.trae.ai"
TRAE_CHECKIN_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"
TRAE_CHECKIN_STATUS = "/trae/api/v2/ug/checkin_credits/status"

ACCOUNTS_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  realm TEXT NOT NULL DEFAULT 'default',
  name TEXT NOT NULL,
  creds_encrypted TEXT NOT NULL,
  last_checkin REAL DEFAULT 0.0,
  credits REAL DEFAULT 0.0,
  status TEXT NOT NULL DEFAULT 'active',
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_channel ON accounts(channel);
"""

ACCOUNTS_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS accounts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  channel VARCHAR(64) NOT NULL,
  realm VARCHAR(64) NOT NULL DEFAULT 'default',
  name VARCHAR(191) NOT NULL,
  creds_encrypted TEXT NOT NULL,
  last_checkin DOUBLE DEFAULT 0,
  credits DOUBLE DEFAULT 0,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  updated_at DOUBLE NOT NULL,
  KEY idx_accounts_channel (channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _mysql(ledger) -> bool:
    return str(getattr(ledger, "backend", "sqlite") or "sqlite").strip().lower() == "mysql"


class _Con:
    """sqlite3 与 pymysql 的统一外壳：`?` 占位符自动翻译，行统一成 tuple。"""

    def __init__(self, con, mysql: bool):
        self._con = con
        self._mysql = mysql

    def executescript(self, sql: str) -> None:
        if not self._mysql:
            self._con.executescript(sql)
            return
        for stmt in sql.split(";"):          # pymysql 没有 executescript
            if stmt.strip():
                self.execute(stmt)

    def execute(self, sql: str, args=()):
        if self._mysql:
            cur = self._con.cursor()
            cur.execute(sql.replace("?", "%s"), args)
            return cur
        return self._con.execute(sql, args)


def _rows(rows) -> list:
    """pymysql DictCursor（dict）→ tuple，按 SELECT 列顺序，与 sqlite3.Row 同形状。"""
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]


@contextlib.contextmanager
def _db(ledger):
    """一次一连接，用完必关。

    原实现用 `with ledger.connect() as con`：sqlite3 的 `with` 只管事务**不关连接**，
    在 Windows 上会占住库文件，tearDown 里 unlink 就 PermissionError。
    """
    con = ledger.connect()
    try:
        yield _Con(con, _mysql(ledger))
        con.commit()
    finally:
        try:
            con.close()
        except Exception:                    # noqa: BLE001 关闭失败不该盖住签到结果
            pass


def ensure_accounts_table(ledger) -> None:
    """确保 accounts 表存在（SQLite / MySQL 同一套调用）。"""
    with _db(ledger) as con:
        con.executescript(ACCOUNTS_SCHEMA_MYSQL if _mysql(ledger) else ACCOUNTS_SCHEMA_SQLITE)


def _passphrase() -> str:
    """保险仓口令：进程内缓存，避免每行都去读一次密钥文件。"""
    with _PASS_LOCK:
        if not _PASS_CACHE["value"]:
            _PASS_CACHE["value"] = vendor_creds.VendorCreds().passphrase
        return _PASS_CACHE["value"]


def seal_creds(payload: dict) -> str:
    """凭据→密文文本（含 salt/nonce/tag），落库前唯一入口。"""
    blob = vendor_creds.seal(dict(payload or {}), _passphrase())
    return json.dumps({"enc": CREDS_FORMAT, "blob": blob}, ensure_ascii=False)


def open_creds(blob: str) -> dict:
    """读一条凭据：密文解封；历史明文行原样兼容；坏了就回空，绝不猜。"""
    try:
        doc = json.loads(str(blob or ""))
    except (TypeError, ValueError):
        # 最早期的裸令牌行：整列就是 token
        raw = str(blob or "").strip()
        return {"token": raw, "extra": {}} if raw else {"token": "", "extra": {}}
    if not isinstance(doc, dict):
        return {"token": "", "extra": {}}
    if doc.get("enc") == CREDS_FORMAT and isinstance(doc.get("blob"), dict):
        try:
            plain = vendor_creds.unseal(doc["blob"], _passphrase())
        except vendor_creds.VendorCredsError:
            return {"token": "", "extra": {}}
        return plain if isinstance(plain, dict) else {"token": "", "extra": {}}
    if "token" in doc or "extra" in doc:
        return doc                          # 旧明文行
    return {"token": "", "extra": {}}


def mask_token(token: str) -> str:
    """脱敏展示凭据。"""
    t = str(token or "").strip()
    if len(t) <= 8:
        return "***"
    return f"{t[:4]}...{t[-4:]}"


def import_text(ledger, channel: str, raw_text: str, realm: str = "default", name: str = "") -> dict[str, Any]:
    """贴原文导入账号凭据并入库。"""
    ensure_accounts_table(ledger)
    text = str(raw_text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty text provided"}

    token = ""
    extra = {}

    # 尝试解析 JSON 格式
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            token = str(data.get("token") or data.get("access_token") or data.get("key") or data.get("api_key") or "")
            realm = str(data.get("realm") or realm or "default").strip()
            name = str(data.get("name") or name or "").strip()
            extra = {k: v for k, v in data.items() if k not in ("token", "access_token", "key", "api_key")}
    except Exception:
        pass

    # 纯文本模式提取 token
    if not token:
        m = re.search(r"(?:Bearer\s+)?([A-Za-z0-9_\-\.]{20,})", text)
        if m:
            token = m.group(1)
        else:
            token = text.splitlines()[0].strip()

    if not token:
        return {"ok": False, "error": "Failed to extract valid token from raw text"}

    now = time.time()
    account_name = name or f"{channel}-{realm}-{int(now) % 10000}"
    creds_blob = seal_creds({"token": token, "extra": extra})

    with _db(ledger) as con:
        cur = con.execute(
            "INSERT INTO accounts (channel, realm, name, creds_encrypted, updated_at) VALUES (?, ?, ?, ?, ?)",
            (channel, realm, account_name, creds_blob, now)
        )
        account_id = cur.lastrowid

    return {
        "ok": True,
        "id": account_id,
        "channel": channel,
        "realm": realm,
        "name": account_name,
        "token_masked": mask_token(token),
    }


def list_accounts(ledger, channel: str = "") -> list[dict[str, Any]]:
    """列表账号（凭据已掩码）。"""
    ensure_accounts_table(ledger)
    sql = ("SELECT id, channel, realm, name, creds_encrypted, last_checkin, credits, status, updated_at "
           "FROM accounts")
    with _db(ledger) as con:
        if channel:
            rows = _rows(con.execute(sql + " WHERE channel = ? ORDER BY id ASC", (channel,)).fetchall())
        else:
            rows = _rows(con.execute(sql + " ORDER BY id ASC").fetchall())

    out = []
    for r in rows:
        token = open_creds(r[4]).get("token", "")
        out.append({
            "id": r[0],
            "channel": r[1],
            "realm": r[2],
            "name": r[3],
            "token_masked": mask_token(token),
            "last_checkin": r[5],
            "last_checkin_iso": clock.iso(r[5]) if r[5] > 0 else "-",
            "credits": r[6],
            "status": r[7],
            "updated_at": r[8],
        })
    return out


def _plaintext_creds(blob) -> dict | None:
    """判断一行是不是明文凭据：是则返回可重加密的明文，否则 None。"""
    text = str(blob or "").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return {"token": text, "extra": {}}          # 裸令牌行
    if not isinstance(doc, dict):
        return None
    if doc.get("enc") == CREDS_FORMAT:
        return None                                   # 已是密文
    if "token" in doc or "extra" in doc:
        return doc                                    # 旧明文 JSON
    return None


def migrate_plaintext_creds(ledger) -> dict[str, Any]:
    """启动时把历史明文行一次性重加密：幂等，已有密文行不动。"""
    ensure_accounts_table(ledger)
    with _db(ledger) as con:
        rows = _rows(con.execute("SELECT id, creds_encrypted FROM accounts").fetchall())
    migrated: list[int] = []
    failed: list[int] = []
    for acc_id, blob in rows:
        plain = _plaintext_creds(blob)
        if plain is None:
            continue
        try:
            sealed = seal_creds(plain)
        except Exception:                             # noqa: BLE001 口令不可用就别动这一行
            failed.append(int(acc_id))
            continue
        with _db(ledger) as con:
            con.execute("UPDATE accounts SET creds_encrypted = ? WHERE id = ?",
                        (sealed, int(acc_id)))
        migrated.append(int(acc_id))
    return {"ok": not failed, "scanned": len(rows),
            "migrated": migrated, "failed": failed}


def delete_account(ledger, account_id) -> dict[str, Any]:
    """删除一条账号行：只删本地这一行，不碰官方会话、不碰保险仓。"""
    ensure_accounts_table(ledger)
    try:
        target = int(account_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "id 必须是整数"}
    with _db(ledger) as con:
        cur = con.execute("DELETE FROM accounts WHERE id = ?", (target,))
        removed = int(getattr(cur, "rowcount", 0) or 0)
    if removed <= 0:
        return {"ok": False, "id": target, "error": f"Account #{target} not found"}
    return {"ok": True, "id": target, "deleted": removed}


def _checkin_workbuddy_online(token: str, realm: str, extra: dict, timeout: int = 10) -> dict[str, Any]:
    base = WB_UPSTREAMS.get(realm.lower(), WB_UPSTREAMS["cn"])
    url = base + WB_CHECKIN_PATH
    headers = {
        "authorization": f"Bearer {token}",
        "user-agent": "WorkBuddy/5.5.6",
        "content-type": "application/json",
    }
    eid = extra.get("enterprise_id") or extra.get("eid")
    if eid:
        headers["x-enterprise-id"] = str(eid)

    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            code = data.get("code", -1)
            msg = str(data.get("msg") or "").strip()
            repeated = "已签到" in msg or "重复" in msg
            ok = code == 0 or repeated
            return {
                "ok": ok,
                "code": code,
                "msg": msg or ("ok" if ok else "failed"),
                "data": data.get("data")
            }
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "status": "NEED_REFRESH", "reason": f"HTTP {exc.code} Unauthorized, please re-import token"}
        return {"ok": False, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


def _checkin_trae_online(token: str, extra: dict, timeout: int = 10) -> dict[str, Any]:
    host = extra.get("host") or TRAE_HOST_DEFAULT
    claim_url = host + TRAE_CHECKIN_CLAIM
    headers = {
        "authorization": f"Bearer {token}",
        "user-agent": "Trae/1.0",
        "content-type": "application/json",
        "x-device-id": extra.get("device_id") or "neo-device-default",
        "x-app-version": "1.0.0",
        "x-os-version": "10.0.0",
    }
    req = urllib.request.Request(claim_url, data=b'{"req_source": 1}', headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            claim_data = json.loads(resp.read().decode("utf-8"))
            code = claim_data.get("code", -1)
            msg = claim_data.get("message") or claim_data.get("msg") or ""
            if code != 0:
                return {"ok": False, "code": code, "reason": msg or f"code={code}"}

            # 成功后回读积分
            credits = None
            try:
                st_req = urllib.request.Request(host + TRAE_CHECKIN_STATUS, data=b'{"req_source": 1}', headers=headers, method="POST")
                with urllib.request.urlopen(st_req, timeout=timeout) as st_resp:
                    st_data = json.loads(st_resp.read().decode("utf-8"))
                    credits = (st_data.get("data") or {}).get("credits") or st_data.get("credits")
            except Exception:
                pass

            return {"ok": True, "code": 0, "msg": "success", "credits": credits}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "status": "NEED_REFRESH", "reason": f"HTTP {exc.code} Unauthorized, please re-import token"}
        return {"ok": False, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


def checkin_account(ledger, account_id: int, mock_result: dict | None = None) -> dict[str, Any]:
    """执行单个账号签到。"""
    ensure_accounts_table(ledger)
    with _db(ledger) as con:
        found = con.execute(
            "SELECT id, channel, realm, name, creds_encrypted, last_checkin, credits, status "
            "FROM accounts WHERE id = ?",
            (account_id,)
        ).fetchone()
    row = _rows([found])[0] if found else None

    if not row:
        return {"ok": False, "error": f"Account #{account_id} not found"}

    acc_id, channel, realm, name, creds_encrypted, _, old_credits, _ = row
    parsed = open_creds(creds_encrypted)
    token = parsed.get("token", "")
    extra = parsed.get("extra", {})

    # 执行签到请求（支持单元测试 mock 注入）
    if mock_result is not None:
        res = mock_result
    else:
        ch = channel.lower()
        if "workbuddy" in ch:
            res = _checkin_workbuddy_online(token, realm, extra)
        elif "trae" in ch:
            res = _checkin_trae_online(token, extra)
        else:
            return {"ok": False, "reason": f"Channel '{channel}' has no checkin provider supported"}

    now = time.time()
    ok = res.get("ok", False)
    credits_gain = res.get("credits")

    # 严格守则：业务成功才更新 last_checkin
    if ok:
        new_credits = float(credits_gain) if credits_gain is not None else old_credits
        with _db(ledger) as con:
            con.execute(
                "UPDATE accounts SET last_checkin = ?, credits = ?, status = 'active', updated_at = ? WHERE id = ?",
                (now, new_credits, now, acc_id)
            )
        return {
            "ok": True,
            "id": acc_id,
            "name": name,
            "channel": channel,
            "msg": res.get("msg") or "Checkin successful",
            "last_checkin_iso": clock.iso(now),
            "credits": new_credits,
        }
    else:
        # 失败不改写 last_checkin
        status_flag = "need_refresh" if res.get("status") == "NEED_REFRESH" else "checkin_failed"
        with _db(ledger) as con:
            con.execute(
                "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
                (status_flag, now, acc_id)
            )
        return {
            "ok": False,
            "id": acc_id,
            "name": name,
            "channel": channel,
            "reason": res.get("reason") or "Checkin failed",
            "status": res.get("status") or "FAILED",
        }


def checkin_all(ledger, channel: str = "", mock_map: dict | None = None) -> dict[str, Any]:
    """批量签到全部账号。"""
    accs = list_accounts(ledger, channel)
    total = len(accs)
    passed = 0
    results = []

    for acc in accs:
        acc_id = acc["id"]
        mock_r = (mock_map or {}).get(acc_id)
        r = checkin_account(ledger, acc_id, mock_result=mock_r)
        if r.get("ok"):
            passed += 1
        results.append(r)

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }


def health_summary(ledger) -> dict[str, Any]:
    """只读账号池健康状况。"""
    accs = list_accounts(ledger)
    now = time.time()
    # 签到日按 CST 00:00 切，与账本/面板同一口径。
    today_start = clock.day_start(now)

    checked_in_today = sum(1 for a in accs if a["last_checkin"] >= today_start)
    total_credits = sum(float(a.get("credits") or 0.0) for a in accs)

    return {
        "total_accounts": len(accs),
        "checked_in_today": checked_in_today,
        "can_checkin": len(accs) - checked_in_today,
        "total_credits": total_credits,
        "accounts": accs,
    }
