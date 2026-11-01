# -*- coding: utf-8 -*-
"""core/dispatch_ledger.py —— 网关派发台账（NEO-UI-02 只读遥测）。

只记录 `auto/*` 分组请求在网关侧的可核对事实：请求号、任务号、分组、最终
模型、锁命中、档位、派发耗时与上游状态。**不保存 prompt、响应正文或密钥**。

内存里保留最近 256 条；传入 `persist_path` 时同步落盘 SQLite，进程重启后
重新装载。这样 `/api/usage` 里的旧行仍能凭 `rid` 找回自己的派发轨迹，不会
因为一次重启出现满屏“无轨迹”。

`rid` 是 `/api/usage` 计费行的 join 键：开票时先记本地 `request_id`，上游
返回 `x-hub-rid` 后绑定为同一条 `rid`。只有绑定成功且计费行带同一个 `rid`
时才挂轨迹；不做时间邻接或模型名猜测。
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from pathlib import Path

__all__ = ["DispatchLedger", "MAX_TRACES"]

MAX_TRACES = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_traces (
  trace_id TEXT PRIMARY KEY,
  rid TEXT,
  request_id TEXT,
  upstream_rid TEXT,
  at INTEGER NOT NULL,
  task_id TEXT,
  requested_model TEXT,
  path TEXT,
  group_id TEXT,
  model TEXT,
  locked INTEGER NOT NULL DEFAULT 0,
  lock_state TEXT,
  lock_ttl INTEGER NOT NULL DEFAULT 0,
  tier TEXT,
  priority REAL,
  policy TEXT,
  dispatch_ms REAL,
  state TEXT,
  upstream_status INTEGER,
  upstream_ms REAL,
  response_bytes INTEGER,
  error TEXT,
  finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dispatch_at ON dispatch_traces(at);
"""

_COLUMNS = (
    "trace_id", "rid", "request_id", "upstream_rid", "at", "task_id",
    "requested_model", "path", "group_id", "model", "locked", "lock_state",
    "lock_ttl", "tier", "priority", "policy", "dispatch_ms", "state",
    "upstream_status", "upstream_ms", "response_bytes", "error", "finished_at",
)


