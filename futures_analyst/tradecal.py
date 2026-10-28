"""交易日历与"信号日"。

- 交易日来自新浪（和沪深交易所同一套节假日安排，期货与股票休市日一致）。
- 夜盘归属下一个交易日：新浪日线里某交易日的开盘价就是前一晚夜盘的开盘价，所以按日线处理即可。
- 信号日：北京时间 16:30 之后算当天（等交易所排名和仓单公布），之前算上一个交易日。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from .config import DAY_CLOSE_READY, now
from .db import DB

_CACHE_KEY = "trade_calendar"


def load_calendar(db: DB, refresh: bool = False) -> list[date]:
    cached = db.get_meta(_CACHE_KEY)
    if cached and not refresh:
        days = [date.fromisoformat(d) for d in json.loads(cached)]
        if days and days[-1] > date.today() + timedelta(days=20):
            return days
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    days = sorted(pd.to_datetime(df["trade_date"]).dt.date.tolist())
    db.set_meta(_CACHE_KEY, json.dumps([d.isoformat() for d in days]))
    return days


class Calendar:
    def __init__(self, days: list[date]):
        self.days = days
        self.index = {d: i for i, d in enumerate(days)}

    def is_trading(self, d: date) -> bool:
        return d in self.index

    def prev(self, d: date) -> date:
        """严格早于 d 的最近一个交易日。"""
        i = pd.Index(self.days).searchsorted(d) - 1
        return self.days[i]

    def on_or_before(self, d: date) -> date:
        return d if d in self.index else self.prev(d)

    def shift(self, d: date, n: int) -> date:
        """往后数 n 个交易日。超出已公布日历（通常只到当年年底）的部分按工作日近似。"""
        k = self.index[self.on_or_before(d)] + n
        if k < len(self.days):
            return self.days[k]
        x, extra = self.days[-1], k - len(self.days) + 1
        while extra:
            x += timedelta(days=1)
            if x.weekday() < 5:
                extra -= 1
        return x

    def between(self, start: date, end: date) -> list[date]:
        return [d for d in self.days if start <= d <= end]

    def signal_date(self, at: datetime | None = None) -> date:
        """最近一个数据已经齐全的交易日。"""
        at = at or now()
        d = at.date()
        if self.is_trading(d) and at.time() >= DAY_CLOSE_READY:
            return d
        return self.prev(d)

    def long_holidays(self, start: date, end: date, min_gap: int = 4) -> list[tuple[date, date]]:
        """相邻交易日间隔 ≥ min_gap 天（周末之外还有假期）的区间：(假期前最后交易日, 假期后第一个交易日)。"""
        out = []
        sel = [d for d in self.days if start - timedelta(days=10) <= d <= end + timedelta(days=10)]
        for a, b in zip(sel, sel[1:]):
            if (b - a).days >= min_gap:
                out.append((a, b))
        return out
