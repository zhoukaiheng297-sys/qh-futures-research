"""多因子打分：滚动样本外 IC 加权 + 保序回归校准。

每个月第一个交易日重新估计一次权重，只用当时已经知道结果的样本（标签区间结束在重估日之前），
然后用这组权重给下个月每天打分——所以历史上的每一个打分都是"样本外"的，回测不会偷看未来。

- 目标：信号日 t 之后第一个交易日开盘（夜盘）买入，持有到 t+h 日收盘的复权对数收益
- 因子权重 = 0.5 × 该品种自己的 IC + 0.5 × 板块合并 IC，都按有效样本数收缩（标签重叠，有效样本约为 n/h）
- 打分 = Σ 权重 × 因子 z 值 / Σ |权重|，大致在 -1~1
- 校准：用更早的样本外打分拟合保序回归，把打分换成"h 日后上涨的概率"
- 对照：梯度提升分类器（板块合并、每季度重训），只用于回测比较
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from .factors import MODEL_FACTORS

HORIZONS = (5, 10, 20)
# 训练窗口：只用最近约 3 年（756 个交易日）的样本估计权重。商品因子的方向会随行情阶段变化
# （2019~2021 动量有效，2022 年以后多数品种转为反转），全部历史一起估计会被早期规律拖住。
# None 表示用全部历史（扩展窗口），作为对照保留在回测里。
TRAIN_WINDOW = 756
MIN_TRAIN = 250        # 每个品种至少这么多个已知结果的样本才开始给权重
MIN_PAIRS = 120        # 单个因子至少这么多有效样本才计算 IC
SHRINK = 30            # IC 收缩：n_eff / (n_eff + SHRINK)
MIN_CALIB = 300


def forward_return(cont: pd.DataFrame, h: int) -> pd.Series:
    entry = cont["adj_open"].shift(-1)
    exit_ = cont["adj_close"].shift(-h)
    return np.log(exit_ / entry)


def _spearman(x: np.ndarray, y: np.ndarray) -> tuple[float, int]:
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    if n < MIN_PAIRS:
        return np.nan, n
    rx = pd.Series(x[m]).rank().to_numpy()
    ry = pd.Series(y[m]).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return np.nan, n
    return float(np.corrcoef(rx, ry)[0, 1]), n


def _shrunk_ic(frame: pd.DataFrame, factors: list[str], h: int) -> pd.Series:
    y = frame["y"].to_numpy()
    out = {}
    for f in factors:
        if f not in frame:
            out[f] = 0.0
            continue
        ic, n = _spearman(frame[f].to_numpy(dtype=float), y)
        n_eff = n / h
        out[f] = 0.0 if np.isnan(ic) else ic * n_eff / (n_eff + SHRINK)
    return pd.Series(out)


def score_rows(z: pd.DataFrame, w: pd.Series) -> pd.Series:
    cols = [c for c in w.index if c in z.columns and w[c] != 0]
    if not cols:
        return pd.Series(np.nan, index=z.index)
    Z = z[cols]
    W = w[cols]
    num = (Z * W).sum(axis=1, min_count=1)
    den = Z.notna().mul(W.abs()).sum(axis=1)
    return num / den.replace(0, np.nan)


def contributions(z_row: pd.Series, w: pd.Series) -> pd.Series:
    cols = [c for c in w.index if c in z_row.index and pd.notna(z_row[c]) and w[c] != 0]
    den = w[cols].abs().sum()
    return (z_row[cols] * w[cols] / den) if den > 0 else pd.Series(dtype=float)


def _refit_dates(dates: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(dates, index=dates)
    return list(s.groupby([dates.year, dates.month]).min())


def walk_forward(panel: dict[str, pd.DataFrame], h: int, log=print, window: int | None = TRAIN_WINDOW,
                 with_gbm: bool = True) -> tuple[pd.DataFrame, dict]:
    """panel[code]：标准化因子 + 'y'（h 日远期收益）。返回 (样本外预测, 权重历史)。"""
    codes = list(panel)
    all_dates = pd.DatetimeIndex(sorted(set().union(*[panel[c].index for c in codes])))
    refits = _refit_dates(all_dates)
    factors = [f for f in MODEL_FACTORS if any(f in panel[c] for c in codes)]
    weights: dict[str, dict] = {c: {} for c in codes}
    scores = {c: pd.Series(np.nan, index=panel[c].index) for c in codes}
    gbm = {c: pd.Series(np.nan, index=panel[c].index) for c in codes}
    gbm_model = None

    for i, m in enumerate(refits):
        m_next = refits[i + 1] if i + 1 < len(refits) else all_dates[-1] + pd.Timedelta(days=1)
        train = {}
        for c in codes:
            df = panel[c]
            k = df.index.searchsorted(m)
            end = k - h - 1
            if end <= 0:
                continue
            tr = df.iloc[max(0, end - window) if window else 0:end]
            tr = tr[tr["y"].notna()]
            if len(tr) >= MIN_TRAIN:
                train[c] = tr
        if not train:
            continue
        pooled = pd.concat(train.values())
        ic_sector = _shrunk_ic(pooled, factors, h)
        for c, tr in train.items():
            w = 0.5 * _shrunk_ic(tr, factors, h) + 0.5 * ic_sector
            weights[c][m] = w
            rows = panel[c].loc[(panel[c].index >= m) & (panel[c].index < m_next)]
            if len(rows):
                scores[c].loc[rows.index] = score_rows(rows, w)

        if with_gbm and (i % 3 == 0 or gbm_model is None):   # 对照模型每季度重训
            gbm_cols = [f for f in factors if f in pooled and pooled[f].notna().sum() >= 100]
            X = pooled[gbm_cols].to_numpy(dtype=float)
            yb = (pooled["y"] > 0).astype(int).to_numpy()
            if gbm_cols and len(np.unique(yb)) == 2 and len(pooled) >= 2 * MIN_TRAIN:
                gbm_model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                           min_samples_leaf=80, l2_regularization=1.0,
                                                           random_state=0).fit(X, yb)
                gbm_model.cols_ = gbm_cols
        if gbm_model is not None:
            for c in codes:
                rows = panel[c].loc[(panel[c].index >= m) & (panel[c].index < m_next)]
                if len(rows):
                    Xr = rows.reindex(columns=gbm_model.cols_).to_numpy(dtype=float)
                    gbm[c].loc[rows.index] = gbm_model.predict_proba(Xr)[:, 1]

    # 第二遍：用更早的样本外打分校准成概率
    probs = {c: pd.Series(np.nan, index=panel[c].index) for c in codes}
    calibrators = {}
    for i, m in enumerate(refits):
        m_next = refits[i + 1] if i + 1 < len(refits) else all_dates[-1] + pd.Timedelta(days=1)
        xs, ys = [], []
        for c in codes:
            df = panel[c]
            k = df.index.searchsorted(m)
            end = k - h - 1
            if end <= 0:
                continue
            lo = max(0, end - window) if window else 0
            s = scores[c].iloc[lo:end]
            y = df["y"].iloc[lo:end]
            ok = s.notna() & y.notna()
            xs.append(s[ok].to_numpy())
            ys.append((y[ok] > 0).astype(int).to_numpy())
        if not xs:
            continue
        x_all, y_all = np.concatenate(xs), np.concatenate(ys)
        if len(x_all) < MIN_CALIB or len(np.unique(y_all)) < 2:
            continue
        iso = IsotonicRegression(y_min=0.02, y_max=0.98, out_of_bounds="clip").fit(x_all, y_all)
        calibrators[m] = iso
        for c in codes:
            sel = (scores[c].index >= m) & (scores[c].index < m_next) & scores[c].notna()
            if sel.any():
                probs[c].loc[sel] = iso.predict(scores[c][sel].to_numpy())

    preds = []
    for c in codes:
        preds.append(pd.DataFrame({"product": c, "horizon": h, "date": panel[c].index, "score": scores[c].to_numpy(),
                                   "prob": probs[c].to_numpy(), "gbm_prob": gbm[c].to_numpy(),
                                   "fwd_ret": panel[c]["y"].to_numpy()}))
    preds = pd.concat(preds, ignore_index=True)
    hist = {c: pd.DataFrame(weights[c]).T.sort_index() for c in codes if weights[c]}
    last_cal = calibrators[max(calibrators)] if calibrators else None
    log(f"  h={h}（{'近 ' + str(window) + ' 个交易日' if window else '全部历史'}）：重估 {len(refits)} 次，样本外打分 {int(preds['score'].notna().sum())} 个，"
        f"已校准 {int(preds['prob'].notna().sum())} 个")
    return preds, {"weights": hist, "calibrator": last_cal}
