"""主力连续合约。

- 主力：前一交易日持仓量最大的合约（只往远月切，不切回近月）。用前一天的持仓量决定今天的主力，避免偷看当天收盘。
- 复权：每天的涨跌幅都用"同一个合约"前后两天的价格计算，再连乘成连续序列（比例复权），换月那天不会出现假跳空。
  原始价格保留用于展示；收益、动量等计算一律用复权价。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .universe import contract_expiry_month


def _expiry_key(c: str) -> int:
    y, m = contract_expiry_month(c)
    return y * 12 + m


def build_continuous(bars: pd.DataFrame) -> pd.DataFrame:
    """bars：单个品种全部合约的日线（contract, date, open, high, low, close, volume, oi, settle）。"""
    bars = bars[(bars["close"] > 0) & bars["close"].notna()]
    fields = {f: bars.pivot_table(index="date", columns="contract", values=f, aggfunc="last")
              for f in ("open", "high", "low", "close", "volume", "oi", "settle")}
    oi = fields["oi"].fillna(0)
    close = fields["close"]
    dates = close.index.sort_values()
    contracts = sorted(close.columns, key=_expiry_key)
    oi = oi.reindex(index=dates, columns=contracts).fillna(0)
    close = close.reindex(index=dates, columns=contracts)
    traded = close.notna()

    main: list[str] = []
    current = None
    for i, d in enumerate(dates):
        ref = oi.iloc[i - 1] if i > 0 else oi.iloc[0]
        if current is None:
            current = ref.idxmax()
        candidates = [c for c in contracts if traded.at[d, c] and _expiry_key(c) >= _expiry_key(current)]
        if not candidates:  # 极少见：当天没有可用合约
            main.append(current)
            continue
        best = max(candidates, key=lambda c: ref[c])
        if not traded.at[d, current] or ref[best] > ref[current]:
            current = best
        main.append(current)

    main_s = pd.Series(main, index=dates)
    idx = np.arange(len(dates))
    col = {c: k for k, c in enumerate(contracts)}
    ci = main_s.map(col).to_numpy()

    def pick(field: str) -> np.ndarray:
        arr = fields[field].reindex(index=dates, columns=contracts).to_numpy(dtype=float)
        return arr[idx, ci]

    raw = {f: pick(f) for f in ("open", "high", "low", "close", "volume", "oi", "settle")}
    close_arr = close.to_numpy(dtype=float)
    prev_same = np.full(len(dates), np.nan)
    prev_same[1:] = close_arr[idx[1:] - 1, ci[1:]]   # 今天主力合约在昨天的收盘价
    ret = raw["close"] / prev_same - 1
    ret[0] = 0.0
    ret = np.where(np.isfinite(ret), ret, 0.0)
    growth = np.cumprod(1 + ret)
    adj_close = growth / growth[-1] * raw["close"][-1]
    factor = adj_close / raw["close"]

    out = pd.DataFrame({
        "contract": main_s.to_numpy(),
        **{f: raw[f] for f in raw},
        "adj_open": raw["open"] * factor, "adj_high": raw["high"] * factor,
        "adj_low": raw["low"] * factor, "adj_close": adj_close,
        "ret": ret,
        "total_oi": oi.sum(axis=1).to_numpy(),
        "total_volume": fields["volume"].reindex(index=dates).sum(axis=1).to_numpy(),
    }, index=dates)
    out["roll_day"] = out["contract"] != out["contract"].shift(1)
    out.iloc[0, out.columns.get_loc("roll_day")] = False

    # 次主力（远一个、有一定持仓的合约），用于期限结构和"换月期"判断
    nxt, nxt_close, nxt_ratio = [], [], []
    for i, d in enumerate(dates):
        m = main[i]
        later = [c for c in contracts if _expiry_key(c) > _expiry_key(m) and traded.at[d, c] and oi.at[d, c] > 0]
        if later:
            c = max(later[:3], key=lambda x: oi.at[d, x])   # 只看紧随其后的几个月份里持仓最大的
            nxt.append(c)
            nxt_close.append(close.at[d, c])
            nxt_ratio.append(oi.at[d, c] / max(oi.at[d, m], 1))
        else:
            nxt.append(None)
            nxt_close.append(np.nan)
            nxt_ratio.append(np.nan)
    out["next_contract"] = nxt
    out["next_close"] = nxt_close
    out["next_oi_ratio"] = nxt_ratio
    out["months_to_next"] = [(_expiry_key(n) - _expiry_key(m)) if n else np.nan for m, n in zip(main, nxt)]

    days_since_roll = np.zeros(len(dates), dtype=int)
    for i in range(1, len(dates)):
        days_since_roll[i] = 0 if out["roll_day"].iat[i] else days_since_roll[i - 1] + 1
    out["days_since_roll"] = days_since_roll
    # 有色金属几乎每月换一次主力，属于常态；只在刚换月或即将换月（次主力持仓逼近主力）时标记
    out["near_roll"] = (out["days_since_roll"] < 2) | (out["next_oi_ratio"] >= 0.85)
    out.index.name = "date"
    return out
