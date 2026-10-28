"""品种表：第一版覆盖有色金属和黑色系。合约参数来自交易所规则（AKShare futures_rule 可核对）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Product:
    code: str                 # 品种代码，如 CU
    name: str                 # 中文名
    exchange: str             # SHFE / DCE
    sector: str               # 有色 / 黑色
    multiplier: int           # 合约乘数（每手多少吨）
    tick: float               # 最小变动价位
    limit_pct: float          # 一般月份涨跌停幅度（%）
    margin_pct: float         # 一般月份交易所保证金比例（%）
    round_step: int           # 整数关口的间隔（元/吨），用于锚定效应
    foreign: str | None = None        # 新浪外盘代码（LME/SGX）
    foreign_name: str | None = None
    receipt_keys: tuple[str, ...] = ()  # 上期所仓单日报里的品种名（新旧格式都列上）
    inventory_em: str | None = None     # 东方财富库存接口代码
    news_tag: str | None = None         # 上海金属网快讯分类
    news_query: str = ""                # 联网搜索消息时用的关键词
    unit: str = "元/吨"


PRODUCTS: dict[str, Product] = {p.code: p for p in [
    Product("CU", "沪铜", "SHFE", "有色", 5, 10, 7, 16, 1000, "CAD", "LME铜3个月",
            ("铜",), "cu", "铜", "沪铜 铜价 LME铜 精炼铜 库存 铜矿"),
    Product("AL", "沪铝", "SHFE", "有色", 5, 5, 7, 16, 500, "AHD", "LME铝3个月",
            ("铝",), "al", "铝", "沪铝 铝价 LME铝 电解铝 氧化铝 库存"),
    Product("ZN", "沪锌", "SHFE", "有色", 5, 5, 7, 16, 500, "ZSD", "LME锌3个月",
            ("锌",), "zn", "锌", "沪锌 锌价 LME锌 锌锭 锌矿加工费 库存"),
    Product("NI", "沪镍", "SHFE", "有色", 1, 10, 8, 18, 5000, "NID", "LME镍3个月",
            ("镍",), "ni", "镍", "沪镍 镍价 LME镍 印尼镍矿 镍铁 不锈钢"),
    Product("RB", "螺纹钢", "SHFE", "黑色", 10, 1, 5, 11, 100, None, None,
            ("螺纹钢仓库", "螺纹钢厂库"), "rb", None, "螺纹钢 期货 钢材库存 建材成交 钢厂 限产"),
    Product("HC", "热轧卷板", "SHFE", "黑色", 10, 1, 5, 11, 100, None, None,
            ("热轧卷板仓库", "热轧卷板厂库", "热轧卷板"), "hc", None, "热卷 热轧卷板 期货 钢材 出口 库存"),
    Product("I", "铁矿石", "DCE", "黑色", 100, 0.5, 6, 14, 50, "FEF", "新加坡铁矿石",
            (), "i", None, "铁矿石 期货 港口库存 发运 铁水产量"),
    Product("J", "焦炭", "DCE", "黑色", 100, 0.5, 8, 18, 100, None, None,
            (), "j", None, "焦炭 期货 提涨 提降 焦化利润 钢厂"),
    Product("JM", "焦煤", "DCE", "黑色", 60, 0.5, 8, 20, 100, None, None,
            (), "jm", None, "焦煤 期货 煤矿 安监 蒙煤 进口"),
]}

SECTORS: dict[str, list[str]] = {}
for _p in PRODUCTS.values():
    SECTORS.setdefault(_p.sector, []).append(_p.code)


@dataclass(frozen=True)
class Spread:
    """产业链价差：value = 主品种价格 − Σ 系数 × 原料价格 − 常数。系数是行业常用的近似值。"""
    name: str
    legs: dict[str, float]     # 品种代码 -> 系数（正 = 产品，负 = 原料）
    const: float
    note: str
    applies_to: tuple[str, ...] = field(default=())


SPREADS = [
    Spread("螺纹盘面利润", {"RB": 1.0, "I": -1.6, "J": -0.5}, 800,
           "螺纹 − 1.6×铁矿 − 0.5×焦炭 − 800（其他成本），近似的长流程钢厂盘面利润", ("RB", "HC", "I", "J")),
    Spread("热卷盘面利润", {"HC": 1.0, "I": -1.6, "J": -0.5}, 900,
           "热卷 − 1.6×铁矿 − 0.5×焦炭 − 900，近似值", ("HC", "RB", "I")),
    Spread("焦化盘面利润", {"J": 1.0, "JM": -1.33}, 200,
           "焦炭 − 1.33×焦煤 − 200，近似的独立焦化厂利润", ("J", "JM")),
]


def contract_month_codes(code: str, start_year: int, end_year: int, end_month: int) -> list[str]:
    """生成 start_year 1 月到 end_year end_month 的全部合约代码（这两个板块的品种 12 个月份都挂牌）。"""
    out = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if y == end_year and m > end_month:
                break
            out.append(f"{code}{y % 100:02d}{m:02d}")
    return out


def contract_expiry_month(contract: str) -> tuple[int, int]:
    """合约代码 -> (年, 月)，如 CU2612 -> (2026, 12)。"""
    digits = contract[-4:]
    return 2000 + int(digits[:2]), int(digits[2:])
