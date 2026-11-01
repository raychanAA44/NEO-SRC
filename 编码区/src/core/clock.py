"""clock.py —— 全站唯一时间口径（CST / UTC+8）。

面板与账本页显示的一律是上海时间，所以「今日」窗口的边界也必须按 UTC+8 切，
不能用宿主机的 `time.localtime`：这台机器的系统时区是美东，直接取本地时间会让
「TODAY 00:00–24:00」实际落在 12:00→12:00（上海），读数与标签对不上。

不用 zoneinfo（Windows 上需要额外 tzdata），中国无夏令时，固定 +8 足够且确定。
"""
from __future__ import annotations

import time

TZ_OFFSET = 8 * 3600          # UTC+8
DAY = 86400


def day_start(now=None) -> int:
    """CST 当日 00:00 的 epoch 秒。"""
    ts = float(time.time() if now is None else now)
    shifted = ts + TZ_OFFSET
    return int(shifted - (shifted % DAY) - TZ_OFFSET)


def week_start(now=None) -> int:
    """CST 本周一 00:00 的 epoch 秒（周一起算）。"""
    start = day_start(now)
    # epoch 1970-01-01 是周四；+8 时区下 day_start 已是北京零点。
    weekday = int((start + TZ_OFFSET) // DAY + 3) % 7   # 0=周一
    return int(start - weekday * DAY)


def month_start(now=None) -> int:
    """CST 本月 1 日 00:00 的 epoch 秒。"""
    ts = float(time.time() if now is None else now)
    stamp = time.gmtime(ts + TZ_OFFSET)
    first = int(ts + TZ_OFFSET) - (stamp.tm_mday - 1) * DAY
    return int(first - (first % DAY) - TZ_OFFSET)


def iso(ts=None) -> str:
    """`YYYY-MM-DD HH:MM:SS`（CST）。"""
    value = time.time() if ts is None else float(ts)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(value + TZ_OFFSET))


def iso_t(ts=None) -> str:
    """`YYYY-MM-DDTHH:MM:SS`（CST），给需要 T 分隔的接口字段。"""
    return iso(ts).replace(" ", "T")


def date(ts=None) -> str:
    """`YYYY-MM-DD`（CST）。"""
    return iso(ts)[:10]
