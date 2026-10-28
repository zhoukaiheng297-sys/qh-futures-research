"""SQLite 原始数据库。批处理会在多个线程里并发采集，所以共用一个连接并用锁串行化。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (            -- 每个合约的日线（新浪）
    contract TEXT NOT NULL, product TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, oi REAL, settle REAL,
    PRIMARY KEY (contract, date)
);
CREATE INDEX IF NOT EXISTS idx_bars_product ON bars (product, date);
CREATE TABLE IF NOT EXISTS ranks (           -- 前 20 名会员持仓合计
    product TEXT NOT NULL, date TEXT NOT NULL, scope TEXT NOT NULL,   -- scope: variety=全品种 / 合约代码=只算主力
    long20 REAL, short20 REAL, long_chg20 REAL, short_chg20 REAL, vol20 REAL,
    PRIMARY KEY (product, date)
);
CREATE TABLE IF NOT EXISTS receipts (        -- 上期所注册仓单
    product TEXT NOT NULL, date TEXT NOT NULL, qty REAL, chg REAL,
    PRIMARY KEY (product, date)
);
CREATE TABLE IF NOT EXISTS inventory (       -- 东方财富库存（只有最近约 60 个交易日，往后逐日积累）
    product TEXT NOT NULL, date TEXT NOT NULL, qty REAL,
    PRIMARY KEY (product, date)
);
CREATE TABLE IF NOT EXISTS basis (           -- 生意社现货价与主力基差
    product TEXT NOT NULL, date TEXT NOT NULL, spot REAL, dom_contract TEXT, dom_price REAL, dom_basis_rate REAL,
    PRIMARY KEY (product, date)
);
CREATE TABLE IF NOT EXISTS foreign_bars (    -- 外盘日线（新浪：LME 3 个月、新加坡铁矿石）
    symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS fx (              -- 美元兑人民币（央行中间价，缺失时用中行折算价）
    date TEXT PRIMARY KEY, usdcny REAL
);
CREATE TABLE IF NOT EXISTS news_items (      -- 消息条目（上海金属网快讯原文，或联网搜索整理出的条目）
    id TEXT PRIMARY KEY, product TEXT NOT NULL, published_at TEXT, source TEXT, title TEXT, url TEXT,
    content TEXT, stance TEXT, impact INTEGER, reason TEXT, digest_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_product ON news_items (product, published_at);
CREATE TABLE IF NOT EXISTS news_digest (     -- 每品种每天一份消息汇总
    product TEXT NOT NULL, date TEXT NOT NULL, summary TEXT, sentiment REAL, bullish INTEGER, bearish INTEGER,
    neutral INTEGER, sources TEXT, model TEXT, created_at TEXT,
    PRIMARY KEY (product, date)
);
CREATE TABLE IF NOT EXISTS predictions (     -- 滚动样本外预测（回测和可靠性统计都用它）
    product TEXT NOT NULL, horizon INTEGER NOT NULL, date TEXT NOT NULL,
    score REAL, prob REAL, gbm_prob REAL, fwd_ret REAL,
    PRIMARY KEY (product, horizon, date)
);
CREATE TABLE IF NOT EXISTS forward_log (     -- 前瞻检验：每天记下主模型和对照模型的预测，只增不改（见 forward.py）
    model TEXT NOT NULL, product TEXT NOT NULL, horizon INTEGER NOT NULL, signal_day TEXT NOT NULL,
    contract TEXT, prob REAL, score REAL, logged_at TEXT,
    PRIMARY KEY (model, product, horizon, signal_day)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    dataset TEXT NOT NULL, key TEXT NOT NULL, ok INTEGER NOT NULL, rows INTEGER, latest TEXT,
    elapsed REAL, error TEXT, at TEXT NOT NULL,
    PRIMARY KEY (dataset, key)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def upsert(self, table: str, columns: list[str], rows: Iterable[tuple]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        with self._lock, self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(sql, self.conn, params=params)

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock, self.conn:
            return self.conn.execute(sql, params).fetchall()

    def get_meta(self, key: str) -> str | None:
        rows = self.execute("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # ---------- 读取 ----------
    def bars(self, product: str) -> pd.DataFrame:
        df = self.query("SELECT * FROM bars WHERE product = ? ORDER BY date, contract", (product,))
        df["date"] = pd.to_datetime(df["date"])
        return df

    def table(self, table: str, where: str = "", params: tuple = ()) -> pd.DataFrame:
        df = self.query(f"SELECT * FROM {table} {where}", params)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def done_keys(self, dataset: str) -> set[str]:
        """已经成功采集过的 key（回补可以断点续跑）。"""
        return {r[0] for r in self.execute("SELECT key FROM fetch_log WHERE dataset = ? AND ok = 1", (dataset,))}
