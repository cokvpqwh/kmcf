"""项目内所有文件路径的唯一来源。"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller 打包后，数据放在 exe 旁边
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

PKG_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PKG_DIR / "templates"

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # 每个交易日的原始接口数据缓存：raw/YYYYMMDD/*.pkl
CACHE_DIR = DATA_DIR / "cache"      # 交易日历等跨日缓存
REPORT_DIR = ROOT / "reports"       # 生成的复盘报告：YYYY-MM-DD.html / .json
ASSET_DIR = REPORT_DIR / "assets"   # 报告用到的本地 JS（echarts）
LOG_DIR = ROOT / "logs"

CONFIG_PATH = DATA_DIR / "config.json"
HISTORY_PATH = DATA_DIR / "history.csv"


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, CACHE_DIR, REPORT_DIR, ASSET_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
