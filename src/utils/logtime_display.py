"""日志库 logtime 字段显示用的秒级偏移（与存库时区/偏差对齐）。"""

import os
from datetime import datetime, timezone, tzinfo
from typing import Optional

# 通知/记录里统一使用的时间显示时区。
# 原来各轮询器口径不一：部分硬编码 Asia/Shanghai、部分用容器本地时区、部分用 astimezone()，
# 容器 TZ=UTC 时同一批通知的时间会互相差 8 小时。这里收敛为一个来源：
#   DISPLAY_TIMEZONE（IANA 名称，默认 Asia/Shanghai）→ 失败时回落容器本地时区。
DEFAULT_DISPLAY_TIMEZONE = "Asia/Shanghai"

_tz_cache: dict = {}


def get_logtime_display_offset_seconds() -> int:
    raw = os.environ.get("LOGTIME_DISPLAY_OFFSET_SECONDS", "28800")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 28800


def get_display_timezone() -> Optional[tzinfo]:
    """返回展示用 tzinfo；DISPLAY_TIMEZONE 非法/不可用时返回 None（调用方用本地时区）。"""
    name = (os.environ.get("DISPLAY_TIMEZONE") or DEFAULT_DISPLAY_TIMEZONE).strip()
    if not name:
        return None
    if name in _tz_cache:
        return _tz_cache[name]
    tz: Optional[tzinfo] = None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(name)
    except Exception:
        tz = None
    _tz_cache[name] = tz
    return tz


def format_ts_for_display(ts: float) -> str:
    """把 Unix 秒格式化为统一的 "YYYY-MM-DD HH:MM:SS" 展示字符串。"""
    tz = get_display_timezone()
    try:
        dt = datetime.fromtimestamp(ts, tz=tz) if tz is not None else datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        dt = datetime.now(tz) if tz is not None else datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def now_for_display() -> str:
    """当前时间（统一展示时区）的 "YYYY-MM-DD HH:MM:SS"。"""
    tz = get_display_timezone()
    dt = datetime.now(tz) if tz is not None else datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def local_tzinfo() -> tzinfo:
    """容器本地时区（用于需要显式 tzinfo 的场景）。"""
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc
