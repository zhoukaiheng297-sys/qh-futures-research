"""把批处理结果导出成网页只读的精简库 site.db（几 MB），网站只依赖这一个文件。"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

import numpy as np
import pandas as pd

from .config import SITE_DB_PATH, now
from .db import DB
from .model import HORIZONS
from .pipeline import health
from .universe import PRODUCTS, SECTORS, SPREADS

DISCLAIMER = ("数据来自期货交易所及新浪财经、生意社、上海金属网等公开网站，经 AKShare 获取；"
              "信号由统计模型生成，仅供研究交流，不构成投资建议。期货交易有杠杆，风险很大。")


def _json(obj) -> str:
    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, (pd.Timestamp,)):
            return o.isoformat()
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, (np.bool_,)):
            return bool(o)
        return str(o)

    def clean(o):  # float（含 np.float64）不经过 default，NaN/inf 要先换成 null，否则网页端 JSON 报错
        if isinstance(o, float):
            return o if np.isfinite(o) else None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o
    return json.dumps(clean(obj), ensure_ascii=False, default=default, allow_nan=False)


def news_for(db: DB, code: str, days: int = 7) -> dict:
    d = db.query("SELECT * FROM news_digest WHERE product = ? ORDER BY date DESC LIMIT 1", (code,))
    items = db.query("SELECT published_at, source, title, url, content, stance, impact, reason FROM news_items "
                     "WHERE product = ? AND published_at >= ? ORDER BY published_at DESC LIMIT 40",
                     (code, (now() - pd.Timedelta(days=days)).isoformat()))
    out = {"items": items.where(items.notna(), None).to_dict(orient="records")}
    if len(d):
        r = d.iloc[0]
        out["digest"] = {"date": r["date"], "summary": r["summary"], "sentiment": r["sentiment"],
                         "bullish": int(r["bullish"] or 0), "bearish": int(r["bearish"] or 0),
                         "neutral": int(r["neutral"] or 0), "sources": json.loads(r["sources"] or "[]"),
                         "model": r["model"], "created_at": r["created_at"]}
    return out


def export_site(db: DB, result: dict, explanations: dict, log: Callable = print) -> None:
    SITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITE_DB_PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE signals (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE series (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE news (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE explanations (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE blobs (key TEXT PRIMARY KEY, payload TEXT);
    """)
    model = result["model"]
    meta = {
        "signal_day": result["signal_day"].isoformat(), "generated_at": now().isoformat(timespec="seconds"),
        "trained_at": model["trained_at"], "data_last_date": model["last_date"], "disclaimer": DISCLAIMER,
        "horizons": list(HORIZONS), "sectors": SECTORS,
        "products": {c: {"name": p.name, "sector": p.sector, "exchange": p.exchange} for c, p in PRODUCTS.items()},
        "spreads": [{"name": s.name, "note": s.note} for s in SPREADS],
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, _json(v)) for k, v in meta.items()])
    for code, sig in result["signals"].items():
        con.execute("INSERT INTO signals VALUES (?, ?)", (code, _json(sig)))
        con.execute("INSERT INTO series VALUES (?, ?)", (code, _json(result["series"][code])))
        con.execute("INSERT INTO news VALUES (?, ?)", (code, _json(news_for(db, code))))
        if code in explanations:
            con.execute("INSERT INTO explanations VALUES (?, ?)", (code, _json(explanations[code])))
    comparison = {f"{sector}|{h}": meta_.get("expanding", {}) for (sector, h), meta_ in model["info"].items()}
    blobs = {
        "backtest": model["backtest"], "comparison_expanding": comparison, "health": health(db),
        "events": [{**e, "date": e["date"].isoformat()} for e in result["events"]],
        "forward": result.get("forward"),
    }
    con.executemany("INSERT INTO blobs VALUES (?, ?)", [(k, _json(v)) for k, v in blobs.items()])
    con.commit()
    con.close()
    tmp.replace(SITE_DB_PATH)
    log(f"网页数据已导出：{SITE_DB_PATH}（{SITE_DB_PATH.stat().st_size / 1e6:.1f} MB）")
