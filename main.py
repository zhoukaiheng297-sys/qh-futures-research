"""命令行入口。

    python main.py probe              探测各数据源在当前机器上能否访问（决定批处理放在哪跑）
    python main.py init               回补历史数据（可断点续跑，首次约 1~2 小时）
    python main.py update             日更：采集 → 因子 → 模型 → 可靠性 → 消息 → 导出网页数据
    python main.py train              重新做滚动训练和回测（不采集）
    python main.py backtest           打印回测结果和基准对比
    python main.py report CU          在终端查看某品种的最新信号和理由
    python main.py publish [--raw]    把网页数据（--raw 连同原始库）上传到 Hugging Face 私有数据集
    python main.py pull-raw           从私有数据集拉回原始库（GitHub Actions 每次运行前用）
    python main.py check              检查各项数据是否过期，过期时退出码为 1（GitHub Actions 靠它发邮件提醒）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta


def cmd_probe(args):
    from futures_analyst import sources as S
    from futures_analyst.config import today

    d = today() - timedelta(days=7)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    checks = [
        ("新浪 合约日线 CU", lambda: S.contract_bars("CU" + f"{(today().year + 1) % 100:02d}01")),
        ("新浪 合约日线 I（大商所）", lambda: S.contract_bars("I" + f"{(today().year + 1) % 100:02d}01")),
        ("上期所 会员持仓排名", lambda: S.ranks_shfe(d, ["CU", "RB"])),
        ("上期所 仓单日报", lambda: S.receipts_shfe(d)),
        ("生意社 现货与基差", lambda: S.basis_range(d, d, ["CU", "I"])),
        ("新浪 外盘 LME铜", lambda: S.foreign_hist("CAD")),
        ("中国银行 美元汇率", lambda: S.usdcny_range(d - timedelta(days=10), d)),
        ("东方财富 库存", lambda: S.inventory_em("CU")),
        ("上海金属网 快讯", lambda: S.shmet_news("CU")),
        ("国泰君安 交易规则", lambda: S.trading_rules(today())),
    ]
    bad = 0
    for name, fn in checks:
        t0 = time.time()
        try:
            n = len(fn())
            print(f"  OK   {name:<22} {n:>6} 行  {time.time() - t0:5.1f}s")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  FAIL {name:<22} {type(e).__name__}: {str(e)[:120]}")
    print("全部可用。" if not bad else f"{bad} 个数据源不可用。")


def cmd_init(args):
    from futures_analyst.collect import run
    from futures_analyst.db import DB

    run(DB(), full=True, only=set(args.only.split(",")) if args.only else None)


def cmd_update(args):
    from futures_analyst.pipeline import daily

    daily(skip_collect=args.skip_collect, skip_news=args.skip_news)


def cmd_train(args):
    from futures_analyst.pipeline import train_and_export

    train_and_export()


def cmd_backtest(args):
    from futures_analyst.report import print_backtest

    print_backtest()


def cmd_report(args):
    from futures_analyst.report import print_product

    print_product(args.product.upper())


def cmd_publish(args):
    from futures_analyst.publish import upload

    upload(raw=args.raw)


def cmd_pull_raw(args):
    from futures_analyst.publish import download_raw

    download_raw()


def cmd_check(args):
    from futures_analyst.db import DB
    from futures_analyst.freshness import check, report
    from futures_analyst.tradecal import Calendar, load_calendar

    db = DB()
    cal = Calendar(load_calendar(db))
    expected = cal.signal_date()
    if not report(check(db, cal, expected), expected):
        sys.exit(1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="QH期货：国内期货多因子信号")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="探测数据源").set_defaults(func=cmd_probe)
    p = sub.add_parser("init", help="回补历史数据")
    p.add_argument("--only", default="", help="只回补部分数据集，逗号分隔：bars,ranks,receipts,basis,foreign,fx,misc")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("update", help="日更")
    p.add_argument("--skip-collect", action="store_true")
    p.add_argument("--skip-news", action="store_true")
    p.set_defaults(func=cmd_update)
    sub.add_parser("train", help="滚动训练与回测").set_defaults(func=cmd_train)
    sub.add_parser("backtest", help="打印回测结果").set_defaults(func=cmd_backtest)
    p = sub.add_parser("report", help="查看某品种")
    p.add_argument("product")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("publish", help="上传网页数据")
    p.add_argument("--raw", action="store_true", help="同时上传原始库")
    p.set_defaults(func=cmd_publish)
    sub.add_parser("pull-raw", help="拉回原始库").set_defaults(func=cmd_pull_raw)
    sub.add_parser("check", help="检查数据是否过期").set_defaults(func=cmd_check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
