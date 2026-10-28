"""网页接口测试：整站口令、错误次数限制、各数据接口。用一个临时的小 site.db，不依赖真实数据。

    .venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

SIGNAL = {
    "product": "CU", "name": "沪铜", "sector": "有色", "exchange": "SHFE", "contract": "CU2612", "close": 108390,
    "settle": 108640, "pre_settle": 108180, "change_pts": 210.0, "change_settle": 0.00194,
    "change_1d": 0.002, "change_5d": -0.004, "change_20d": -0.005, "quadrant": "价跌仓增（空头主动）", "date": "2026-10-27",
    "near_roll": False,
    "horizons": {"10": {"direction": "偏多", "confidence": "低", "prob": 0.56, "until": "2026-11-10", "downgrades": ["x"],
                        "skill": {"auc_within": 0.45, "auc_se": 0.04}}},
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE signals (product TEXT PRIMARY KEY, payload TEXT); CREATE TABLE series (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE news (product TEXT PRIMARY KEY, payload TEXT); CREATE TABLE explanations (product TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE blobs (key TEXT PRIMARY KEY, payload TEXT);""")
    meta = {"signal_day": "2026-10-27", "generated_at": "2026-10-28T06:00:00", "disclaimer": "仅供研究", "horizons": [10],
            "sectors": {"有色": ["CU"]}, "products": {"CU": {"name": "沪铜"}}, "data_last_date": "2026-10-27",
            "trained_at": "2026-10-28T06:00:00"}
    con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    con.execute("INSERT INTO signals VALUES ('CU', ?)", (json.dumps(SIGNAL, ensure_ascii=False),))
    con.execute("INSERT INTO series VALUES ('CU', ?)", (json.dumps({"date": ["2026-10-27"], "close": [108390]}),))
    con.execute("INSERT INTO news VALUES ('CU', '{}')")
    con.executemany("INSERT INTO blobs VALUES (?, ?)", [("backtest", "{}"), ("comparison_expanding", "{}"),
                                                         ("health", "[]"), ("events", "[]")])
    con.commit()
    con.close()
    monkeypatch.setenv("SITE_PASSCODE", "test-passcode-123")
    monkeypatch.setenv("HF_DATASET_REPO", "")
    import futures_analyst.config as cfg
    importlib.reload(cfg)
    monkeypatch.setattr(cfg, "SITE_DB_PATH", db)
    import web.app as webapp
    importlib.reload(webapp)
    monkeypatch.setattr(webapp, "SITE_DB_PATH", db)
    with TestClient(webapp.app) as c:
        yield c


def test_requires_passcode(client):
    assert client.get("/api/session").json() == {"authed": False, "passcode_required": True}
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/product/CU").status_code == 401
    assert "QH期货" in client.get("/").text          # 静态页面本身不含数据，可以打开


def test_login_and_data(client):
    assert client.post("/api/login", json={"passcode": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"passcode": "test-passcode-123"})
    assert r.status_code == 200 and "fa_session" in r.cookies
    ov = client.get("/api/overview").json()
    assert ov["products"][0]["horizons"]["10"]["direction"] == "偏多"
    assert ov["products"][0]["downgrades"] == ["x"]
    quote = {k: ov["products"][0][k] for k in ("settle", "pre_settle", "change_settle")}
    assert quote == {"settle": 108640, "pre_settle": 108180, "change_settle": 0.00194}   # 总览也要有行情软件口径的涨跌
    p = client.get("/api/product/cu").json()
    assert p["signal"]["contract"] == "CU2612" and p["series"]["close"] == [108390]
    assert client.get("/api/product/XX").status_code == 404
    assert client.get("/api/product/../etc").status_code in (400, 404)
    assert client.get("/api/health").status_code == 200
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/overview").status_code == 401


def test_forged_cookie_rejected(client):
    client.cookies.set("fa_session", "9999999999.deadbeef")
    assert client.get("/api/overview").status_code == 401


def test_too_many_failures(client):
    for _ in range(5):
        client.post("/api/login", json={"passcode": "nope"})
    r = client.post("/api/login", json={"passcode": "test-passcode-123"})
    assert r.status_code == 429 and "10 分钟" in r.json()["detail"]


# 新浪真实返回的格式（2026-10-28 沪铜夜盘收盘时的一行；XX2601 是不存在的合约，新浪返回空字符串）
SINA = ('var hq_str_nf_CU2612="铜2612,010000,109010.000,109450.000,108950.000,0.000,109370.000,109430.000,109420.000,'
        '0.000,108640.000,3,2,195920.000,36694,沪,铜,2026-10-28,1,,,,,,,,,109182.765,109350.000,1";\n'
        'var hq_str_nf_XX2601="";\n')


def test_parse_sina_quotes():
    from web.quotes import parse

    q = parse(SINA)
    assert list(q) == ["CU2612"]
    cu = q["CU2612"]
    assert (cu["last"], cu["pre_settle"], cu["change_pts"], cu["settle"]) == (109420, 108640, 780, None)
    assert abs(cu["change_settle"] - (109420 / 108640 - 1)) < 1e-12
    assert cu["time"] == "2026-10-28T01:00:00+08:00" and cu["session"] == "夜盘" and cu["volume"] == 36694


def test_quotes_endpoint(client, monkeypatch):
    from datetime import datetime

    import web.app as webapp
    from futures_analyst.config import TZ
    from web.quotes import Quotes, parse

    assert client.get("/api/quotes").status_code == 401                         # 实时行情同样要口令
    client.post("/api/login", json={"passcode": "test-passcode-123"})
    asked = []
    monkeypatch.setattr(webapp, "QUOTES", Quotes(fetch=lambda cs: asked.append(cs) or parse(SINA)))
    monkeypatch.setattr(webapp, "now", lambda: datetime(2026, 10, 28, 0, 59, 30, tzinfo=TZ))
    d = client.get("/api/quotes").json()
    assert asked == [["CU2612"]]                                                # 取的是信号日的主力合约
    assert d["error"] is None and d["quotes"]["CU"]["last"] == 109420 and d["quotes"]["CU"]["live"] is True
    client.get("/api/quotes")
    assert len(asked) == 1                                                      # 1 分钟内共用缓存
    monkeypatch.setattr(webapp, "now", lambda: datetime(2026, 10, 28, 8, 0, tzinfo=TZ))
    assert client.get("/api/quotes").json()["quotes"]["CU"]["live"] is False   # 夜盘收盘后算休市

    def boom(cs):
        raise ConnectionError("新浪连不上")
    monkeypatch.setattr(webapp, "QUOTES", Quotes(fetch=boom))
    d = client.get("/api/quotes").json()
    assert d["quotes"] == {} and "新浪连不上" in d["error"]
