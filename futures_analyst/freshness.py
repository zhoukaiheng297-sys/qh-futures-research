"""数据过期检查：每项关键数据比信号日落后几个交易日。

GitHub Actions 每次上传结果之后运行 `main.py check`，有过期就让任务失败，GitHub 会发邮件提醒。
单个数据源抓取失败不会让采集中断（只记进 fetch_log），不做这一步的话，数据可能悄悄停更。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .config import SITE_DB_PATH
from .db import DB
from .tradecal import Calendar
from .universe import PRODUCTS

# 落后 3 个交易日及以上算过期。按 2024-10 ~ 2026-10 的真实数据回看，只会在 2024 年 12 月报一次
# （新浪连续 7 个交易日没有焦炭主力的会员排名）；外盘遇到英国假日最多落后 2 天，不会误报。
MAX_LAG = 3

_ALL = sorted(PRODUCTS)
CHECKS = [
    # 名称、按组取最新日期的 SQL、应当有数据的组
    ("合约日线", "SELECT product, MAX(date) FROM bars GROUP BY product", _ALL),
    ("会员持仓排名", "SELECT product, MAX(date) FROM ranks GROUP BY product", _ALL),
    ("上期所仓单", "SELECT product, MAX(date) FROM receipts GROUP BY product",
     sorted(c for c, p in PRODUCTS.items() if p.receipt_keys)),
    ("现货与基差", "SELECT product, MAX(date) FROM basis GROUP BY product", _ALL),
    ("外盘", "SELECT symbol, MAX(date) FROM foreign_bars GROUP BY symbol",
     sorted({p.foreign for p in PRODUCTS.values() if p.foreign})),
    ("汇率", "SELECT 'USDCNY', MAX(date) FROM fx", ["USDCNY"]),
]


def lag(cal: Calendar, last: date | None, expected: date) -> int | None:
    """last 之后、expected 及之前有几个交易日没有数据；完全没有数据时返回 None。"""
    if last is None:
        return None
    return len(cal.between(last + timedelta(days=1), expected))


def check(db: DB, cal: Calendar, expected: date, site_db: Path = SITE_DB_PATH) -> list[dict]:
    rows = []
    for name, sql, groups in CHECKS:
        latest = {g: d for g, d in db.execute(sql)}
        for g in groups:
            last = date.fromisoformat(latest[g][:10]) if latest.get(g) else None
            n = lag(cal, last, expected)
            rows.append({"name": name, "group": g, "last": last, "lag": n, "ok": n is not None and n < MAX_LAG})

    # 网页数据必须是这次算出来的
    signal_day = None
    if site_db.exists():
        con = sqlite3.connect(f"file:{site_db}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT value FROM meta WHERE key = 'signal_day'").fetchone()
            signal_day = date.fromisoformat(json.loads(r[0])) if r else None
        finally:
            con.close()
    rows.append({"name": "网页数据", "group": "信号日", "last": signal_day,
                 "lag": lag(cal, signal_day, expected), "ok": signal_day == expected})
    return rows


def report(rows: list[dict], expected: date, log=print) -> bool:
    bad = [r for r in rows if not r["ok"]]
    log(f"数据过期检查：应更新到 {expected}，落后 {MAX_LAG} 个交易日及以上算过期")
    for r in rows:
        mark = "OK  " if r["ok"] else "过期"
        behind = "没有数据" if r["lag"] is None else f"落后 {r['lag']} 天"
        log(f"  {mark} {r['name']:<8} {r['group']:<7} 最新 {r['last'] or '—'}  {behind}")
    if bad:
        log(f"{len(bad)} 项过期。看上面“回补缺失的历史”步骤的日志，或网站的“数据状态”页，找出是哪个数据源抓取失败。")
    else:
        log("全部是最新的。")
    return not bad
