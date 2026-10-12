"""A股交易日历与“数据是否对应某个交易日”的判断。"""

import datetime as dt
import logging
import os
from bisect import bisect_left, bisect_right

import pandas as pd

from . import paths

log = logging.getLogger("kmcf")

PREOPEN = dt.time(9, 0)       # 此后实时快照开始变成当日盘中数据
CLOSE_READY = dt.time(15, 5)  # 此后当日收盘数据可用

_DAYS: list[dt.date] | None = None
_REAL_LAST: dt.date | None = None  # 真实日历覆盖到的最后一天，之后的日子是按工作日估算的
approximate = False  # True 表示日历获取失败，退化为“工作日=交易日”


def _fetch_remote() -> list[dt.date]:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    return sorted(set(pd.to_datetime(df["trade_date"]).dt.date))


def trade_days(refresh: bool = False) -> list[dt.date]:
    """返回升序交易日列表（内存 + 磁盘缓存，磁盘缓存 7 天刷新一次）。"""
    global _DAYS, _REAL_LAST, approximate
    if _DAYS is not None and not refresh:
        return _DAYS

    today = dt.date.today()
    cache = paths.CACHE_DIR / "trade_calendar.pkl"
    days: list[dt.date] | None = None
    stale = True
    if cache.exists():
        try:
            days = list(pd.read_pickle(cache))
            age = dt.datetime.now() - dt.datetime.fromtimestamp(cache.stat().st_mtime)
            stale = refresh or age > dt.timedelta(days=7) or not days or days[-1] < today
        except Exception as e:  # 缓存损坏
            log.warning("交易日历缓存损坏：%s", e)
            days = None

    if days is None or stale:
        try:
            fresh = _fetch_remote()
            if fresh:
                days = fresh
                paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                pd.Series(days).to_pickle(tmp)
                os.replace(tmp, cache)
        except Exception as e:
            log.warning("交易日历获取失败：%s", e)

    approximate = False
    _REAL_LAST = days[-1] if days else None
    if not days:
        approximate = True
        start = today - dt.timedelta(days=800)
        days = [start + dt.timedelta(days=i) for i in range(1200)]
        days = [d for d in days if d.weekday() < 5]
    elif days[-1] < today + dt.timedelta(days=30):
        # 年底时新一年的日历可能还没发布，用工作日临时补齐
        d = days[-1] + dt.timedelta(days=1)
        while d <= today + dt.timedelta(days=30):
            if d.weekday() < 5:
                days.append(d)
            d += dt.timedelta(days=1)

    _DAYS = days
    return days


def is_estimated(d: dt.date) -> bool:
    """d 是否超出了真实交易日历的覆盖范围（只能按工作日估算，节假日可能不准）。"""
    trade_days()
    return approximate or _REAL_LAST is None or d > _REAL_LAST


def is_trade_day(d: dt.date) -> bool:
    days = trade_days()
    i = bisect_left(days, d)
    return i < len(days) and days[i] == d


def prev_trade_day(d: dt.date, n: int = 1) -> dt.date | None:
    """严格早于 d 的第 n 个交易日。"""
    days = trade_days()
    i = bisect_left(days, d) - n
    return days[i] if i >= 0 else None


def next_trade_day(d: dt.date) -> dt.date | None:
    days = trade_days()
    i = bisect_right(days, d)
    return days[i] if i < len(days) else None


def recent_trade_days(end: dt.date, n: int) -> list[dt.date]:
    """截至 end（含，若 end 是交易日）的最近 n 个交易日，升序。"""
    days = trade_days()
    i = bisect_right(days, end)
    return days[max(0, i - n):i]


def latest_completed_trade_day(now: dt.datetime | None = None) -> dt.date:
    """当前时刻已经收盘的最近一个交易日。"""
    now = now or dt.datetime.now()
    today = now.date()
    if is_trade_day(today) and now.time() >= CLOSE_READY:
        return today
    return prev_trade_day(today)


def snapshot_available_for(target: dt.date, now: dt.datetime | None = None) -> bool:
    """此刻抓取“实时快照”类接口，得到的是否正好是 target 当天的收盘数据。

    只有 target 是最近一个已收盘交易日、且现在不在新交易日的盘中时才成立。
    """
    now = now or dt.datetime.now()
    if target != latest_completed_trade_day(now):
        return False
    if is_trade_day(now.date()) and PREOPEN <= now.time() < CLOSE_READY:
        return False
    return True
