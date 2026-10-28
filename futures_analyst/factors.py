"""因子：每个品种每天一行。

约定：
- 只用信号日（北京时间 16:30）之前能拿到的数据。外盘和汇率用严格早于信号日的最后一个值（伦敦当天还没收盘）。
- 因子原始值先算出来，再在该品种自己的历史里做滚动标准化（250 日，至少 120 日），截断在 ±3。
  仓单这类有季节性的因子，和往年同期比（季节性 z 值）。
- 方向不预设：模型用滚动样本外 IC 决定权重的正负；registry 里的 expected 只是文献里的常见方向，用来写解释。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .universe import PRODUCTS, SPREADS

TRADING_DAYS = 242
Z_WINDOW, Z_MIN = 250, 120


@dataclass(frozen=True)
class FactorDef:
    name: str
    label: str
    group: str
    desc: str
    expected: int = 0          # 文献中的常见方向：+1 越大越偏多，-1 越大越偏空，0 不确定
    in_model: bool = True
    seasonal: bool = False


REGISTRY: list[FactorDef] = [
    FactorDef("mom_5", "5 日动量", "趋势", "近 5 个交易日的复权涨跌幅", +1),
    FactorDef("mom_20", "20 日动量", "趋势", "近 20 个交易日的复权涨跌幅", +1),
    FactorDef("mom_60", "60 日动量", "趋势", "近 60 个交易日的复权涨跌幅", +1),
    FactorDef("ma_slope", "均线斜率", "趋势", "20 日均线相对 5 天前的变化", +1),
    FactorDef("breakout", "通道位置", "趋势", "收盘价在近 55 日高低区间里的位置（-0.5 最低，+0.5 最高）", +1),
    FactorDef("roll_yield", "展期收益", "期限与基差", "主力相对次主力的年化价差；正值为近月升水（现货偏紧）", +1),
    FactorDef("basis_rate", "基差率", "期限与基差", "（现货 − 主力）/ 主力；现货升水为正", +1),
    FactorDef("basis_mom", "基差变化", "期限与基差", "基差率相对 20 个交易日前的变化", +1),
    FactorDef("receipts_chg", "仓单变化", "库存", "上期所注册仓单 20 日对数变化（与往年同期比）", -1, seasonal=True),
    FactorDef("oi_chg_20", "持仓量变化", "持仓", "全部合约持仓量 20 日对数变化", 0),
    FactorDef("oi_confirmed_mom", "增仓动量", "持仓", "5 日涨跌幅，只在持仓量同时增加时计入（价涨仓增 / 价跌仓增）", +1),
    FactorDef("net_long", "前 20 名净多", "持仓", "前 20 名会员（多 − 空）/（多 + 空）", +1),
    FactorDef("net_long_chg", "净多变化", "持仓", "前 20 名净多比例的 5 日变化", +1),
    FactorDef("clv_flow", "主动买卖（近似）", "内外盘", "收盘在当日高低区间的位置 × 成交量，20 日累计；正值表示收盘多在高位", +1),
    FactorDef("ext_ratio", "内外比价偏离", "内外盘", "国内价格 ÷（外盘 × 汇率）的对数，相对自身历史的偏离", -1),
    FactorDef("ext_lead", "外盘领先", "内外盘", "外盘近 5 日涨幅减国内近 5 日涨幅；正值表示外盘涨得更多", +1),
    FactorDef("spread_rb", "螺纹盘面利润", "产业链", SPREADS[0].note, 0),
    FactorDef("spread_hc", "热卷盘面利润", "产业链", SPREADS[1].note, 0),
    FactorDef("spread_coke", "焦化盘面利润", "产业链", SPREADS[2].note, 0),
    FactorDef("rv_20", "20 日波动率", "波动", "日收益率的年化标准差", 0, in_model=False),
    FactorDef("skew_60", "60 日偏度", "波动", "日收益率分布的偏度；文献中负偏度后续收益偏高", -1),
    FactorDef("overreaction", "过度反应", "行为金融", "5 日涨跌超过 1.5 倍正常波动后的反向信号（只在极端时不为零）", +1),
    FactorDef("cgo", "浮盈盘（处置效应）", "行为金融",
              "现价相对按成交换手估算的平均持仓成本；正值表示多数多头浮盈", +1),
    FactorDef("anchor_high", "距一年高点", "行为金融", "现价相对近 250 日最高价（锚定效应：接近高点时常延续）", +1),
    FactorDef("anchor_low", "距一年低点", "行为金融", "现价相对近 250 日最低价", 0),
    FactorDef("crowding", "拥挤度", "行为金融", "持仓量所处历史分位 × 前 20 名净多方向；越大表示多头越拥挤", -1),
    FactorDef("sentiment", "消息情绪", "消息", "消息汇总的利多/利空净值（-1~1）；没有历史可回测，暂不计入打分", +1,
              in_model=False),
]
FACTORS = {f.name: f for f in REGISTRY}
MODEL_FACTORS = [f.name for f in REGISTRY if f.in_model]
SPREAD_FACTOR = {"螺纹盘面利润": "spread_rb", "热卷盘面利润": "spread_hc", "焦化盘面利润": "spread_coke"}


def _log_chg(s: pd.Series, n: int) -> pd.Series:
    return np.log(s / s.shift(n))


def rolling_z(x: pd.Series) -> pd.Series:
    m = x.rolling(Z_WINDOW, min_periods=Z_MIN).mean()
    sd = x.rolling(Z_WINDOW, min_periods=Z_MIN).std()
    return ((x - m) / sd.replace(0, np.nan)).clip(-3, 3)


def seasonal_z(x: pd.Series, window_days: int = 20, min_years: int = 2) -> pd.Series:
    """和往年同期（前后 window_days 天）的分布比；往年数据不够时退回普通滚动 z。"""
    x = x.dropna()
    out = pd.Series(np.nan, index=x.index)
    doy = x.index.day_of_year.to_numpy()
    years = x.index.year.to_numpy()
    vals = x.to_numpy()
    for i in range(len(x)):
        mask = (years < years[i]) & (np.abs(((doy - doy[i] + 183) % 366) - 183) <= window_days)
        if len(set(years[mask])) >= min_years and mask.sum() >= 20:
            ref = vals[mask]
            sd = ref.std()
            if sd > 0:
                out.iat[i] = (vals[i] - ref.mean()) / sd
    fallback = rolling_z(x)
    return out.fillna(fallback).clip(-3, 3)


def _asof_before(series: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """每个信号日取严格早于它的最后一个值（外盘、汇率）。"""
    s = series.dropna().sort_index()
    left = pd.DataFrame({"date": dates})
    right = pd.DataFrame({"src": s.index, "v": s.to_numpy()})
    m = pd.merge_asof(left, right, left_on="date", right_on="src", allow_exact_matches=False)
    return pd.Series(m["v"].to_numpy(), index=dates)


def _asof(series: pd.Series, dates: pd.DatetimeIndex, max_gap_days: int = 10) -> pd.Series:
    s = series.dropna().sort_index()
    left = pd.DataFrame({"date": dates})
    right = pd.DataFrame({"src": s.index, "v": s.to_numpy()})
    m = pd.merge_asof(left, right, left_on="date", right_on="src")
    stale = (m["date"] - m["src"]).dt.days > max_gap_days
    m.loc[stale, "v"] = np.nan
    return pd.Series(m["v"].to_numpy(), index=dates)


def compute_raw(code: str, conts: dict[str, pd.DataFrame], ranks: pd.DataFrame, receipts: pd.DataFrame,
                basis: pd.DataFrame, foreign: pd.DataFrame, fx: pd.Series, sentiment: pd.Series | None = None
                ) -> pd.DataFrame:
    """某品种的原始因子值（未标准化）。conts 是全部品种的主力连续（产业链价差要用）。"""
    p = PRODUCTS[code]
    c = conts[code]
    d = c.index
    px = c["adj_close"]
    logret = np.log(px / px.shift(1))
    f = pd.DataFrame(index=d)

    f["mom_5"] = _log_chg(px, 5)
    f["mom_20"] = _log_chg(px, 20)
    f["mom_60"] = _log_chg(px, 60)
    ma20 = px.rolling(20).mean()
    f["ma_slope"] = _log_chg(ma20, 5)
    hi55, lo55 = c["adj_high"].rolling(55).max(), c["adj_low"].rolling(55).min()
    f["breakout"] = (px - lo55) / (hi55 - lo55).replace(0, np.nan) - 0.5

    f["roll_yield"] = np.log(c["close"] / c["next_close"]) * 12 / c["months_to_next"]
    if not basis.empty:
        b = basis.set_index("date")["dom_basis_rate"].sort_index()
        f["basis_rate"] = _asof(b, d)
        f["basis_mom"] = f["basis_rate"] - f["basis_rate"].shift(20)

    if not receipts.empty:
        r = _asof(receipts.set_index("date")["qty"].sort_index(), d)
        f["receipts_chg"] = np.log1p(r) - np.log1p(r.shift(20))

    oi = c["total_oi"].replace(0, np.nan)
    f["oi_chg_20"] = _log_chg(oi, 20)
    oi5 = _log_chg(oi, 5)
    f["oi_confirmed_mom"] = f["mom_5"].where(oi5 > 0, 0.0)

    if not ranks.empty:
        rk = ranks.set_index("date").sort_index()
        lng, sht = _asof(rk["long20"], d, 5), _asof(rk["short20"], d, 5)
        net = (lng - sht) / (lng + sht)
        f["net_long"] = net
        f["net_long_chg"] = net - net.shift(5)
        ref_oi = np.where(rk["scope"].reindex(d, method="ffill").fillna("variety") == "variety", c["total_oi"], c["oi"])
        oi_pct = c["total_oi"].rolling(250, min_periods=120).rank(pct=True)
        f["crowding"] = oi_pct * np.sign(net)
        f["_concentration"] = (lng + sht) / (2 * pd.Series(ref_oi, index=d).replace(0, np.nan))

    rng = (c["high"] - c["low"]).replace(0, np.nan)
    clv = ((c["close"] - c["low"]) - (c["high"] - c["close"])) / rng
    vol = c["total_volume"]
    f["clv_flow"] = (clv.fillna(0) * vol).rolling(20).sum() / vol.rolling(20).sum()

    if p.foreign and not foreign.empty:
        fc = foreign[foreign["symbol"] == p.foreign].set_index("date")["close"].sort_index()
        f_prev = _asof_before(fc, d)
        fx_prev = _asof_before(fx, d)
        f["ext_ratio"] = np.log(c["close"] / (f_prev * fx_prev))
        f["_ext_close"] = f_prev
        f["ext_lead"] = np.log(f_prev / f_prev.shift(5)) - _log_chg(px, 5).shift(1)

    for sp in SPREADS:
        if code not in sp.applies_to or not all(k in conts for k in sp.legs):
            continue
        value = -sp.const
        for leg, coef in sp.legs.items():
            value = value + coef * conts[leg]["close"].reindex(d)
        f[SPREAD_FACTOR[sp.name]] = value

    f["rv_20"] = logret.rolling(20).std() * np.sqrt(TRADING_DAYS)
    f["skew_60"] = logret.rolling(60).skew()
    x = f["mom_5"] / (f["rv_20"] * np.sqrt(5 / TRADING_DAYS))
    f["overreaction"] = -np.sign(x) * (x.abs() - 1.5).clip(lower=0)

    typical = (c["adj_high"] + c["adj_low"] + c["adj_close"]) / 3
    turnover = (0.1 * c["total_volume"] / oi).clip(0.01, 0.3).fillna(0.05).to_numpy()
    ref = np.empty(len(d))
    ref[0] = typical.iat[0]
    tp = typical.to_numpy()
    for i in range(1, len(d)):
        ref[i] = (1 - turnover[i]) * ref[i - 1] + turnover[i] * tp[i]
    f["cgo"] = np.log(px / ref)
    f["anchor_high"] = np.log(px / c["adj_high"].rolling(250, min_periods=120).max())
    f["anchor_low"] = np.log(px / c["adj_low"].rolling(250, min_periods=120).min())

    if sentiment is not None and not sentiment.empty:
        f["sentiment"] = _asof(sentiment, d, 3)
    return f


def expected_factors(code: str) -> list[str]:
    """按设计这个品种应该有的计分因子（用来判断"数据是否齐全"，结构上没有的不算缺）。"""
    p = PRODUCTS[code]
    out = []
    for f in MODEL_FACTORS:
        if f == "receipts_chg" and not p.receipt_keys:
            continue
        if f in ("ext_ratio", "ext_lead") and not p.foreign:
            continue
        if f.startswith("spread_"):
            sp = next(s for s in SPREADS if SPREAD_FACTOR[s.name] == f)
            if code not in sp.applies_to:
                continue
        out.append(f)
    return out


def standardize(raw: pd.DataFrame) -> pd.DataFrame:
    z = pd.DataFrame(index=raw.index)
    for f in REGISTRY:
        if f.name not in raw:
            continue
        if f.name in ("overreaction", "sentiment"):   # 本身已经是标准化的量
            z[f.name] = raw[f.name].clip(-3, 3)
        elif f.seasonal:
            z[f.name] = seasonal_z(raw[f.name]).reindex(raw.index)
        else:
            z[f.name] = rolling_z(raw[f.name])
    return z


def quadrant(mom5: float, oi5: float) -> str:
    """量价仓四象限（展示用）。"""
    if np.isnan(mom5) or np.isnan(oi5):
        return "—"
    if mom5 >= 0:
        return "价涨仓增（多头主动）" if oi5 >= 0 else "价涨仓减（空头回补）"
    return "价跌仓增（空头主动）" if oi5 >= 0 else "价跌仓减（多头离场）"
