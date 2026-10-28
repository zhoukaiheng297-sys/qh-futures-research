"""前瞻检验：主模型和一个事先约定的对照模型，从 FORWARD_START 起逐日记下预测，只用之后的新数据比较。

为什么要前瞻：对照模型的因子是看了 2019~2026 的历史表现之后挑的（基差、持仓、内外盘几类平均略好于随机），
再用同一段历史回测它，结果必然偏好看。只有挑选之后才出现的数据，才能公平地比较两个模型。

事先约定（2026-10-28 定下，之后不再修改；想换因子就另起一个模型名、重新开始计时）：
- 对照模型 B：只用"期限与基差""持仓""内外盘"三类共 10 个因子；训练窗口、权重、校准等方法和主模型 A 完全一样
- 记录：每次运行把信号日 ≥ FORWARD_START 的两个模型 5/10/20 日上涨概率写进 forward_log，已经记过的不覆盖
- 结果：和训练目标一样，信号日后第一个交易日开盘（夜盘）进场、持有到第 h 个交易日收盘，涨了算"上涨"
- 判定：满 TARGET_DAYS 个信号日后看 5 日和 10 日，B 的 AUC 比 A 高出两个标准误以上，才考虑改用 B；否则维持 A
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .config import now
from .db import DB
from .factors import REGISTRY
from .model import HORIZONS, forward_return
from .reliability import LONG_TH, SHORT_TH

FORWARD_START = "2026-10-28"
TARGET_DAYS = 250          # 约一年
B_GROUPS = ("期限与基差", "持仓", "内外盘")
B_FACTORS = [f.name for f in REGISTRY if f.in_model and f.group in B_GROUPS]
MODELS = {"A": "主模型：全部 25 个因子",
          "B": "对照模型：期限与基差、持仓、内外盘 3 类 10 个因子"}
RULE = (f"事先约定（2026-10-28）：满 {TARGET_DAYS} 个信号日后看 5 日和 10 日，对照模型的 AUC 比主模型高出两个标准误以上，"
        "才考虑改用对照模型；否则维持主模型。约定之后不再修改，想换因子就另起一个模型、重新计时。")
COLS = ["model", "product", "horizon", "signal_day", "contract", "prob", "score", "logged_at"]


def record(db: DB, preds_by_model: dict[str, pd.DataFrame], conts: dict, signal_day) -> int:
    """把 FORWARD_START 到信号日之间、还没记过的预测写进 forward_log，返回新增条数。

    滚动训练给每一天的打分只用当时已知的数据，所以某天批处理没跑成，下次补记的值和当天会算出的一样；
    logged_at 记下实际写入的时间，补记的可以看出来。已经记过的永远不改。
    """
    at = now().isoformat(timespec="seconds")
    rows = []
    for m, preds in preds_by_model.items():
        p = preds[preds["prob"].notna()]
        d = pd.to_datetime(p["date"])
        p = p[(d >= pd.Timestamp(FORWARD_START)) & (d <= pd.Timestamp(signal_day))]
        for r in p.itertuples():
            day = pd.Timestamp(r.date)
            cont = conts.get(r.product)
            contract = cont["contract"].get(day) if cont is not None and "contract" in cont else None
            rows.append((m, r.product, int(r.horizon), day.date().isoformat(), contract, float(r.prob),
                         None if pd.isna(r.score) else float(r.score), at))
    before = db.execute("SELECT COUNT(*) FROM forward_log")[0][0]
    with db._lock, db.conn:
        db.conn.executemany(f"INSERT OR IGNORE INTO forward_log ({', '.join(COLS)}) "
                            f"VALUES ({', '.join('?' * len(COLS))})", rows)
    return db.execute("SELECT COUNT(*) FROM forward_log")[0][0] - before


def _auc(prob: np.ndarray, up: np.ndarray) -> float | None:
    pos, neg = int(up.sum()), int((~up).sum())
    if pos == 0 or neg == 0:
        return None
    r = pd.Series(prob).rank().to_numpy()
    return float((r[up].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _stats(g: pd.DataFrame, h: int) -> dict:
    n = len(g)
    n_eff = n / h          # 同一品种相邻几天的持有期重叠，有效样本约为 n/h
    up = (g["y"] > 0).to_numpy()
    active = g[(g["prob"] >= LONG_TH) | (g["prob"] <= SHORT_TH)]
    hit = (np.sign(active["prob"] - 0.5) == np.sign(active["y"])).mean() if len(active) else None
    auc = _auc(g["prob"].to_numpy(), up) if n else None
    return {"matured": n, "matured_days": int(g["signal_day"].nunique()), "n_eff": round(n_eff, 1),
            "auc": None if auc is None else round(auc, 3),
            "auc_se": round(1 / np.sqrt(3 * n_eff), 3) if n_eff >= 1 else None,
            "active": int(len(active)), "hit_rate": None if hit is None else round(float(hit), 3),
            "up_share": round(float(up.mean()), 3) if n else None}


def evaluate(db: DB, conts: dict, cal=None, horizons=HORIZONS) -> dict:
    log = db.query("SELECT * FROM forward_log")
    out = {"start": FORWARD_START, "rule": RULE, "models": MODELS, "target_days": TARGET_DAYS,
           "b_factors": B_FACTORS, "logged_days": int(log["signal_day"].nunique()) if len(log) else 0,
           "horizons": {}, "latest": None, "first_results": None}
    if cal is not None:   # 第一批结果什么时候出来：第一个信号日之后第 h 个交易日收盘
        first = next(iter(cal.between(date.fromisoformat(FORWARD_START), cal.days[-1])), None)
        if first:
            out["first_results"] = {str(h): cal.shift(first, h).isoformat() for h in horizons}
    if not len(log):
        return out
    ys = {}
    for code, h in log[["product", "horizon"]].drop_duplicates().itertuples(index=False):
        if code in conts:
            y = forward_return(conts[code], int(h))
            ys[(code, int(h))] = dict(zip(y.index.strftime("%Y-%m-%d"), y.to_numpy()))
    log["y"] = [ys.get((r.product, int(r.horizon)), {}).get(r.signal_day, np.nan) for r in log.itertuples()]
    for h in horizons:
        res = {m: _stats(log[(log["model"] == m) & (log["horizon"] == h) & log["y"].notna()], h) for m in MODELS}
        a, b = res["A"], res["B"]
        if a["auc"] is not None and b["auc"] is not None and a["auc_se"] and b["auc_se"]:
            res["diff"] = round(b["auc"] - a["auc"], 3)
            res["diff_se"] = round(float(np.hypot(a["auc_se"], b["auc_se"])), 3)   # 偏保守：没算两者的相关
        out["horizons"][str(h)] = res
    last = log["signal_day"].max()
    today = log[log["signal_day"] == last]
    out["latest"] = {"signal_day": last, "probs": {
        code: {str(int(h)): {m: round(float(p), 3) for m, p in zip(gg["model"], gg["prob"])}
               for h, gg in g.groupby("horizon")} for code, g in today.groupby("product")}}
    return out
