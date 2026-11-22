# -*- coding: utf-8 -*-
"""集中配置：路径、采集参数、数据源定义。

所有可调参数集中此处，采集层与转换层不硬编码任何地址或阈值。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

DUCKDB_PATH = DATA_DIR / "market.duckdb"
RUN_LOG_PATH = LOG_DIR / "etl_run_log.jsonl"

for _d in (DATA_DIR, RAW_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 采集

HTTP_TIMEOUT = 20          # 秒
HTTP_RETRIES = 3           # 失败重试次数
HTTP_BACKOFF = 1.5         # 退避基数（秒），第 n 次重试等待 BACKOFF * 2^n
HTTP_MAX_WORKERS = 5       # 并发上限——实测 5 并发不触发限流
HTTP_MIN_INTERVAL = 0.05   # 单域最小请求间隔（秒），礼貌抓取

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 数据源端点。两源互补：新浪给截面（估值/市值），腾讯给时序（K线）。
SINA_BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
SINA_NODES_URL = f"{SINA_BASE}/Market_Center.getHQNodes"
SINA_PAGE_URL = f"{SINA_BASE}/Market_Center.getHQNodeData"
SINA_REFERER = "https://finance.sina.com.cn/"

# 腾讯 K 线有两个域名指向同一服务，行为不同（实测）：
#   web.ifzq.gtimg.cn  -> 高频请求后返回 HTTP 501（带 JS 跳转的拦截页）
#   ifzq.gtimg.cn      -> 稳定返回 JSON，5 并发 10 只仅 2.9s
# 因此用不带 web. 前缀的域名。这是踩坑后确定的，勿随意改回。
TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

# 分页：接口硬上限 100/页，实测 num>100 仍只返回 100。
SINA_PAGE_SIZE = 100
SINA_MAX_PAGES = 80        # 安全上限，正常 56 页结束

# K线：默认取近 3 年日线，够算 250 日均线与年度分位。
KLINE_LIMIT = 800
KLINE_PERIOD = "day"       # day / week / month
KLINE_ADJUST = "qfq"       # qfq=前复权 / hfq=后复权 / 空=不复权

# ---------------------------------------------------------------- 因子

TRADING_DAYS_PER_YEAR = 250
MOMENTUM_WINDOWS = (5, 20, 60, 120)
VOLATILITY_WINDOW = 20
FUTURE_RETURN_DAYS = 5     # AI 模块的预测目标：未来 5 日收益

# ---------------------------------------------------------------- 数据质量

# 全市场快照的合理行数区间；低于下限说明接口异常，应中止而非写入。
SNAPSHOT_MIN_ROWS = 3000
SNAPSHOT_MAX_ROWS = 8000


def env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量，用于覆盖默认行为（如跳过网络）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