class DispatchLedger:
    """线程安全的派发轨迹环形缓冲，可选 SQLite 持久化。

    用法::

        ledger = DispatchLedger(persist_path=".../dispatch_traces.db")
        trace = ledger.start(rid="r1", task_id="t1", requested_model="auto/x",
                             model="chan/m1", group_id="auto/x", locked=False)
        ledger.finish(trace["trace_id"], status=200, ms=812, response_bytes=42)
    """

    def __init__(self, maxlen: int = MAX_TRACES, clock=time.time,
                 persist_path=None):
        self._maxlen = max(1, int(maxlen))
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self._order: deque[str] = deque(maxlen=self._maxlen)
        self._db_path = Path(persist_path) if persist_path else None
        self.persist_error = ""
        if self._db_path is not None:
            try:
                self._init_db()
                self._load()
            except Exception as exc:  # 落盘失败只降级为内存台账，不拖垮网关
                self.persist_error = str(exc)[:300]
                self._db_path = None

    # ── 持久化基座 ───────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._db_path), timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict:
        try:
            policy = json.loads(row["policy"] or "{}")
        except (TypeError, ValueError):
            policy = {}
        return {
            "trace_id": row["trace_id"],
            "rid": row["rid"] or "",
            "request_id": row["request_id"] or "",
            "upstream_rid": row["upstream_rid"] or "",
            "at": int(row["at"]),
            "task_id": row["task_id"] or "",
            "requested_model": row["requested_model"] or "",
            "path": row["path"] or "",
            "group_id": row["group_id"] or "",
            "model": row["model"] or "",
            "locked": bool(row["locked"]),
            "lock_state": row["lock_state"] or "",
            "lock_ttl": int(row["lock_ttl"] or 0),
            "tier": row["tier"] or "",
            "priority": row["priority"],
            "policy": policy if isinstance(policy, dict) else {},
            "dispatch_ms": row["dispatch_ms"],
            "state": row["state"] or "done",
            "upstream_status": row["upstream_status"],
            "upstream_ms": row["upstream_ms"],
            "response_bytes": row["response_bytes"],
            "error": row["error"] or "",
            "finished_at": row["finished_at"],
        }

    def _load(self) -> None:
        """只恢复最近 maxlen 条；旧数据按时间升序回填 deque。"""
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM (SELECT * FROM dispatch_traces"
                " ORDER BY at DESC LIMIT ?)"
                " ORDER BY at ASC",
                (self._maxlen,),
            ).fetchall()
        for row in rows:
            rec = self._from_row(row)
            self._items[rec["trace_id"]] = rec
            self._order.append(rec["trace_id"])

    def _persist(self, rec: dict, prune: bool = False) -> None:
        if self._db_path is None:
            return
        try:
            policy = json.dumps(rec.get("policy") or {}, ensure_ascii=False,
                                separators=(",", ":"))
            values = [
                rec.get("trace_id"), rec.get("rid") or "",
                rec.get("request_id") or "", rec.get("upstream_rid") or "",
                int(rec.get("at") or 0), rec.get("task_id") or "",
                rec.get("requested_model") or "", rec.get("path") or "",
                rec.get("group_id") or "", rec.get("model") or "",
                1 if rec.get("locked") else 0, rec.get("lock_state") or "",
                int(rec.get("lock_ttl") or 0), rec.get("tier") or "",
                (float(rec["priority"]) if rec.get("priority") is not None
                 else None),
                policy, rec.get("dispatch_ms"), rec.get("state") or "done",
                rec.get("upstream_status"), rec.get("upstream_ms"),
                rec.get("response_bytes"), rec.get("error") or "",
                rec.get("finished_at"),
            ]
            with closing(self._connect()) as con:
                con.execute(
                    "INSERT OR REPLACE INTO dispatch_traces (%s)"
                    " VALUES (%s)" % (", ".join(_COLUMNS),
                                      ", ".join(["?"] * len(_COLUMNS))),
                    values,
                )
                if prune:
                    ids = list(self._order)
                    if ids:
                        con.execute(
                            "DELETE FROM dispatch_traces WHERE trace_id NOT IN"
                            " (%s)" % ", ".join(["?"] * len(ids)),
                            ids,
                        )
                con.commit()
            self.persist_error = ""
        except Exception as exc:  # 单次落盘失败不影响请求，保留错误供排查
            self.persist_error = str(exc)[:300]

    # ── 开票 ─────────────────────────────────────────────────────────
    def start(self, rid: str, task_id: str, requested_model: str, model: str,
              group_id: str, locked: bool, tier: str = "",
              priority=None, policy: dict | None = None,
              lock_state: str = "", lock_ttl: int = 0,
              dispatch_ms: float | None = None, path: str = "") -> dict:
        """登记一笔已裁决的分组派发，返回轨迹副本（含 `trace_id`）。"""
        trace_id = uuid.uuid4().hex
        rec = {
            "trace_id": trace_id,
            "rid": str(rid or ""),
            "request_id": str(rid or ""),
            "upstream_rid": "",
            "at": int(self._clock()),
            "task_id": str(task_id or ""),
            "requested_model": str(requested_model or ""),
            "path": str(path or ""),
            "group_id": str(group_id or requested_model or ""),
            "model": str(model or ""),
            "locked": bool(locked),
            "lock_state": str(lock_state or ""),
            "lock_ttl": max(0, int(lock_ttl or 0)),
            "tier": str(tier or ""),
            "priority": priority,
            "policy": copy.deepcopy(dict(policy or {})),
            "dispatch_ms": round(float(dispatch_ms), 3) if dispatch_ms is not None else None,
            "state": "running",
            "upstream_status": None,
            "upstream_ms": None,
            "response_bytes": None,
            "error": "",
            "finished_at": None,
        }
        with self._lock:
            self._items[trace_id] = rec
            self._order.append(trace_id)
            alive = set(self._order)
            for key in list(self._items):
                if key not in alive:
                    del self._items[key]
            self._persist(rec, prune=True)
            return copy.deepcopy(rec)

    # ── 绑定上游请求号 ───────────────────────────────────────────────
    def bind_rid(self, trace_id: str, rid: str) -> dict | None:
        """把上游 `x-hub-rid` 绑成 usage 行的 join 键。

        `request_id` 保留网关本地 X-Request-Id；`rid` 改为上游请求号，
        只有两者相等或上游确实回传时才更新。轨迹已滚出缓冲时返回 None。
        """
        upstream = str(rid or "").strip()[:64]
        with self._lock:
            rec = self._items.get(str(trace_id or ""))
            if rec is None:
                return None
            if upstream:
                rec["upstream_rid"] = upstream
                rec["rid"] = upstream
            self._persist(rec)
            return copy.deepcopy(rec)

    # ── 收票 ─────────────────────────────────────────────────────────
    def finish(self, trace_id: str, status: int | None, ms: float | None,
               response_bytes: int | None = None, error: str = "",
               state: str = "done") -> dict | None:
        """回填上游结果；轨迹已被环形缓冲淘汰时返回 None。"""
        with self._lock:
            rec = self._items.get(str(trace_id or ""))
            if rec is None:
                return None
            rec["state"] = str(state or "done")
            rec["upstream_status"] = int(status) if status is not None else None
            rec["upstream_ms"] = round(float(ms), 3) if ms is not None else None
            rec["response_bytes"] = int(response_bytes) if response_bytes is not None else None
            rec["error"] = str(error or "")[:300]
            rec["finished_at"] = int(self._clock())
            self._persist(rec)
            return copy.deepcopy(rec)

    # ── 读取 ─────────────────────────────────────────────────────────
    def get(self, trace_id: str) -> dict | None:
        with self._lock:
            rec = self._items.get(str(trace_id or ""))
            return copy.deepcopy(rec) if rec is not None else None

    def snapshot(self, limit: int = 50) -> list[dict]:
        """按时间倒序返回最近 `limit` 条轨迹（新票在前）。"""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))
        with self._lock:
            ids = list(self._order)[-limit:]
            return [copy.deepcopy(self._items[i]) for i in reversed(ids)
                    if i in self._items]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self, persisted: bool = False) -> None:
        """清空内存台账；`persisted=True` 时一并删档。

        默认不动 SQLite 文件：测试、误调与进程复位都不应抹掉生产轨迹。
        """
        with self._lock:
            self._items.clear()
            self._order.clear()
            if persisted and self._db_path is not None:
                try:
                    with closing(self._connect()) as con:
                        con.execute("DELETE FROM dispatch_traces")
                        con.commit()
                    self.persist_error = ""
                except Exception as exc:
                    self.persist_error = str(exc)[:300]
