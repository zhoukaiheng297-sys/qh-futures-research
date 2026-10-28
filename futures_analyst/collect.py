"""采集编排：init（回补历史，可断点续跑）和 update（日更）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Callable, Iterable

import pandas as pd

from . import sources as S
from .config import HISTORY_START, RANKS_START, now, today
from .db import DB
from .tradecal import Calendar, load_calendar
from .universe import PRODUCTS, contract_expiry_month, contract_month_codes

WORKERS = 4


def _latest(rows: list[tuple], idx: int) -> tuple[int, str | None]:
    return len(rows), max((r[idx] for r in rows), default=None)


def _run_parallel(items: Iterable, job: Callable, log: Callable, label: str) -> tuple[int, int]:
    items = list(items)
    ok = fail = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(job, it): it for it in items}
        for k, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
                ok += 1
            except S.SourceError as e:
                fail += 1
                log(f"  ! {e}")
            if k % 50 == 0 or k == len(items):
                log(f"  {label}: {k}/{len(items)}（失败 {fail}）")
    return ok, fail


# ---------- 各数据集 ----------
def collect_bars(db: DB, full: bool, log: Callable = print) -> None:
    t = today()
    end_y, end_m = (t.year + 1, t.month) if t.month < 12 else (t.year + 1, 12)
    start_year = date.fromisoformat(HISTORY_START).year - 1   # 往前多取一年，保证回补起点就有主力合约
    codes = []
    for p in PRODUCTS:
        all_codes = contract_month_codes(p, start_year, end_y, end_m)
        if full:
            done = db.done_keys("bars")
            codes += [c for c in all_codes if c not in done or contract_expiry_month(c) >= (t.year, t.month)]
        else:  # 日更：只取还没到期的合约（新浪每次返回整个合约的全部历史）
            codes += [c for c in all_codes if contract_expiry_month(c) >= (t.year, t.month)]

    def job(code):
        rows = S.fetch(db, "bars", code, lambda: S.contract_bars(code), describe=lambda r: _latest(r, 2))
        db.upsert("bars", S.BAR_COLS, rows)

    log(f"合约日线：{len(codes)} 个合约")
    _run_parallel(codes, job, log, "合约日线")


def _nonempty(rows: list[tuple], d: date, end: date) -> list[tuple]:
    """最近几天拿到空结果多半是还没公布：报错，下次再取；更早的日期空就是真的没有（如新品种上市前）。"""
    if not rows and d >= end - timedelta(days=7):
        raise S.SourceError(f"{d} 的数据还没有公布")
    return rows


def _pending_dates(db: DB, cal: Calendar, dataset: str, start: str, end: date) -> list[date]:
    done = db.done_keys(dataset)
    return [d for d in cal.between(date.fromisoformat(start), end) if d.isoformat() not in done]


def main_contract_on(db: DB, product: str, d: date) -> str | None:
    rows = db.execute("SELECT contract FROM bars WHERE product = ? AND date = ? ORDER BY oi DESC LIMIT 1",
                      (product, d.isoformat()))
    return rows[0][0] if rows else None


def collect_ranks(db: DB, cal: Calendar, end: date, log: Callable = print) -> None:
    shfe = [p.code for p in PRODUCTS.values() if p.exchange == "SHFE"]
    dce = [p.code for p in PRODUCTS.values() if p.exchange == "DCE"]

    def job_shfe(d):
        rows = S.fetch(db, "ranks_shfe", d.isoformat(), lambda: _nonempty(S.ranks_shfe(d, shfe), d, end),
                       describe=lambda r: (len(r), d.isoformat()))
        db.upsert("ranks", S.RANK_COLS, rows)

    dates = _pending_dates(db, cal, "ranks_shfe", RANKS_START, end)
    log(f"上期所会员持仓排名：{len(dates)} 个交易日")
    _run_parallel(dates, job_shfe, log, "上期所排名")

    done = db.done_keys("ranks_sina")
    jobs = [(p, d) for d in cal.between(date.fromisoformat(RANKS_START), end) for p in dce
            if f"{p}:{d.isoformat()}" not in done]

    def job_dce(item):
        p, d = item
        c = main_contract_on(db, p, d)
        if not c:
            return
        rows = S.fetch(db, "ranks_sina", f"{p}:{d.isoformat()}",
                       lambda: _nonempty(S.ranks_sina_contract(p, c, d), d, end),
                       describe=lambda r: (len(r), d.isoformat()))
        db.upsert("ranks", S.RANK_COLS, rows)

    log(f"大商所会员持仓排名（新浪，主力合约）：{len(jobs)} 个品种日")
    _run_parallel(jobs, job_dce, log, "大商所排名")


def collect_receipts(db: DB, cal: Calendar, end: date, log: Callable = print) -> None:
    def job(d):
        rows = S.fetch(db, "receipts", d.isoformat(), lambda: _nonempty(S.receipts_shfe(d), d, end),
                       describe=lambda r: (len(r), d.isoformat()))
        db.upsert("receipts", S.RECEIPT_COLS, rows)

    dates = _pending_dates(db, cal, "receipts", HISTORY_START, end)
    log(f"上期所仓单：{len(dates)} 个交易日")
    _run_parallel(dates, job, log, "仓单")


def collect_basis(db: DB, cal: Calendar, end: date, log: Callable = print) -> None:
    dates = _pending_dates(db, cal, "basis", HISTORY_START, end)
    chunks = [dates[i:i + 15] for i in range(0, len(dates), 15)]

    def job(chunk):
        rows = S.fetch(db, "basis_chunk", f"{chunk[0]}~{chunk[-1]}",
                       lambda: S.basis_range(chunk[0], chunk[-1], list(PRODUCTS)), describe=lambda r: _latest(r, 1))
        db.upsert("basis", S.BASIS_COLS, rows)
        got = {r[1] for r in rows}
        # 逐日标记完成；当天还没出数据的日期留给下次
        db.upsert("fetch_log", ["dataset", "key", "ok", "rows", "latest", "elapsed", "error", "at"],
                  [("basis", d.isoformat(), 1, None, d.isoformat(), 0, None, now().isoformat(timespec="seconds"))
                   for d in chunk if d.isoformat() in got or d < end - timedelta(days=7)])

    log(f"基差：{len(dates)} 个交易日")
    _run_parallel(chunks, job, log, "基差")


def collect_foreign(db: DB, log: Callable = print) -> None:
    for sym in sorted({p.foreign for p in PRODUCTS.values() if p.foreign}):
        try:
            rows = S.fetch(db, "foreign", sym, lambda: S.foreign_hist(sym), describe=lambda r: _latest(r, 1))
            db.upsert("foreign_bars", S.FOREIGN_COLS, rows)
        except S.SourceError as e:
            log(f"  ! {e}")
    log("外盘日线：完成")


def collect_fx(db: DB, full: bool, log: Callable = print) -> None:
    t = today()
    start = date.fromisoformat(HISTORY_START) if full else t - timedelta(days=30)
    for y in range(start.year, t.year + 1):
        a, b = max(start, date(y, 1, 1)), min(t, date(y, 12, 31))
        try:
            rows = S.fetch(db, "fx", str(y), lambda: S.usdcny_range(a, b), describe=lambda r: _latest(r, 0))
            db.upsert("fx", ["date", "usdcny"], rows)
        except S.SourceError as e:
            log(f"  ! {e}")
    log("汇率：完成")


def collect_inventory_and_news(db: DB, log: Callable = print) -> None:
    for p in PRODUCTS.values():
        try:
            rows = S.fetch(db, "inventory", p.code, lambda: S.inventory_em(p.code), describe=lambda r: _latest(r, 1))
            db.upsert("inventory", ["product", "date", "qty"], rows)
        except S.SourceError as e:
            log(f"  ! {e}")
        if p.news_tag:
            try:
                rows = S.fetch(db, "shmet", p.code, lambda: S.shmet_news(p.code), describe=lambda r: _latest(r, 2))
                with db._lock, db.conn:  # 已经打过标签的条目不覆盖
                    db.conn.executemany(f"INSERT OR IGNORE INTO news_items VALUES ({', '.join('?' * 11)})", rows)
            except S.SourceError as e:
                log(f"  ! {e}")
    log("库存与快讯：完成")


def collect_rules(db: DB, d: date, log: Callable = print) -> None:
    """交易规则和手续费只有当前快照，存进 meta 供风险刻画使用。"""
    try:
        rules = S.fetch(db, "rules", d.isoformat(), lambda: S.trading_rules(d), describe=lambda r: (len(r), None))
        db.set_meta("rules", rules.to_json(orient="records", force_ascii=False))
    except S.SourceError as e:
        log(f"  ! {e}")
    try:
        fees = S.fetch(db, "fees", d.isoformat(), S.fees, describe=lambda r: (len(r), None))
        keep = ["品种代码", "合约代码", "1手开仓费用", "1手平仓费用", "做多1手保证金", "成交量", "持仓量"]
        db.set_meta("fees", fees[keep].to_json(orient="records", force_ascii=False))
    except S.SourceError as e:
        log(f"  ! {e}")


# ---------- 入口 ----------
def run(db: DB, full: bool, log: Callable = print, only: set[str] | None = None) -> date:
    cal = Calendar(load_calendar(db))
    end = cal.signal_date()
    want = (lambda name: only is None or name in only)
    log(f"采集截至 {end}（{'回补' if full else '日更'}）")
    if want("bars"):
        collect_bars(db, full, log)
    if want("ranks"):
        collect_ranks(db, cal, end, log)
    if want("receipts"):
        collect_receipts(db, cal, end, log)
    if want("basis"):
        collect_basis(db, cal, end, log)
    if want("foreign"):
        collect_foreign(db, log)
    if want("fx"):
        collect_fx(db, full, log)
    if want("misc"):
        collect_inventory_and_news(db, log)
        collect_rules(db, end, log)
    db.set_meta("last_collect", now().isoformat(timespec="seconds"))
    return end
