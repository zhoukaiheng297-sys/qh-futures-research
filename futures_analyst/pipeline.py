"""批处理主流程：采集 → 主力连续 → 因子 → 滚动打分与校准 → 回测 → 可靠性 → 消息 → 解读 → 导出网页数据。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable

import joblib
import numpy as np
import pandas as pd

from . import backtest as BT
from . import events as EV
from . import forward as FW
from .config import MODEL_PATH, now
from .continuous import build_continuous
from .db import DB
from .factors import FACTORS, REGISTRY, compute_raw, expected_factors, quadrant, standardize
from .model import HORIZONS, contributions, forward_return, walk_forward
from .reliability import agreement_of, analogs, calibration_bucket, factor_stats, judge, model_skill
from .tradecal import Calendar, load_calendar
from .universe import PRODUCTS, SECTORS, SPREADS


# ---------- 读数据、算因子 ----------
def load_inputs(db: DB) -> dict:
    conts = {}
    for code in PRODUCTS:
        bars = db.bars(code)
        if len(bars):
            conts[code] = build_continuous(bars)
    fx = db.table("fx").set_index("date")["usdcny"].sort_index()
    digest = db.table("news_digest")
    return {
        "conts": conts,
        "ranks": db.table("ranks"),
        "receipts": db.table("receipts"),
        "basis": db.table("basis"),
        "foreign": db.table("foreign_bars"),
        "fx": fx,
        "sentiment": digest.set_index(["product", "date"])["sentiment"] if len(digest) else pd.Series(dtype=float),
    }


def build_factors(inp: dict) -> tuple[dict, dict]:
    raw, z = {}, {}
    for code, cont in inp["conts"].items():
        sent = None
        if len(inp["sentiment"]) and code in inp["sentiment"].index.get_level_values(0):
            sent = inp["sentiment"].xs(code)
        r = compute_raw(code, inp["conts"],
                        inp["ranks"][inp["ranks"]["product"] == code],
                        inp["receipts"][inp["receipts"]["product"] == code],
                        inp["basis"][inp["basis"]["product"] == code],
                        inp["foreign"], inp["fx"], sent)
        raw[code] = r
        z[code] = standardize(r)
    return raw, z


def _fees_per_lot(db: DB) -> dict[str, float]:
    raw = db.get_meta("fees")
    if not raw:
        return {}
    df = pd.DataFrame(json.loads(raw))
    out = {}
    for code, g in df.groupby("品种代码"):
        main = g.sort_values("持仓量", ascending=False).iloc[0]
        out[code] = float(main["1手开仓费用"])
    return out


def _rules(db: DB) -> dict[str, dict]:
    raw = db.get_meta("rules")
    if not raw:
        return {}
    return {r["代码"]: r for r in json.loads(raw)}


# ---------- 训练与回测 ----------
def train(db: DB, inp: dict, z: dict, log: Callable = print) -> dict:
    preds_all, preds_b_all, info = [], [], {}
    for h in HORIZONS:
        for sector, codes in SECTORS.items():
            panel = {}
            for c in codes:
                if c not in z:
                    continue
                df = z[c].copy()
                df["y"] = forward_return(inp["conts"][c], h)
                panel[c] = df
            if not panel:
                continue
            log(f"{sector} 板块：")
            preds, meta = walk_forward(panel, h, log)
            preds_all.append(preds)
            # 对照：全部历史一起估计权重（扩展窗口），只记录样本外表现
            alt, _ = walk_forward(panel, h, log, window=None, with_gbm=False)
            meta["expanding"] = {c: model_skill(alt[alt["product"] == c], h) for c in panel}
            info[(sector, h)] = meta
            # 前瞻检验的对照模型 B：同样的方法，只用期限与基差、持仓、内外盘三类因子（见 forward.py）
            panel_b = {c: df[[f for f in FW.B_FACTORS if f in df] + ["y"]] for c, df in panel.items()}
            pb, _ = walk_forward(panel_b, h, lambda msg: log(msg.replace("  h=", "  对照模型 h=", 1)), with_gbm=False)
            preds_b_all.append(pb)
    preds = pd.concat(preds_all, ignore_index=True)
    rows = [(r.product, int(r.horizon), r.date.date().isoformat(),
             *(None if pd.isna(v) else float(v) for v in (r.score, r.prob, r.gbm_prob, r.fwd_ret)))
            for r in preds.itertuples()]
    with db._lock, db.conn:
        db.conn.execute("DELETE FROM predictions")
    db.upsert("predictions", ["product", "horizon", "date", "score", "prob", "gbm_prob", "fwd_ret"], rows)

    moms = {c: inp["conts"][c]["adj_close"].pipe(lambda s: np.log(s / s.shift(20))) for c in inp["conts"]}
    bt = BT.run(preds, inp["conts"], moms, _fees_per_lot(db), HORIZONS)
    model = {"info": info, "backtest": bt, "trained_at": now().isoformat(timespec="seconds"),
             "preds_b": pd.concat(preds_b_all, ignore_index=True)[["product", "horizon", "date", "score", "prob"]],
             "last_date": max(c.index.max() for c in inp["conts"].values()).date().isoformat()}
    joblib.dump(model, MODEL_PATH)
    return model


# ---------- 最新信号 ----------
def _pct_rank(s: pd.Series, window: int = 250) -> float | None:
    tail = s.dropna().iloc[-window:]
    if len(tail) < 60:
        return None
    return round(float((tail <= tail.iloc[-1]).mean()), 3)


def _num(v, nd=4):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def settle_quote(db: DB, contract: str, d: date) -> dict:
    """行情软件口径：涨跌 = 收盘 − 这个合约自己的昨日结算价（换月当天也不拿旧主力的结算价比）。"""
    rows = db.execute("SELECT date, close, settle FROM bars WHERE contract = ? AND date <= ? ORDER BY date DESC LIMIT 2",
                      (contract, d.isoformat()))
    out = {"settle": None, "pre_settle": None, "change_pts": None, "change_settle": None}
    if not rows or rows[0][0] != d.isoformat():
        return out
    close, out["settle"] = rows[0][1], rows[0][2]
    pre = rows[1][2] if len(rows) > 1 else None
    if pre and close is not None:
        out.update(pre_settle=pre, change_pts=round(close - pre, 2), change_settle=close / pre - 1)
    return out


def latest_signal(code: str, db: DB, cal: Calendar, inp: dict, raw: dict, z: dict, preds: pd.DataFrame,
                  model: dict, events: list[dict], signal_day: date) -> dict:
    p = PRODUCTS[code]
    c = inp["conts"][code]
    zz, rr = z[code], raw[code]
    last = c.index[-1]
    i = len(c) - 1
    rules = _rules(db).get(code, {})
    limit_pct = float(rules.get("涨跌停板幅度") or p.limit_pct)
    margin_pct = float(rules.get("交易保证金比例") or p.margin_pct)
    close = float(c["close"].iloc[-1])
    tr = pd.concat([c["high"] - c["low"], (c["high"] - c["close"].shift()).abs(),
                    (c["low"] - c["close"].shift()).abs()], axis=1).max(axis=1)
    atr14 = float(tr.iloc[-14:].mean())

    out = {
        "product": code, "name": p.name, "sector": p.sector, "exchange": p.exchange, "unit": p.unit,
        "date": last.date().isoformat(), "signal_day": signal_day.isoformat(),
        "contract": c["contract"].iloc[-1], "close": close,
        **settle_quote(db, c["contract"].iloc[-1], last.date()),   # 网页展示用的涨跌（比昨结算）
        # 以下是收盘对收盘、换月已复权的涨跌，模型和因子用这个口径
        "change_1d": _num(c["ret"].iloc[-1]), "change_5d": _num(np.exp(rr["mom_5"].iloc[-1]) - 1),
        "change_20d": _num(np.exp(rr["mom_20"].iloc[-1]) - 1),
        "total_oi": float(c["total_oi"].iloc[-1]),
        "oi_change_5d": _num(c["total_oi"].iloc[-1] / c["total_oi"].iloc[-6] - 1),
        "quadrant": quadrant(rr["mom_5"].iloc[-1], np.log(c["total_oi"].iloc[-1] / c["total_oi"].iloc[-6])),
        "risk": {
            "multiplier": p.multiplier, "limit_pct": limit_pct, "margin_pct": margin_pct,
            "margin_per_lot": round(close * p.multiplier * margin_pct / 100),
            "limit_pnl_per_lot": round(close * p.multiplier * limit_pct / 100),
            "atr14": round(atr14, 2), "atr_pnl_per_lot": round(atr14 * p.multiplier),
            "rv_20": _num(rr["rv_20"].iloc[-1], 3), "rv_pct": _pct_rank(rr["rv_20"]),
        },
        "near_roll": bool(c["near_roll"].iloc[-1]),
        "next_contract": c["next_contract"].iloc[-1],
        "round_number": {"nearest": round(close / p.round_step) * p.round_step,
                         "distance_pct": _num(close / (round(close / p.round_step) * p.round_step) - 1)},
    }
    if p.foreign and "_ext_close" in rr:
        out["foreign"] = {"symbol": p.foreign, "name": p.foreign_name, "close": _num(rr["_ext_close"].iloc[-1], 2),
                          "ratio_log": _num(rr["ext_ratio"].iloc[-1]), "ratio_z": _num(zz["ext_ratio"].iloc[-1], 2)}

    factors = []
    for f in REGISTRY:
        if f.name not in rr:
            continue
        factors.append({"name": f.name, "label": f.label, "group": f.group, "desc": f.desc, "in_model": f.in_model,
                        "expected": f.expected, "raw": _num(rr[f.name].iloc[-1]),
                        "z": _num(zz[f.name].iloc[-1], 2) if f.name in zz else None, "pct": _pct_rank(rr[f.name])})
    out["factors"] = factors
    nd = db.query("SELECT date, summary, sentiment, bullish, bearish, neutral FROM news_digest WHERE product = ? "
                  "ORDER BY date DESC LIMIT 1", (code,))
    if len(nd):
        out["news"] = nd.iloc[0].to_dict()
    expected = expected_factors(code)
    missing = [FACTORS[f].label for f in expected if f not in zz or pd.isna(zz[f].iloc[-1])]
    coverage = 1 - len(missing) / len(expected)

    # 降级条件（与周期无关的部分）
    common_flags = []
    if last.date() < signal_day:
        common_flags.append((f"行情数据不是最新的（最新到 {last.date()}，应为 {signal_day}）", True))
    if coverage < 0.75:
        common_flags.append((f"只有 {coverage:.0%} 的因子有数据（缺：{'、'.join(missing[:6])}）", True))
    elif missing:
        common_flags.append((f"部分因子今天没有数据：{'、'.join(missing[:6])}", False))
    if out["near_roll"]:
        common_flags.append(("处于主力换月期，主力合约和持仓数据有扰动", False))
    vol_pct = _pct_rank(c["total_volume"])
    if vol_pct is not None and vol_pct < 0.1:
        common_flags.append(("成交量处于近一年最低的 10%，流动性偏弱", False))
    if abs(c["ret"].iloc[-1]) * 100 >= 0.8 * limit_pct:
        common_flags.append((f"今天涨跌 {c['ret'].iloc[-1]:+.1%}，接近涨跌停幅度 {limit_pct:.0f}%", False))
    if out["risk"]["rv_pct"] is not None and out["risk"]["rv_pct"] >= 0.9:
        common_flags.append(("波动率处于近一年最高的 10%，历史规律在剧烈行情里更容易失效", False))

    out["horizons"] = {}
    prod_events = EV.for_product(events, code)
    for h in HORIZONS:
        meta = model["info"].get((p.sector, h))
        if not meta or code not in meta["weights"]:
            continue
        w = meta["weights"][code].iloc[-1]
        ph = preds[(preds["product"] == code) & (preds["horizon"] == h)].set_index("date").sort_index()
        row = ph.iloc[-1] if len(ph) else None
        score = float(row["score"]) if row is not None and pd.notna(row["score"]) else np.nan
        prob = float(row["prob"]) if row is not None and pd.notna(row["prob"]) else np.nan
        contrib = contributions(zz.iloc[-1], w).sort_values(key=np.abs, ascending=False)
        y = forward_return(c, h)
        fstats = factor_stats(zz, y, meta["weights"][code], h)
        skill = model_skill(ph.reset_index(), h)
        sector_preds = preds[(preds["horizon"] == h) & preds["product"].isin(SECTORS[p.sector])]
        bucket = calibration_bucket(sector_preds, prob)
        agree = agreement_of(contrib)
        horizon_end = cal.shift(signal_day, h)
        ev = [e for e in prod_events if signal_day < e["date"] <= horizon_end]
        flags = list(common_flags)
        for e in ev:
            if e.get("importance") == "high":
                flags.append((f"{e['date'].isoformat()} {e['name']}", False))
        rel = judge(prob, score, skill, agree, bucket, flags)
        out["horizons"][str(h)] = {
            "horizon": h, "until": horizon_end.isoformat(), "score": _num(score, 3), "prob": _num(prob, 3),
            **rel.to_dict(),
            "agreement": _num(agree, 3),
            "contributions": [{"factor": f, "label": FACTORS[f].label, "group": FACTORS[f].group,
                               "z": _num(zz[f].iloc[-1], 2), "contribution": _num(v, 3), "weight": _num(w[f], 4),
                               **{k: fstats.get(f, {}).get(k) for k in ("hit_rate", "n", "ic")}}
                              for f, v in contrib.items()],
            "skill": skill, "bucket": bucket, "analogs": analogs(zz, y, i, h),
            "events": [{"date": e["date"].isoformat(), "name": e["name"], "importance": e.get("importance")}
                       for e in ev],
            "backtest": model["backtest"].get(h, {}).get(code),
        }
    return out


def chart_series(code: str, inp: dict, raw: dict, preds: pd.DataFrame, days: int = 500) -> dict:
    c = inp["conts"][code].iloc[-days:]
    r = raw[code].reindex(c.index)
    pr = preds[(preds["product"] == code)].pivot_table(index="date", columns="horizon", values="prob")
    pr = pr.reindex(c.index)
    out = {
        "date": [d.date().isoformat() for d in c.index],
        "close": c["close"].round(2).tolist(), "adj_close": c["adj_close"].round(2).tolist(),
        "high": c["high"].round(2).tolist(), "low": c["low"].round(2).tolist(),
        "total_oi": c["total_oi"].tolist(), "volume": c["total_volume"].tolist(),
        "contract": c["contract"].tolist(), "roll_day": c["roll_day"].astype(bool).tolist(),
    }
    for col in ("ext_ratio", "_ext_close", "net_long", "receipts_chg", "basis_rate", "spread_rb", "spread_hc",
                "spread_coke", "clv_flow"):
        if col in r:
            out[col.lstrip("_")] = [None if pd.isna(v) else round(float(v), 5) for v in r[col]]
    for h in HORIZONS:
        if h in pr:
            out[f"prob_{h}"] = [None if pd.isna(v) else round(float(v), 3) for v in pr[h]]
    return out


def health(db: DB) -> list[dict]:
    df = db.query("SELECT dataset, MAX(at) AS last_ok, MAX(latest) AS latest, COUNT(*) AS n FROM fetch_log "
                  "WHERE ok = 1 GROUP BY dataset")
    bad = db.query("SELECT dataset, COUNT(*) AS failures, MAX(at) AS last_fail, MAX(error) AS error FROM fetch_log "
                   "WHERE ok = 0 GROUP BY dataset")
    df = df.merge(bad, on="dataset", how="outer")
    names = {"bars": "合约日线（新浪）", "ranks_shfe": "上期所会员排名", "ranks_sina": "大商所会员排名（新浪）",
             "receipts": "上期所仓单", "basis": "现货与基差（生意社）", "basis_chunk": "现货与基差（批量）",
             "foreign": "外盘（新浪）", "fx": "汇率（中国银行）", "inventory": "库存（东方财富）",
             "shmet": "上海金属网快讯", "rules": "交易规则", "fees": "手续费", "news_ai": "消息汇总（Claude）"}
    out = []
    for r in df.itertuples():
        out.append({"dataset": r.dataset, "name": names.get(r.dataset, r.dataset), "last_ok": r.last_ok,
                    "latest": r.latest, "failures": 0 if pd.isna(r.failures) else int(r.failures),
                    "last_error": None if pd.isna(r.error) else r.error})
    return out


# ---------- 入口 ----------
def compute_all(db: DB, log: Callable = print, retrain: bool = True) -> dict:
    cal = Calendar(load_calendar(db))
    signal_day = cal.signal_date()
    log("构建主力连续合约与因子 …")
    inp = load_inputs(db)
    raw, z = build_factors(inp)
    if retrain or not MODEL_PATH.exists():
        log("滚动训练与回测 …")
        model = train(db, inp, z, log)
    else:
        model = joblib.load(MODEL_PATH)
    preds = db.table("predictions")
    events = EV.load(cal, signal_day - timedelta(days=5), cal.shift(signal_day, 30))
    signals = {code: latest_signal(code, db, cal, inp, raw, z, preds, model, events, signal_day)
               for code in inp["conts"]}
    series = {code: chart_series(code, inp, raw, preds) for code in inp["conts"]}
    if model.get("preds_b") is not None:
        n = FW.record(db, {"A": preds, "B": model["preds_b"]}, inp["conts"], signal_day)
        log(f"前瞻检验：新记录 {n} 条预测（{FW.FORWARD_START} 起）")
    forward = FW.evaluate(db, inp["conts"], cal)
    return {"signals": signals, "series": series, "model": model, "events": events, "signal_day": signal_day,
            "inp": inp, "raw": raw, "z": z, "forward": forward}


def daily(skip_collect: bool = False, skip_news: bool = False, log: Callable = print) -> None:
    from .collect import run as collect
    from .explain import explain_all
    from .export import export_site

    db = DB()
    if not skip_collect:
        collect(db, full=False, log=log)
    result = compute_all(db, log)
    if not skip_news:
        from .news import digest_all

        if digest_all(db, result["signals"], log):
            result = compute_all(db, log, retrain=False)   # 让最新的消息情绪进入展示
    explanations = explain_all(db, result["signals"], log, use_ai=not skip_news)
    export_site(db, result, explanations, log)


def train_and_export(log: Callable = print) -> None:
    from .explain import explain_all
    from .export import export_site

    db = DB()
    result = compute_all(db, log)
    export_site(db, result, explain_all(db, result["signals"], log, use_ai=False), log)
