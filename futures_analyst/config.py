"""全局配置：路径、时区、回补起点、Claude 模型等。可通过环境变量或项目根目录下的 .env 覆盖。"""
import os
import socket
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv 是可选依赖
    pass

DATA_DIR = Path(os.getenv("FUTURES_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "futures.db"        # 原始数据（采集用，体积大）
SITE_DB_PATH = DATA_DIR / "site.db"      # 网页只读的精简结果库
MODEL_PATH = DATA_DIR / "model.joblib"
EVENTS_PATH = ROOT / "events.yaml"

# 国内期货一律按北京时间算交易日（服务器在海外也一样）
TZ = ZoneInfo("Asia/Shanghai")
# 日盘 15:00 收盘；交易所排名、仓单一般 16:00 前后公布，留出余量
DAY_CLOSE_READY = time(16, 30)

HISTORY_START = os.getenv("HISTORY_START", "2019-01-01")   # 行情、基差、仓单、外盘、汇率的回补起点
RANKS_START = os.getenv("RANKS_START", "2022-01-01")       # 会员持仓排名回补较慢，起点晚一些

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1") == "1"

SITE_PASSCODE = os.getenv("SITE_PASSCODE", "")
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "")         # 存 site.db 的私有数据集仓库，例如 用户名/futures-site-data

# AKShare 底层用 requests / urllib，不传超时就可能一直挂着；统一设一个默认超时
socket.setdefaulttimeout(float(os.getenv("NET_TIMEOUT", "30")))


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()
