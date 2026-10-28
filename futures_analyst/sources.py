"""数据接口：每个函数调用一个 AKShare 接口并把结果整理成入库用的行。

所有调用都经过 fetch()：失败重试、记录到 fetch_log（来源、行数、最新日期、耗时、错误），网页据此显示数据新鲜度。
AKShare 背后是交易所和公开网站，接口偶尔变动；这里的字段名按 akshare 1.20 实测确认。
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from typing import Callable

import pandas as pd

from .config import now
from .db import DB
from .universe import PRODUCTS


class SourceError(Exception):
    pass


def fetch(db: DB, dataset: str, key: str, fn: Callable, retries: int = 3, backoff: float = 2.0,
          describe: Callable | None = None):
    """调用 fn()，失败重试；把结果写进 fetch_log。describe(result) -> (行数, 最新日期)。"""
    t0 = time.time()
    err = None
    for attempt in range(retries):
        try:
            result = fn()
            rows, latest = describe(result) if describe else (None, None)
            db.upsert("fetch_log", ["dataset", "key", "ok", "rows", "latest", "elapsed", "error", "at"],
                      [(dataset, key, 1, rows, latest, round(time.time() - t0, 2), None,
                        now().isoformat(timespec="seconds"))])
            return result
        except Exception as e:  # noqa: BLE001 —— 数据源的异常五花八门，统一重试后记录
            err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    db.upsert("fetch_log", ["dataset", "key", "ok", "rows", "latest", "elapsed", "error", "at"],
              [(dataset, key, 0, None, None, round(time.time() - t0, 2), f"{type(err).__name__}: {str(err)[:300]}",
                now().isoformat(timespec="seconds"))])
    raise SourceError(f"{dataset}/{key}: {type(err).__name__}: {err}") from err


def _iso(d) -> str:
    return pd.Timestamp(d).date().isoformat()


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ---------- 合约日线（新浪，覆盖上期所和大商所） ----------
def contract_bars(contract: str) -> list[tuple]:
    import akshare as ak

    try:
        df = ak.futures_zh_daily_sina(symbol=contract)
    except (KeyError, IndexError, TypeError, ValueError):
        return []  # 不存在的合约（如未挂牌月份），新浪返回的结构不完整
    if df is None or df.empty:
        return []
    product = re.match(r"[A-Z]+", contract).group(0)
    return [(contract, product, _iso(r.date), float(r.open), float(r.high), float(r.low), float(r.close),
             float(r.volume), float(r.hold), float(r.settle) if r.settle else None)
            for r in df.itertuples()]


BAR_COLS = ["contract", "product", "date", "open", "high", "low", "close", "volume", "oi", "settle"]


# ---------- 会员持仓排名 ----------
def ranks_shfe(d: date, codes: list[str]) -> list[tuple]:
    """上期所：交易所公布的每个合约前 20 名，AKShare 汇总出品种合计行（symbol == variety）。"""
    import akshare as ak

    df = ak.get_rank_sum_daily(start_day=_ymd(d), end_day=_ymd(d), vars_list=codes)
    if df is None or df.empty:
        return []
    df = df[df["symbol"] == df["variety"]]
    return [(r.variety, d.isoformat(), "variety", float(r.long_open_interest_top20), float(r.short_open_interest_top20),
             float(r.long_open_interest_chg_top20), float(r.short_open_interest_chg_top20), float(r.vol_top20))
            for r in df.itertuples()]


def ranks_sina_contract(product: str, contract: str, d: date) -> list[tuple]:
    """大商所官网有反爬，改用新浪的单合约前 20 名（只统计主力合约）。"""
    import akshare as ak

    out = {}
    for kind in ("多单持仓", "空单持仓"):
        df = ak.futures_hold_pos_sina(symbol=kind, contract=contract, date=_ymd(d))
        if df is None or df.empty:
            return []
        out[kind] = (float(pd.to_numeric(df.iloc[:, 2], errors="coerce").sum()),
                     float(pd.to_numeric(df.iloc[:, 3], errors="coerce").sum()))
    return [(product, d.isoformat(), contract, out["多单持仓"][0], out["空单持仓"][0],
             out["多单持仓"][1], out["空单持仓"][1], None)]


RANK_COLS = ["product", "date", "scope", "long20", "short20", "long_chg20", "short_chg20", "vol20"]


# ---------- 上期所仓单 ----------
def _receipt_total(table: pd.DataFrame) -> tuple[float, float]:
    name = table["WHABBRNAME"].astype(str).str.strip()
    status = pd.to_numeric(table.get("ROWSTATUS", 0), errors="coerce")
    w = pd.to_numeric(table["WRTWGHTS"], errors="coerce").fillna(0)
    c = pd.to_numeric(table["WRTCHANGE"], errors="coerce").fillna(0)
    if (name == "总计").any():
        m = name == "总计"
    elif (status == 1).any():
        m = status == 1
    else:
        m = status == 0
    return float(w[m].sum()), float(c[m].sum())


def receipts_shfe(d: date) -> list[tuple]:
    import akshare as ak

    tables = ak.futures_shfe_warehouse_receipt(date=_ymd(d))
    out = []
    for p in PRODUCTS.values():
        keys = [k for k in p.receipt_keys if k in tables]
        if not keys:
            continue
        # 新格式把"仓库"和"厂库"分开列；旧格式只有一个表。两种都存在时只取新格式
        if any(k.endswith(("仓库", "厂库")) for k in keys):
            keys = [k for k in keys if k.endswith(("仓库", "厂库"))]
        qty = chg = 0.0
        for k in keys:
            q, c = _receipt_total(tables[k])
            qty += q
            chg += c
        out.append((p.code, d.isoformat(), qty, chg))
    return out


RECEIPT_COLS = ["product", "date", "qty", "chg"]


# ---------- 基差（生意社现货） ----------
def basis_range(start: date, end: date, codes: list[str]) -> list[tuple]:
    import akshare as ak

    df = ak.futures_spot_price_daily(start_day=_ymd(start), end_day=_ymd(end), vars_list=codes)
    if df is None or df.empty:
        return []
    return [(r.symbol, _iso(r.date), float(r.spot_price), str(r.dominant_contract).upper(),
             float(r.dominant_contract_price), float(r.dom_basis_rate)) for r in df.itertuples()]


BASIS_COLS = ["product", "date", "spot", "dom_contract", "dom_price", "dom_basis_rate"]


# ---------- 外盘与汇率 ----------
def foreign_hist(symbol: str) -> list[tuple]:
    import akshare as ak

    df = ak.futures_foreign_hist(symbol=symbol)
    return [(symbol, _iso(r.date), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
            for r in df.itertuples()]


FOREIGN_COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def usdcny_range(start: date, end: date) -> list[tuple]:
    """中国银行（经新浪）公布的美元汇率，单位是每 100 美元；优先用央行中间价。"""
    import akshare as ak

    df = ak.currency_boc_sina(symbol="美元", start_date=_ymd(start), end_date=_ymd(end))
    if df is None or df.empty:
        return []
    mid = pd.to_numeric(df["央行中间价"], errors="coerce")
    alt = pd.to_numeric(df["中行折算价"], errors="coerce")
    rate = mid.fillna(alt) / 100
    return [(_iso(d), float(v)) for d, v in zip(df["日期"], rate) if pd.notna(v)]


# ---------- 库存（东方财富，最近约 60 个交易日） ----------
def inventory_em(product: str) -> list[tuple]:
    import akshare as ak

    df = ak.futures_inventory_em(symbol=PRODUCTS[product].inventory_em)
    return [(product, _iso(d), float(q)) for d, q in zip(df["日期"], df["库存"]) if pd.notna(q)]


# ---------- 上海金属网快讯 ----------
_TAG = re.compile(r"<[^>]+>")


def shmet_news(product: str) -> list[tuple]:
    import akshare as ak

    tag = PRODUCTS[product].news_tag
    if not tag:
        return []
    df = ak.futures_news_shmet(symbol=tag)
    out = []
    for ts, content in zip(df["发布时间"], df["内容"]):
        text = _TAG.sub("", str(content)).replace("&nbsp;", " ").strip()
        if not text:
            continue
        published = pd.Timestamp(ts).tz_convert("Asia/Shanghai").isoformat()
        nid = "shmet-" + hashlib.sha1(f"{product}{published}{text[:50]}".encode()).hexdigest()[:16]
        title = text.split("】")[0].lstrip("【") if text.startswith("【") else text[:40]
        out.append((nid, product, published, "上海金属网", title, None, text[:2000], None, None, None, None))
    return out


NEWS_COLS = ["id", "product", "published_at", "source", "title", "url", "content", "stance", "impact", "reason",
             "digest_date"]


# ---------- 交易规则与手续费（只有当前快照） ----------
def trading_rules(d: date) -> pd.DataFrame:
    import akshare as ak

    df = ak.futures_rule(date=_ymd(d))
    return df[df["代码"].isin(list(PRODUCTS))]


def fees() -> pd.DataFrame:
    import akshare as ak

    df = ak.futures_fees_info()
    df = df[df["品种代码"].str.upper().isin(list(PRODUCTS))].copy()
    df["品种代码"] = df["品种代码"].str.upper()
    df["合约代码"] = df["合约代码"].str.upper()
    return df
