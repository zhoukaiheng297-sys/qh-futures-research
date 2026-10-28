"""可靠性：每条信号都附上"凭什么信它"的统计依据，并用确定性规则给出可信度。

全部统计只用样本外预测（walk_forward 产出），且只用当时已经知道结果的样本。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .factors import FACTORS, MODEL_FACTORS

ACTIVE = 0.05          # |概率 − 0.5| 至少这么大才算"有方向"
LONG_TH, SHORT_TH = 0.55, 0.45


def direction(prob: float | None, score: float | None) -> str:
    if prob is not None and np.isfinite(prob):
        if prob >= LONG_TH:
            return "偏多"
        if prob <= SHORT_TH:
            return "偏空"
        return "中性"
    if score is not None and np.isfinite(score):
        return "偏多（未校准）" if score > 0.15 else "偏空（未校准）" if score < -0.15 else "中性"
    return "数据不足"


def model_skill(p: pd.DataFrame, h: int) -> dict:
    """p：某品种某周期的样本外预测（prob、score、fwd_ret）。

    关键指标是"年内 AUC"：每个自然年内分别算 AUC 再按样本数平均。全样本 AUC 会把"校准跟上了这几年
    整体是涨还是跌"也算成本事，年内 AUC 只衡量在同一段行情里分辨涨跌的能力。
    """
    s = p[p["prob"].notna() & p["fwd_ret"].notna()].copy()
    out = {"n": int(len(s))}
    if len(s) < 100:
        return out
    s["up"] = (s["fwd_ret"] > 0).astype(int)
    s["year"] = pd.to_datetime(s["date"]).dt.year
    aucs, ics, ns = [], [], []
    for _, g in s.groupby("year"):
        if len(g) >= 60 and g["up"].nunique() == 2:
            aucs.append(roc_auc_score(g["up"], g["prob"]))
            ics.append(g["score"].corr(g["fwd_ret"], method="spearman"))
            ns.append(len(g))
    active = s[(s["prob"] - 0.5).abs() >= ACTIVE]
    hit = float((np.sign(active["prob"] - 0.5) == np.sign(active["fwd_ret"])).mean()) if len(active) else np.nan
    # 同样这些天"一直押多数方向"的胜率：衡量胜率里有多少只是趋势本身
    naive = float(max(active["fwd_ret"].gt(0).mean(), active["fwd_ret"].lt(0).mean())) if len(active) else np.nan
    n_eff = max(len(s) / h, 1)
    out.update(
        n_active=int(len(active)),
        hit_rate=round(hit, 3) if np.isfinite(hit) else None,
        naive_hit=round(naive, 3) if np.isfinite(naive) else None,
        auc=round(float(roc_auc_score(s["up"], s["prob"])), 3) if s["up"].nunique() == 2 else None,
        auc_within=round(float(np.average(aucs, weights=ns)), 3) if aucs else None,
        auc_se=round(float(1 / np.sqrt(3 * n_eff)), 3),   # 随机猜测时 AUC 的标准误（按不重叠样本数）
        ic_within=round(float(np.average(ics, weights=ns)), 3) if ics else None,
        base_up=round(float(s["up"].mean()), 3),
        recent_up=round(float(s["up"].iloc[-750:].mean()), 3),
        since=pd.to_datetime(s["date"]).min().date().isoformat(),
    )
    return out


def factor_stats(z: pd.DataFrame, y: pd.Series, weight_hist: pd.DataFrame | None, h: int) -> dict:
    """每个因子的样本外表现：按当时的权重符号给方向，在 |z| ≥ 0.5 的日子里统计胜率。"""
    out = {}
    if weight_hist is None or weight_hist.empty:
        return out
    w_daily = weight_hist.reindex(z.index, method="ffill")
    start = weight_hist.index.min()
    for f in MODEL_FACTORS:
        if f not in z or f not in w_daily:
            continue
        sel = (z.index >= start) & z[f].notna() & y.notna() & w_daily[f].notna()
        zz, yy, ww = z.loc[sel, f], y[sel], w_daily.loc[sel, f]
        strong = zz.abs() >= 0.5
        pred = np.sign(zz[strong] * ww[strong])
        valid = pred != 0
        n = int(valid.sum())
        hit = float((pred[valid] == np.sign(yy[strong][valid])).mean()) if n else np.nan
        ic = float(zz.corr(yy, method="spearman")) if len(zz) > 50 else np.nan
        out[f] = {"n": n, "hit_rate": round(hit, 3) if np.isfinite(hit) else None,
                  "ic": round(ic, 3) if np.isfinite(ic) else None, "n_eff": int(n / h)}
    return out


def calibration_bucket(sector_preds: pd.DataFrame, prob: float) -> dict | None:
    s = sector_preds[sector_preds["prob"].notna() & sector_preds["fwd_ret"].notna()]
    if not np.isfinite(prob) or len(s) < 200:
        return None
    edges = [0, 0.40, 0.45, 0.50, 0.55, 0.60, 1.0001]
    k = np.searchsorted(edges, prob, side="right") - 1
    lo, hi = edges[k], min(edges[k + 1], 1.0)
    b = s[(s["prob"] >= lo) & (s["prob"] < edges[k + 1])]
    if len(b) < 30:
        return {"range": [lo, hi], "n": int(len(b))}
    return {"range": [lo, round(hi, 2)], "n": int(len(b)), "up_share": round(float((b["fwd_ret"] > 0).mean()), 3),
            "median_ret": round(float(b["fwd_ret"].median()), 4)}


def analogs(z: pd.DataFrame, y: pd.Series, today_idx: int, h: int, k: int = 30) -> dict | None:
    """因子空间里和今天最像的 k 段历史（结果已知），它们之后 h 日的表现。

    相邻几天的因子几乎一样，直接取最近邻会挑出同一段行情的连续多天、夸大样本数，
    所以要求选中的日子彼此至少相隔 h 个交易日。
    """
    cols = [f for f in MODEL_FACTORS if f in z]
    hist_end = today_idx - h - 1
    if hist_end < 250:
        return None
    X = z[cols].iloc[:hist_end].fillna(0).to_numpy()
    yy = y.iloc[:hist_end].to_numpy()
    pos = np.arange(hist_end)
    ok = np.isfinite(yy) & (np.abs(X).sum(axis=1) > 0)
    X, yy, pos, dates = X[ok], yy[ok], pos[ok], z.index[:hist_end][ok]
    x0 = z[cols].iloc[today_idx].fillna(0).to_numpy()
    dist = np.sqrt(((X - x0) ** 2).sum(axis=1))
    picked: list[int] = []
    for j in np.argsort(dist):
        if all(abs(pos[j] - pos[q]) >= h for q in picked):
            picked.append(j)
            if len(picked) == k:
                break
    r = yy[picked]
    return {"k": int(len(picked)), "up_share": round(float((r > 0).mean()), 3),
            "median_ret": round(float(np.median(r)), 4), "p25": round(float(np.percentile(r, 25)), 4),
            "p75": round(float(np.percentile(r, 75)), 4), "years": sorted({d.year for d in dates[picked]})}


@dataclass
class Reliability:
    direction: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def judge(prob: float, score: float, skill: dict, agreement: float | None, bucket: dict | None,
          flags: list[tuple[str, bool]]) -> Reliability:
    """确定性规则：先看这个品种这个周期历史上有没有优势，再看信号强度、因子一致性、同档位命中率，最后按降级条件下调。"""
    d = direction(prob, score)
    reasons, downgrades = [], []
    tiers = ["低", "中", "高"]
    if not np.isfinite(prob):
        return Reliability(d, "低", ["样本外历史还不够长，概率没有校准"], [])

    strength = abs(prob - 0.5)
    level = 2 if strength >= 0.08 else 1 if strength >= ACTIVE else 0
    reasons.append(f"校准后上涨概率 {prob:.0%}，偏离 50% {strength * 100:.0f} 个百分点")

    auc_w, se = skill.get("auc_within"), skill.get("auc_se")
    hit, naive = skill.get("hit_rate"), skill.get("naive_hit")
    if auc_w is None:
        level = 0
        reasons.append("样本外历史太短，无法评估区分能力")
    else:
        edge = auc_w - 0.5
        reasons.append(f"样本外（{skill['since']} 起）年内 AUC {auc_w:.2f}；随机猜测约 0.50，标准误 ±{se:.2f}")
        if hit is not None and naive is not None:
            reasons.append(f"有方向信号 {skill['n_active']} 次，胜率 {hit:.0%}；同期一直押多数方向的胜率 {naive:.0%}")
        if edge < se:
            level = 0
            reasons.append("历史上在同一段行情里分辨涨跌的能力和随机猜测差不多，这个结论只供参考")
        elif edge < 2 * se:
            level = min(level, 1)
    recent = skill.get("recent_up")
    if recent is not None and abs(prob - recent) < 0.02:
        level = 0
        reasons.append(f"概率和近 3 年 {recent:.0%} 的上涨比例几乎一样，主要反映长期趋势，因子没有提供额外信息")

    if agreement is not None:
        reasons.append(f"按贡献加权，{agreement:.0%} 的因子与结论同向")
        if agreement < 0.6:
            level = min(level, 1)

    if bucket and bucket.get("up_share") is not None:
        same_dir = bucket["up_share"] if prob >= 0.5 else 1 - bucket["up_share"]
        reasons.append(f"同板块历史上概率落在 {bucket['range'][0]:.0%}~{bucket['range'][1]:.0%} 的 {bucket['n']} 次里，"
                       f"实际{'上涨' if prob >= 0.5 else '下跌'} {same_dir:.0%}")
        if same_dir < 0.52:
            level = min(level, 0)

    for text, severe in flags:
        downgrades.append(text)
        level = 0 if severe else max(level - 1, 0)

    if d == "中性":
        level = min(level, 1)
    return Reliability(d, tiers[level], reasons, downgrades)


def agreement_of(contrib: pd.Series) -> float | None:
    if contrib.empty or contrib.sum() == 0:
        return None
    total = contrib.abs().sum()
    same = contrib[np.sign(contrib) == np.sign(contrib.sum())].abs().sum()
    return float(same / total) if total > 0 else None


def factor_label(name: str) -> str:
    return FACTORS[name].label if name in FACTORS else name
