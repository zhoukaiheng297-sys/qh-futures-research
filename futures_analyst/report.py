"""命令行查看：某品种的最新信号，以及回测汇总。读取 site.db（先运行 update 或 train）。"""
from __future__ import annotations

import json
import sqlite3
import sys

from .config import SITE_DB_PATH


def _site() -> sqlite3.Connection:
    if not SITE_DB_PATH.exists():
        sys.exit("还没有结果，先运行：python main.py train（或 update）")
    return sqlite3.connect(SITE_DB_PATH)


def _pct(v):
    return "—" if v is None else f"{v:.0%}"


def print_product(code: str) -> None:
    con = _site()
    row = con.execute("SELECT payload FROM signals WHERE product = ?", (code,)).fetchone()
    if not row:
        sys.exit(f"没有 {code} 的结果")
    s = json.loads(row[0])
    chg = "—" if s.get("change_settle") is None else f"{s['change_pts']:+,g}（{s['change_settle']:+.2%}）"
    print(f"{s['name']} {s['contract']}  {s['date']} 日盘收盘（不含夜盘） {s['close']:,g}  "
          f"结算 {s.get('settle') or 0:,g}  涨跌（比昨结算）{chg}  "
          f"5 日 {s['change_5d']:+.2%}  20 日 {s['change_20d']:+.2%}")
    print(f"量价仓：{s['quadrant']}；持仓量 5 日 {s['oi_change_5d']:+.1%}")
    if s.get("foreign"):
        f = s["foreign"]
        print(f"外盘 {f['name']} {f['close']}，内外比价偏离 z={f['ratio_z']}")
    r = s["risk"]
    print(f"风险：每手保证金约 {r['margin_per_lot']:,} 元，一个涨跌停（{r['limit_pct']}%）约 {r['limit_pnl_per_lot']:,} 元，"
          f"ATR14 约 {r['atr_pnl_per_lot']:,} 元/手")
    for h, v in s["horizons"].items():
        print(f"\n>>> {h} 日（至 {v['until']}）：{v['direction']}，上涨概率 {_pct(v['prob'])}，可信度 {v['confidence']}")
        for reason in v["reasons"]:
            print(f"    - {reason}")
        for d in v["downgrades"]:
            print(f"    ! {d}")
        print("    主要贡献：" + "；".join(f"{c['label']} {c['contribution']:+.2f}（胜率 {_pct(c.get('hit_rate'))}）"
                                    for c in v["contributions"][:5] if c.get("contribution") is not None))
        if v.get("analogs"):
            a = v["analogs"]
            print(f"    相似情形 {a['k']} 次：之后上涨 {a['up_share']:.0%}，中位收益 {a['median_ret']:+.2%}")
    ex = con.execute("SELECT payload FROM explanations WHERE product = ?", (code,)).fetchone()
    if ex:
        print("\n解读（" + json.loads(ex[0])["source"] + "）：\n" + json.loads(ex[0])["text"])


def print_backtest() -> None:
    con = _site()
    bt = json.loads(con.execute("SELECT payload FROM blobs WHERE key = 'backtest'").fetchone()[0])
    comp = json.loads(con.execute("SELECT payload FROM blobs WHERE key = 'comparison_expanding'").fetchone()[0])
    sig = {r[0]: json.loads(r[1]) for r in con.execute("SELECT product, payload FROM signals")}
    for h, per in bt.items():
        print(f"\n===== 持有 {h} 个交易日（不重叠，扣手续费和 1 跳滑点） =====")
        print(f"{'品种':<6}{'年内AUC':>8}{'±SE':>6}{'扩展窗口AUC':>12}{'策略胜率':>9}{'年化':>8}{'夏普':>6}"
              f"{'一直做多年化':>12}{'动量年化':>9}{'抛硬币胜率95%':>16}")
        for code, r in per.items():
            st = r.get("strategy")
            if not st:
                continue
            skill = sig.get(code, {}).get("horizons", {}).get(h, {}).get("skill", {})
            sector = sig.get(code, {}).get("sector")
            alt = comp.get(f"{sector}|{h}", {}).get(code, {})
            print(f"{code:<6}{skill.get('auc_within') or 0:>8.3f}{skill.get('auc_se') or 0:>6.3f}"
                  f"{alt.get('auc_within') or 0:>12.3f}{_pct(st['hit_rate']):>9}{st['ann_return']:>8.1%}"
                  f"{st['sharpe'] if st['sharpe'] is not None else 0:>6.2f}{r['always_long']['ann_return']:>12.1%}"
                  f"{r['momentum_20']['ann_return']:>9.1%}{str(r['coin_hit_95']):>16}")
