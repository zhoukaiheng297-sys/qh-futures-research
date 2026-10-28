"""核心计算的离线测试（构造数据，不联网）：主力连续、因子、防偷看未来、滚动训练、可靠性规则、回测。

    .venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from futures_analyst import model as M
from futures_analyst.continuous import build_continuous
from futures_analyst.factors import compute_raw, rolling_z, seasonal_z, standardize
from futures_analyst.reliability import analogs, judge, model_skill


def two_contract_bars(n=60, roll_at=30, gap=100.0):
    """两个合约：远月比近月贵 gap 元；第 roll_at 天起远月持仓超过近月。"""
    dates = pd.bdate_range("2024-01-01", periods=n)
    base = 1000 + np.cumsum(np.random.default_rng(0).normal(0, 5, n))
    rows = []
    for i, d in enumerate(dates):
        near_oi = 1000 if i < roll_at else 300
        far_oi = 500 if i < roll_at else 1200
        for c, px, oi in (("XX2403", base[i], near_oi), ("XX2405", base[i] + gap, far_oi)):
            rows.append((c, "XX", d, px, px + 5, px - 5, px, 100.0, oi, px))
    return pd.DataFrame(rows, columns=["contract", "product", "date", "open", "high", "low", "close", "volume", "oi",
                                       "settle"])


def test_continuous_switches_one_day_late_without_fake_jump():
    bars = two_contract_bars()
    c = build_continuous(bars)
    dates = c.index
    # 第 30 天收盘时远月持仓才超过近月，所以第 31 天起才算主力（不用当天收盘的信息）
    assert c["contract"].iloc[30] == "XX2403"
    assert c["contract"].iloc[31] == "XX2405"
    assert bool(c["roll_day"].iloc[31])
    # 原始价格在换月日跳 +100，复权收益不应该有这个跳空
    raw_jump = c["close"].iloc[31] / c["close"].iloc[30] - 1
    assert raw_jump > 0.08
    assert abs(c["ret"].iloc[31]) < 0.03
    # 复权价最后一天等于原始价
    assert c["adj_close"].iloc[-1] == pytest.approx(c["close"].iloc[-1])
    assert len(dates) == 60


def test_rolling_and_seasonal_z():
    x = pd.Series(np.arange(300, dtype=float), index=pd.bdate_range("2020-01-01", periods=300))
    z = rolling_z(x)
    assert z.iloc[:119].isna().all() and z.iloc[200] > 1.5
    idx = pd.date_range("2018-01-01", "2023-12-31", freq="B")
    season = pd.Series(np.sin(2 * np.pi * idx.day_of_year / 365), index=idx)
    sz = seasonal_z(season + np.random.default_rng(1).normal(0, 0.05, len(idx)))
    # 去掉季节性后，后几年的 z 值应该在 0 附近，而普通滚动 z 会跟着季节大幅摆动
    assert sz.loc["2022":].abs().median() < 1.0
    assert rolling_z(season).loc["2022":].abs().median() > 0.8


def _synthetic_conts(n=700, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-01", periods=n)
    px = 3000 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    df = pd.DataFrame({
        "contract": "RB2405", "open": px * (1 + rng.normal(0, 0.002, n)), "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": rng.integers(1000, 5000, n).astype(float), "oi": 20000.0, "settle": px,
        "total_oi": 40000 + np.cumsum(rng.normal(0, 200, n)), "total_volume": rng.integers(5000, 9000, n).astype(float),
        "next_close": px * 0.99, "months_to_next": 2.0, "roll_day": False, "near_roll": False,
    }, index=dates)
    for k in ("open", "high", "low", "close"):
        df["adj_" + k] = df[k]
    df["ret"] = df["close"].pct_change().fillna(0)
    return df


def test_factors_do_not_look_ahead():
    """把数据截断到某天重算，那天的因子值必须和用全量数据算出来的一样。"""
    full = _synthetic_conts()
    fx = pd.Series(7.1, index=full.index)
    empty = pd.DataFrame(columns=["product", "date"])
    t = full.index[500]
    a = standardize(compute_raw("RB", {"RB": full}, empty, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), fx))
    b = standardize(compute_raw("RB", {"RB": full.loc[:t]}, empty, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), fx))
    pd.testing.assert_series_equal(a.loc[t], b.loc[t], check_names=False)


def test_walk_forward_trains_only_on_known_labels():
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2019-01-01", periods=900)
    h = 10
    panel = {}
    for c in ("A", "B"):
        f = pd.Series(rng.normal(size=900), index=dates)
        y = 0.3 * f + rng.normal(size=900)       # 因子真的有预测力
        panel[c] = pd.DataFrame({"mom_20": f, "y": y})
    preds, meta = M.walk_forward(panel, h, log=lambda *a: None, with_gbm=False)
    # 手工按"只用标签在重估日之前结束的样本"重算最后一次的权重，应该和模型给出的完全一致
    m = meta["weights"]["A"].index[-1]
    k = dates.searchsorted(m)
    end = k - h - 1
    lo = max(0, end - M.TRAIN_WINDOW)
    train = {c: panel[c].iloc[lo:end] for c in panel}
    pooled = pd.concat(train.values())
    expected = 0.5 * M._shrunk_ic(train["A"], ["mom_20"], h) + 0.5 * M._shrunk_ic(pooled, ["mom_20"], h)
    assert meta["weights"]["A"].loc[m, "mom_20"] == pytest.approx(expected["mom_20"])
    # 如果偷看了未来（把 end 往后挪），权重会不同
    peek = M._shrunk_ic(panel["A"].iloc[lo:end + h], ["mom_20"], h)["mom_20"]
    assert peek != pytest.approx(M._shrunk_ic(train["A"], ["mom_20"], h)["mom_20"])
    s = preds.dropna(subset=["score", "fwd_ret"])
    assert s["score"].corr(s["fwd_ret"], method="spearman") > 0.15
    assert meta["weights"]["A"]["mom_20"].iloc[-1] > 0


def test_skill_and_judge_rules():
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2020-01-01", periods=1200)
    noise = pd.DataFrame({"date": dates, "score": rng.normal(size=1200), "prob": 0.5 + rng.normal(0, 0.05, 1200),
                          "fwd_ret": rng.normal(size=1200)})
    sk = model_skill(noise, 10)
    assert abs(sk["auc_within"] - 0.5) < 0.08
    rel = judge(0.62, 0.5, sk, 0.8, None, [])
    assert rel.confidence == "低" and any("随机猜测" in r for r in rel.reasons)

    good = noise.copy()
    good["prob"] = 1 / (1 + np.exp(-good["fwd_ret"] * 2 + rng.normal(0, 0.3, 1200)))
    sk2 = model_skill(good, 10)
    assert sk2["auc_within"] > 0.8
    assert judge(0.70, 0.5, sk2, 0.8, None, []).confidence == "高"
    assert judge(0.70, 0.5, sk2, 0.8, None, [("临近重大事件", False)]).confidence == "中"
    assert judge(0.70, 0.5, sk2, 0.8, None, [("数据不是最新的", True)]).confidence == "低"
    assert judge(0.52, 0.0, sk2, 0.8, None, []).direction == "中性"


def test_analogs_are_spaced_apart():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2019-01-01", periods=800)
    z = pd.DataFrame({"mom_20": np.repeat(rng.normal(size=80), 10)}, index=idx)   # 每 10 天一样，最近邻会扎堆
    y = pd.Series(rng.normal(size=800), index=idx)
    a = analogs(z, y, 799, 10, k=20)
    assert a["k"] <= 20 and a["k"] > 5


def test_export_json_turns_nan_into_null():
    """健康页曾因 NaN 导致网页接口 500：导出时 NaN/inf（含 np.float64）必须变成 null。"""
    import json

    from futures_analyst.export import _json
    out = json.loads(_json({"a": float("nan"), "b": [np.float64("inf"), 1.5], "c": None}))
    assert out == {"a": None, "b": [None, 1.5], "c": None}


def test_freshness_flags_stale_and_missing_data(tmp_path):
    """数据过期检查：落后 3 个交易日及以上、完全没有数据、网页数据不是这次算的，都要报出来。"""
    import json
    import sqlite3

    from futures_analyst.db import DB
    from futures_analyst.freshness import check
    from futures_analyst.tradecal import Calendar
    from futures_analyst.universe import PRODUCTS

    days = [d.date() for d in pd.bdate_range("2026-10-01", "2026-10-27")]
    cal, today_, d = Calendar(days), days[-1], days[-1].isoformat()
    db = DB(tmp_path / "futures.db")
    for code, p in PRODUCTS.items():
        db.execute("INSERT INTO bars (contract, product, date) VALUES (?, ?, ?)", (code + "2612", code, d))
        db.execute("INSERT INTO ranks (product, date, scope) VALUES (?, ?, 'variety')", (code, d))
        db.execute("INSERT INTO basis (product, date) VALUES (?, ?)", (code, d))
        if p.receipt_keys:
            db.execute("INSERT INTO receipts (product, date) VALUES (?, ?)", (code, d))
        if p.foreign:
            db.execute("INSERT OR IGNORE INTO foreign_bars (symbol, date) VALUES (?, ?)", (p.foreign, days[-2].isoformat()))
    db.execute("INSERT INTO fx (date, usdcny) VALUES (?, 7.1)", (d,))
    site = tmp_path / "site.db"
    con = sqlite3.connect(site)
    with con:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('signal_day', ?)", (json.dumps(d),))
    con.close()

    rows = check(db, cal, today_, site)
    assert all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]     # 外盘落后 1 天是正常的

    db.execute("DELETE FROM ranks WHERE product = 'J' AND date = ?", (d,))
    db.execute("INSERT INTO ranks (product, date, scope) VALUES ('J', ?, 'variety')", (days[-4].isoformat(),))  # 落后 3 天
    db.execute("DELETE FROM basis WHERE product = 'CU'")                                                     # 完全没有
    bad = {(r["name"], r["group"]): r["lag"] for r in check(db, cal, today_, site) if not r["ok"]}
    assert bad == {("会员持仓排名", "J"): 3, ("现货与基差", "CU"): None}

    later = days[-1].replace(day=28)
    stale = {(r["name"], r["group"]) for r in check(db, Calendar(days + [later]), later, site) if not r["ok"]}
    assert ("网页数据", "信号日") in stale and ("合约日线", "CU") not in stale       # 落后 1 天不算过期
    assert not next(r for r in check(db, cal, today_, tmp_path / "missing.db") if r["name"] == "网页数据")["ok"]


def test_settle_quote_uses_same_contract_on_roll_day(tmp_path):
    """涨跌按行情软件口径：收盘 − 这个合约自己的昨结算。换月当天不能拿旧主力的结算价比。"""
    from datetime import date

    from futures_analyst.db import DB
    from futures_analyst.pipeline import settle_quote

    db = DB(tmp_path / "futures.db")
    cols = ["contract", "product", "date", "open", "high", "low", "close", "volume", "oi", "settle"]
    db.upsert("bars", cols, [
        ("CU2611", "CU", "2026-10-26", 0, 0, 0, 107000, 0, 900, 107100),   # 旧主力
        ("CU2612", "CU", "2026-10-26", 0, 0, 0, 108110, 0, 800, 108180),
        ("CU2612", "CU", "2026-10-27", 0, 0, 0, 108390, 0, 1000, 108640),   # 当天换到 2612
    ])
    q = settle_quote(db, "CU2612", date(2026, 10, 27))
    assert q["settle"] == 108640 and q["pre_settle"] == 108180 and q["change_pts"] == 210
    assert abs(q["change_settle"] - (108390 / 108180 - 1)) < 1e-12      # 和上期所官方 10-27 的 +0.19% 一致
    assert settle_quote(db, "CU2612", date(2026, 10, 28))["change_settle"] is None   # 当天没有行情就不显示
    assert settle_quote(db, "CU2611", date(2026, 10, 26))["pre_settle"] is None      # 没有前一天的结算价


def test_forward_log_is_append_only_and_only_counts_new_matured_data(tmp_path):
    """前瞻检验：从约定日期起才记录；已经记下的预测不会被之后的重训覆盖；只统计持有期已经结束的。"""
    from futures_analyst import forward as FW
    from futures_analyst.db import DB

    assert FW.FORWARD_START == "2026-10-28"
    assert FW.B_FACTORS == ["roll_yield", "basis_rate", "basis_mom", "oi_chg_20", "oi_confirmed_mom", "net_long",
                            "net_long_chg", "clv_flow", "ext_ratio", "ext_lead"]   # 事先约定，改了这个测试就会失败
    db = DB(tmp_path / "futures.db")
    days = pd.bdate_range("2026-10-26", periods=12)                  # 10-26 ~ 11-10，价格一路上涨
    cont = pd.DataFrame({"adj_open": np.linspace(100, 111, 12), "adj_close": np.linspace(100.5, 111.5, 12),
                         "contract": "CU2612"}, index=days)
    preds = lambda p: pd.DataFrame({"product": "CU", "horizon": 5, "date": days, "score": 0.1, "prob": p})

    assert FW.record(db, {"A": preds(0.6), "B": preds(0.4)}, {"CU": cont}, days[4].date()) == 6   # 只有 10-28~10-30
    assert FW.record(db, {"A": preds(0.9), "B": preds(0.1)}, {"CU": cont}, days[4].date()) == 0   # 重训后不覆盖
    assert db.execute("SELECT DISTINCT prob FROM forward_log WHERE model = 'A'") == [(0.6,)]
    FW.record(db, {"A": preds(0.6), "B": preds(0.4)}, {"CU": cont}, days[-1].date())

    rep = FW.evaluate(db, {"CU": cont}, horizons=(5,))
    a, b = rep["horizons"]["5"]["A"], rep["horizons"]["5"]["B"]
    assert rep["logged_days"] == 10 and rep["latest"]["signal_day"] == "2026-11-10"
    assert a["matured"] == 5 and a["hit_rate"] == 1.0 and b["hit_rate"] == 0.0   # 10-28~11-03 的 5 日结果已出
    assert a["auc"] is None and a["up_share"] == 1.0                            # 全部上涨时 AUC 无意义
