"""时区解析。

库里存的时间一律是 UTC（`store.iso()` 写出来带 `+00:00`），只有「按天 / 按分钟」
分桶的统计和页面标签需要按配置的时区来切 —— 否则 UTC+8 的用户在早上 8 点前
看到的「今天」其实是昨天。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo

log = logging.getLogger("timeutil")


def system_tz() -> tzinfo:
    """本机时区。拿不到就退回 UTC。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def resolve(name: str) -> tzinfo:
    """配置里的时区名 → tzinfo。

    留空 = 跟随本机。名字解析不了（拼错、或者 Windows 上没装 tzdata）也只告警
    不抛异常 —— 一个显示用的时区不该让服务起不来。
    """
    name = (name or "").strip()
    if not name:
        return system_tz()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as e:  # ZoneInfoNotFoundError 在 Windows 上很常见
        log.warning("时区 %r 解析失败（%s），改用本机时区", name, e)
        return system_tz()


def sqlite_offset(tz: tzinfo) -> str:
    """给 SQLite 的 `datetime(列, ?)` 用的偏移量，单位分钟。

    用分钟而不是小时：印度（+5:30）、尼泊尔（+5:45）这类半小时时区
    按小时取整会直接算错日期。
    """
    offset = datetime.now(tz).utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds() // 60):+d} minutes"


def label(tz: tzinfo) -> str:
    """页面上显示的短标签，如 UTC+08:00。"""
    offset = datetime.now(tz).utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
