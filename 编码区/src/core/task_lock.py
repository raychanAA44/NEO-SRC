# -*- coding: utf-8 -*-
"""core/task_lock.py —— Task Model Lock:单任务生命周期粘性锁(NEO-GW-02)。

## 解决什么问题

多轮会话中途换模型会导致 Prefix Cache 击穿且 prompt 风格断层。本模块在
`(task_id, group_id)` 粒度上把"任务"钉死在同一具体模型上:分组首轮选中某模型后,
同任务后续请求一律复用,直至 TTL 到期或显式 release(故障熔断切换)。

## 设计边界(诚实标注)

- **内存态,不落盘**:网关进程重启即失忆。重启后首轮请求会重新选模并重新锁 ——
  代价仅是一次缓存重建,不产生正确性问题;持久化属后续独立切片。
- **纯标准库**:`threading.Lock` + 字典,无第三方依赖。
- TTL 到期惰性清理:查询时发现过期才删除,不做后台定时扫(零线程开销)。
"""
from __future__ import annotations

import threading
import time

__all__ = ["TaskLockStore", "DEFAULT_TTL_SECONDS"]

# 默认锁时长:12 小时(43200s),覆盖单个长任务的典型生命周期
DEFAULT_TTL_SECONDS = 43200


class TaskLockStore:
    """线程安全的 `(task_id, group_id) -> {model, expire_at}` 内存锁管理器。

    用法::

        store = TaskLockStore()
        store.bind("task-1", "auto/code-heavy", "trae/claude-3-7-sonnet")
        store.get("task-1", "auto/code-heavy")   # -> "trae/claude-3-7-sonnet"
        store.release("task-1", "auto/code-heavy")  # 故障熔断切换用
    """

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock                       # 可注入时钟,单测可造"过期"
        # {(task_id, group_id): {"model": str, "expire_at": float}}
        self._binds: dict = {}

    # ── 建立绑定 ─────────────────────────────────────────────
    def bind(self, task_id: str, group_id: str, concrete_model: str,
             ttl: int = DEFAULT_TTL_SECONDS) -> None:
        """建立/刷新绑定记录 `{model, expire_at}`。重复 bind 即刷新(续期+改模)。"""
        if not task_id or not group_id or not concrete_model:
            raise ValueError("bind 需要 task_id / group_id / concrete_model 均非空")
        with self._lock:
            self._binds[(str(task_id), str(group_id))] = {
                "model": str(concrete_model),
                "expire_at": self._clock() + max(1, int(ttl)),
            }

    # ── 查询绑定 ─────────────────────────────────────────────
    def get(self, task_id: str, group_id: str):
        """查询绑定;已过期则自动删除并返回 `None`(惰性过期)。"""
        key = (str(task_id or ""), str(group_id or ""))
        with self._lock:
            rec = self._binds.get(key)
            if rec is None:
                return None
            if self._clock() >= rec["expire_at"]:
                self._binds.pop(key, None)        # 过期即清,返回无锁
                return None
            return rec["model"]

    # ── 解绑 / 清空 ──────────────────────────────────────────
    def release(self, task_id: str, group_id: str) -> bool:
        """解绑(用于故障熔断切换:旧模型挂了,先解锁再让调度器重新选模)。

        返回是否真的移除了绑定;锁已过期/不存在时返回 False,供前端如实回执。
        """
        with self._lock:
            return self._binds.pop(
                (str(task_id or ""), str(group_id or "")), None) is not None

    def clear(self) -> None:
        """清空所有锁(测试与运维复位用)。"""
        with self._lock:
            self._binds.clear()

    # ── 观测(不参与裁决)────────────────────────────────────
    def size(self) -> int:
        with self._lock:
            return len(self._binds)

    def snapshot(self) -> list[dict]:
        """列出未过期锁(惰性清过期),按到期时间升序。

        只读快照,不改变裁决;TTL 秒数四舍五入,便于前端直接展示。
        """
        now = self._clock()
        rows: list[dict] = []
        with self._lock:
            for (task_id, group_id), rec in list(self._binds.items()):
                if now >= rec["expire_at"]:
                    self._binds.pop((task_id, group_id), None)
                    continue
                rows.append({
                    "task_id": task_id,
                    "group_id": group_id,
                    "model": rec["model"],
                    "expire_at": rec["expire_at"],
                    "ttl_remaining": max(0, int(rec["expire_at"] - now + 0.5)),
                })
        rows.sort(key=lambda r: (r["expire_at"], r["task_id"], r["group_id"]))
        return rows
