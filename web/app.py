"""网页版：FastAPI 后端 + 原生 JS 前端，只读批处理导出的 site.db。

    uvicorn web.app:app --port 7860

整站口令：所有 /api 数据接口都要先登录（口令换一个 30 天有效的签名 Cookie）；静态页面本身不含数据。
部署在 Hugging Face 时，site.db 从私有数据集仓库下载，每小时检查一次更新。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from futures_analyst.config import HF_DATASET_REPO, SITE_DB_PATH, SITE_PASSCODE, now
from web.quotes import Quotes, is_live

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "fa_session"
COOKIE_DAYS = 30
MAX_FAILED = 5            # 同一 IP 10 分钟内口令输错次数上限
REFRESH_SECONDS = 3600


class State:
    failed: dict[str, list[float]] = {}
    cache: dict[str, tuple[float, object]] = {}
    lock = threading.Lock()
    last_pull: str | None = None
    pull_error: str | None = None


state = State()
QUOTES = Quotes()


# ---------- site.db ----------
def pull_site_db() -> None:
    """从 Hugging Face 私有数据集下载最新的 site.db（没配置仓库时什么也不做）。"""
    token = os.getenv("HF_TOKEN")
    if not (HF_DATASET_REPO and token):
        return
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(HF_DATASET_REPO, "site.db", repo_type="dataset", token=token, force_download=True)
        SITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SITE_DB_PATH.with_suffix(".download")
        tmp.write_bytes(Path(path).read_bytes())
        tmp.replace(SITE_DB_PATH)
        state.last_pull, state.pull_error = now().isoformat(timespec="seconds"), None
        state.cache.clear()
    except Exception as e:  # noqa: BLE001 —— 下载失败就继续用旧文件
        state.pull_error = f"{type(e).__name__}: {e}"


def _refresher():
    while True:
        time.sleep(REFRESH_SECONDS)
        pull_site_db()


def _site_query(sql: str, params: tuple = ()) -> list[tuple]:
    if not SITE_DB_PATH.exists():
        raise HTTPException(503, "还没有数据：请先在批处理机器上运行 python main.py update（并 publish）。")
    con = sqlite3.connect(f"file:{SITE_DB_PATH}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _cached(key: str, loader):
    """按 site.db 的修改时间缓存解析后的 JSON。"""
    mtime = SITE_DB_PATH.stat().st_mtime if SITE_DB_PATH.exists() else 0
    with state.lock:
        hit = state.cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    value = loader()
    with state.lock:
        state.cache[key] = (mtime, value)
    return value


def _meta() -> dict:
    return _cached("meta", lambda: {k: json.loads(v) for k, v in _site_query("SELECT key, value FROM meta")})


def _blob(key: str):
    rows = _site_query("SELECT payload FROM blobs WHERE key = ?", (key,))
    return json.loads(rows[0][0]) if rows else None


# ---------- 口令 ----------
def _sign(ts: str) -> str:
    key = hashlib.sha256(("futures-analyst:" + SITE_PASSCODE).encode()).digest()
    return hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    if not SITE_PASSCODE:
        return True   # 本地开发未设口令时放行（部署脚本会强制要求设置）
    raw = request.cookies.get(COOKIE, "")
    ts, _, sig = raw.partition(".")
    if not ts.isdigit() or time.time() - int(ts) > COOKIE_DAYS * 86400:
        return False
    return hmac.compare_digest(sig, _sign(ts))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pull_site_db()
    threading.Thread(target=_refresher, daemon=True).start()
    yield


app = FastAPI(title="QH期货", lifespan=lifespan, docs_url=None, redoc_url=None)

OPEN_PATHS = {"/api/session", "/api/login", "/api/logout"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in OPEN_PATHS and not _authed(request):
        return JSONResponse({"detail": "请先输入访问口令。"}, status_code=401)
    return await call_next(request)


class LoginIn(BaseModel):
    passcode: str = Field(max_length=200)


@app.get("/api/session")
def session(request: Request):
    return {"authed": _authed(request), "passcode_required": bool(SITE_PASSCODE)}


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = _client_ip(request)
    now = time.time()
    recent = [t for t in state.failed.get(ip, []) if now - t < 600]
    if len(recent) >= MAX_FAILED:
        raise HTTPException(429, "口令错误次数太多，请 10 分钟后再试。")
    if not SITE_PASSCODE or not hmac.compare_digest(body.passcode.encode(), SITE_PASSCODE.encode()):
        state.failed[ip] = recent + [now]
        raise HTTPException(401, "访问口令不对。")
    state.failed.pop(ip, None)
    ts = str(int(now))
    response.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------- 数据接口 ----------
@app.get("/api/overview")
def overview():
    def load():
        rows = _site_query("SELECT product, payload FROM signals")
        items = []
        for code, payload in rows:
            s = json.loads(payload)
            items.append({k: s.get(k) for k in ("product", "name", "sector", "contract", "close", "settle",
                                                 "pre_settle", "change_pts", "change_settle", "change_1d",
                                                 "change_5d", "change_20d", "quadrant", "date", "near_roll")}
                         | {"horizons": {h: {k: v.get(k) for k in ("direction", "confidence", "prob", "until")}
                                         for h, v in s.get("horizons", {}).items()},
                            "downgrades": sorted({d for v in s.get("horizons", {}).values() for d in v["downgrades"]})})
        return {"meta": _meta(), "products": items}
    return _cached("overview", load)


@app.get("/api/product/{code}")
def product(code: str):
    code = code.upper()
    if len(code) > 4 or not code.isalpha():
        raise HTTPException(400, "品种代码不对。")

    def load():
        sig = _site_query("SELECT payload FROM signals WHERE product = ?", (code,))
        if not sig:
            raise HTTPException(404, f"没有 {code} 的数据。")
        get = lambda t: (r[0][0] if (r := _site_query(f"SELECT payload FROM {t} WHERE product = ?", (code,))) else None)
        return {"signal": json.loads(sig[0][0]), "series": json.loads(get("series") or "{}"),
                "news": json.loads(get("news") or "{}"), "explanation": json.loads(get("explanations") or "null"),
                "meta": _meta()}
    return _cached(f"product:{code}", load)


@app.get("/api/backtest")
def backtest():
    return _cached("backtest", lambda: {"backtest": _blob("backtest"), "expanding": _blob("comparison_expanding"),
                                        "forward": _blob("forward"),
                                        "signals": {c: {"sector": json.loads(p)["sector"],
                                                        "skill": {h: v.get("skill") for h, v in
                                                                  json.loads(p).get("horizons", {}).items()}}
                                                    for c, p in _site_query("SELECT product, payload FROM signals")},
                                        "meta": _meta()})


@app.get("/api/quotes")
def quotes():
    """各品种信号日主力合约的实时行情（新浪，约 1 分钟更新）。只用于展示，不影响信号。"""
    contracts = _cached("contracts", lambda: {c: json.loads(p)["contract"]
                                              for c, p in _site_query("SELECT product, payload FROM signals")})
    got, at = QUOTES.get(list(contracts.values())), now()
    out = {code: {**got["data"][c], "contract": c, "live": is_live(got["data"][c], at)}
           for code, c in contracts.items() if c in got["data"]}
    return {"quotes": out, "fetched_at": got["fetched_at"], "error": got["error"]}


@app.get("/api/health")
def health():
    return {"sources": _blob("health"), "events": _blob("events"), "meta": _meta(),
            "site_db": {"pulled_at": state.last_pull, "pull_error": state.pull_error,
                        "remote": bool(HF_DATASET_REPO)}}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
