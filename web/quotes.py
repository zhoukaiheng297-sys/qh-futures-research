"""实时行情（新浪）：只用于网页展示最新价，不进模型、不改信号。

新浪期货行情接口 hq.sinajs.cn 要带 Referer，每个合约返回一行：
  var hq_str_nf_CU2612="铜2612,010000,开,高,低,昨收,买一,卖一,最新,结算,昨结算,买量,卖量,持仓,成交量,沪,铜,2026-10-28,...";
时间是最后一笔成交的北京时间；夜盘过了零点，日期按自然日。没有的数值用 0 表示。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Callable

import httpx

from futures_analyst.config import TZ, now

URL = "https://hq.sinajs.cn/rn={rn}&list={codes}"
HEADERS = {"Referer": "https://vip.stock.finance.sina.com.cn/",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"}
TTL = 60            # 所有访问者共用一次请求，价格最多是 1 分钟前的
LIVE_WITHIN = 180   # 最后一笔成交在 3 分钟以内算"交易中"，否则算休市


def _num(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v or None


def parse(text: str) -> dict[str, dict]:
    """新浪返回的文本 -> {合约代码: 行情}。合约不存在时新浪返回空字符串，直接跳过。"""
    out = {}
    for line in text.split(";"):
        key, sep, val = line.strip().partition("=")
        f = val.strip().strip('"').split(",")
        if not sep or len(f) < 18 or not f[17]:
            continue
        last, pre = _num(f[8]), _num(f[10])
        if last is None:
            continue
        t = f[1].zfill(6)
        ts = datetime.fromisoformat(f"{f[17]}T{t[:2]}:{t[2:4]}:{t[4:6]}").replace(tzinfo=TZ)
        out[key.rsplit("_", 1)[-1]] = {
            "last": last, "open": _num(f[2]), "high": _num(f[3]), "low": _num(f[4]),
            "settle": _num(f[9]), "pre_settle": pre, "oi": _num(f[13]), "volume": int(float(f[14] or 0)),
            "time": ts.isoformat(timespec="seconds"),
            "session": "夜盘" if ts.hour >= 21 or ts.hour < 3 else "日盘",
            "change_pts": round(last - pre, 2) if pre else None,
            "change_settle": last / pre - 1 if pre else None,
        }
    return out


def fetch_sina(contracts: list[str]) -> dict[str, dict]:
    url = URL.format(rn=f"{random.randrange(2 ** 31):x}", codes=",".join("nf_" + c for c in contracts))
    r = httpx.get(url, headers=HEADERS, timeout=5)
    r.raise_for_status()
    data = parse(r.text)
    if not data:
        raise ValueError("新浪没有返回行情")
    return data


def is_live(q: dict, at: datetime) -> bool:
    age = (at - datetime.fromisoformat(q["time"])).total_seconds()
    return -60 < age < LIVE_WITHIN


class Quotes:
    """带缓存的行情。取不到时继续给上一次的价格，并带上出错原因。"""

    def __init__(self, fetch: Callable[[list[str]], dict] = fetch_sina):
        self.fetch = fetch
        self._lock = threading.Lock()
        self._key: tuple = ()
        self._at = 0.0
        self._data: dict = {}
        self._error: str | None = None
        self._fetched_at: str | None = None

    def get(self, contracts: list[str]) -> dict:
        key = tuple(sorted(contracts))
        with self._lock:            # 同一时刻只发一次请求，其他人等它的结果
            if key != self._key:
                self._key, self._data, self._at = key, {}, 0.0
            if time.time() - self._at > TTL:
                try:
                    self._data, self._error = self.fetch(list(key)), None
                    self._fetched_at = now().isoformat(timespec="seconds")
                except Exception as e:  # noqa: BLE001
                    self._error = f"{type(e).__name__}: {e}"[:200]
                self._at = time.time()
            return {"data": dict(self._data), "error": self._error, "fetched_at": self._fetched_at}
