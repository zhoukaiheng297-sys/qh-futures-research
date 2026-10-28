"""回测：用样本外预测模拟"每 h 个交易日看一次信号、按方向持有 h 日"，扣手续费和滑点，并和简单基准比。

- 不重叠持有（每 h 天换一次），这样夏普和回撤不会因为重叠收益被高估
- 进场：信号日下一个交易日开盘（夜盘）；出场：第 h 个交易日收盘
- 成本：双边手续费 + 双边各 1 跳滑点，按当时价格折算成收益率
- 基准：抛硬币（随机方向，给出 95% 区间）、一直做多、20 日动量（涨就做多、跌就做空）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .reliability import LONG_TH, SHORT_TH
from .universe import PRODUCTS


def _cost(code: str, price: pd.Series, fee_per_lot: float | None) -> pd.Series:
    p = PRODUCTS[code]
    fee = fee_per_lot if fee_per_lot is not None else 0.0001 * price * p.multiplier   # 缺省按万分之一
    return (2 * fee + 2 * p.tick * p.multiplier) / (price * p.multiplier)


def _stats(r: pd.Series, h: int) -> dict:
    """r：每个持有期的净收益（不持仓记 0）。"""
    active = r[r != 0]
    eq = r.cumsum()
    dd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    per_year = 242 / h
    sd = r.std()
    return {
        "periods": int(len(r)), "trades": int(len(active)),
        "hit_rate": round(float((active > 0).mean()), 3) if len(active) else None,
        "avg_trade": round(float(active.mean()), 4) if len(active) else None,
        "ann_return": round(float(r.mean() * per_year), 4),
        "sharpe": round(float(r.mean() / sd * np.sqrt(per_year)), 2) if sd > 0 else None,
        "max_drawdown": round(dd, 4),
    }


def run_product(code: str, h: int, preds: pd.DataFrame, cont: pd.DataFrame, mom20: pd.Series,
                fee_per_lot: float | None, use: str = "prob") -> dict:
    p = preds[(preds["product"] == code) & (preds["horizon"] == h)].set_index("date").sort_index()
    p = p[p[use].notna() & p["fwd_ret"].notna()]
    if len(p) < 50:
        return {"n": int(len(p))}
    sample = p.iloc[::h]
    gross = sample["fwd_ret"]
    cost = _cost(code, cont["close"].reindex(sample.index), fee_per_lot)
    pos = np.where(sample[use] >= LONG_TH, 1, np.where(sample[use] <= SHORT_TH, -1, 0))
    strat = pd.Series(pos * gross - np.abs(pos) * cost, index=sample.index)
    long_only = gross - cost
    m = np.sign(mom20.reindex(sample.index).fillna(0))
    momentum = m * gross - np.abs(m) * cost

    rng = np.random.default_rng(0)
    coin_hits = []
    for _ in range(500):
        rs = rng.choice([-1, 1], size=len(gross))
        coin_hits.append(float(((rs * gross) > 0).mean()))
    up = (p["fwd_ret"] > 0).astype(int)
    return {
        "n": int(len(p)), "since": p.index.min().date().isoformat(),
        "auc": round(float(roc_auc_score(up, p[use])), 3) if up.nunique() == 2 else None,
        "ic": round(float(p["score"].corr(p["fwd_ret"], method="spearman")), 3) if use == "prob" else None,
        "strategy": _stats(strat, h),
        "always_long": _stats(long_only, h),
        "momentum_20": _stats(momentum, h),
        "coin_hit_95": [round(float(np.percentile(coin_hits, 2.5)), 3), round(float(np.percentile(coin_hits, 97.5)), 3)],
    }


def run(preds: pd.DataFrame, conts: dict[str, pd.DataFrame], moms: dict[str, pd.Series],
        fees: dict[str, float], horizons) -> dict:
    out = {}
    for h in horizons:
        out[h] = {}
        for code in conts:
            res = run_product(code, h, preds, conts[code], moms[code], fees.get(code))
            if res.get("strategy"):
                gbm = run_product(code, h, preds, conts[code], moms[code], fees.get(code), use="gbm_prob")
                res["gbm"] = {"auc": gbm.get("auc"), "strategy": gbm.get("strategy")}
            out[h][code] = res
    return out
