"""事件日历：手工维护的 events.yaml + 按规律生成的常规数据发布日 + 交易日历里的长假。

常规数据的发布日只是"约"，实际以官方日历为准；它们只用于提示"这几天消息面可能波动"，不参与打分。
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

import yaml

from .config import EVENTS_PATH
from .tradecal import Calendar


def _first_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def recurring(start: date, end: date) -> list[dict]:
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        last = date(y, m, _cal.monthrange(y, m)[1])
        out += [
            {"date": last, "name": "中国官方制造业 PMI 公布", "importance": "medium"},
            {"date": date(y, m, 9), "name": "中国 CPI/PPI 公布（约）", "importance": "low"},
            # 美国非农在每月第一个周五美东时间早上公布，对应北京时间周五晚上
            {"date": _first_weekday(y, m, 4), "name": "美国非农就业数据公布", "importance": "medium"},
        ]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [e for e in out if start <= e["date"] <= end]


def holidays(cal: Calendar, start: date, end: date) -> list[dict]:
    out = []
    for before, after in cal.long_holidays(start, end):
        if start <= before <= end:
            out.append({"date": before, "name": f"长假前最后一个交易日（休市至 {after.isoformat()}，当晚无夜盘）",
                        "importance": "high"})
    return out


def load(cal: Calendar, start: date, end: date) -> list[dict]:
    events = []
    if EVENTS_PATH.exists():
        for e in yaml.safe_load(EVENTS_PATH.read_text(encoding="utf-8")) or []:
            d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
            if start <= d <= end:
                events.append({**e, "date": d})
    events += recurring(start, end) + holidays(cal, start, end)
    for e in events:
        e.setdefault("products", None)
    return sorted(events, key=lambda e: e["date"])


def for_product(events: list[dict], code: str) -> list[dict]:
    return [e for e in events if not e.get("products") or code in e["products"]]
