# -*- coding: utf-8 -*-
"""腾讯行情数据源：前复权 K 线 + 批量实时快照。

K线接口实测：
- 单只 800 根日线 1.2s，5 并发下平均 0.30s/只
- 返回结构 `data[symbol][qfqday|day]` = [[date, open, close, high, low, volume], ...]
- 注意列顺序是 open/close/high/low/volume，不是常见的 OHLC，必须显式映射
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import settings
from src.extract.http_client import HttpClient

logger = logging.getLogger(__name__)

# 腾讯返回列的固定顺序，显式命名避免 OHLC 错位
KLINE_COLUMNS = ["date", "open", "close", "high", "low", "volume"]

# 少于这个根数的 K 线视为无效：滚动窗口（20 日）都撑不满，算出来的
# 因子是残数据硬凑的，比没有更糟。
MIN_VALID_BARS = 25


def to_tencent_symbol(code: str) -> str:
    """把 6 位代码转成腾讯的 sh/sz/bj 前缀格式。

    各板代码段（容易踩坑的地方）：
    - 沪市主板 60xxxx / 科创板 68xxxx / 沪 B 股 900xxx  -> sh
    - 深市主板 000xxx / 中小板 002xxx / 创业板 30xxxx  -> sz
    - 深 B 股 200xxx                                    -> sz
    - 北交所 43xxxx / 83xxxx / 87xxxx / 88xxxx / 92xxxx -> bj

    注意 920xxx 是北交所新代码段，不能因为「9 开头」就归到沪市——
    沪市只有 900xxx 这一小段是 9 开头的（B 股），两者必须区分。
    """
    code = str(code).strip().lower()
    if code[:2] in {"sh", "sz", "bj"}:
        return code

    code = code.zfill(6)

    if code.startswith(("43", "83", "87", "88", "92")):
        return f"bj{code}"
    if code.startswith("900"):
        return f"sh{code}"
    if code.startswith(("60", "68", "90")):
        return f"sh{code}"
    if code.startswith(("00", "20", "30")):
        return f"sz{code}"

    # 兜底：按首位判断，宁可错也不丢（调用方会把失败计入 failures）
    return f"sh{code}" if code[0] in "69" else f"sz{code}"


def fetch_kline(
    client: HttpClient,
    code: str,
    period: str = settings.KLINE_PERIOD,
    adjust: str = settings.KLINE_ADJUST,
    limit: int = settings.KLINE_LIMIT,
    min_bars: int = MIN_VALID_BARS,
) -> list[dict[str, Any]]:
    """抓取单只股票的 K 线，返回按日期升序的字典列表。

    复权失败时（部分新股/退市股无复权数据）自动回落到不复权，
    并在每行标记 adjust_type，便于下游识别数据口径差异。

    `min_bars` 是「有效样本」门槛：少于这个根数直接视为无效返回空。
    实测北交所（43/83/87/88/92 开头）腾讯只给 0~1 根，新股也只有几天，
    这类数据进因子表会污染滚动窗口（算出来的 mom_20d 是拿几天数据硬凑的）。
    """
    symbol = to_tencent_symbol(code)
    param = f"{symbol},{period},,,{limit},{adjust}"
    resp = client.get(
        settings.TENCENT_KLINE_URL,
        params={"param": param},
        headers={"Referer": "https://gu.qq.com/"},
    )

    # 该接口被限流时不返回 4xx/5xx，而是回一段带 JS 跳转的 HTML 拦截页。
    # 直接 resp.json() 会抛 JSONDecodeError，错误信息毫无线索，所以这里
    # 显式识别并给出可操作的提示。
    body = resp.text.lstrip()
    if body.startswith("<"):
        raise ValueError(
            f"tencent kline returned HTML (likely rate-limited) for {code}; "
            f"status={resp.status_code}, head={body[:80]!r}"
        )

    payload = resp.json()

    if payload.get("code") != 0:
        raise ValueError(f"tencent kline error for {code}: {payload.get('msg')}")

    node = (payload.get("data") or {}).get(symbol) or {}
    if not node:
        return []

    # 前复权键名是 qfq+period，不复权是裸 period
    candidates = [f"{adjust}{period}" if adjust else period, period]
    bars: list[Any] | None = None
    used_key = ""
    for key in candidates:
        if node.get(key):
            bars = node[key]
            used_key = key
            break

    if not bars:
        return []

    # 有效样本不足则整只丢弃，而不是留下几天残数据
    if len(bars) < min_bars:
        logger.debug("%s: only %d bars (< %d), skipped", code, len(bars), min_bars)
        return []

    effective_adjust = "qfq" if used_key.startswith("qfq") else (
        "hfq" if used_key.startswith("hfq") else "none"
    )

    rows: list[dict[str, Any]] = []
    for bar in bars:
        if len(bar) < 6:
            continue
        row = {col: bar[i] for i, col in enumerate(KLINE_COLUMNS)}
        row.update({
            "code": symbol[2:],
            "symbol": symbol,
            "period": period,
            "adjust_type": effective_adjust,
        })
        rows.append(row)
    return rows


def fetch_kline_batch(
    client: HttpClient,
    codes: list[str],
    period: str = settings.KLINE_PERIOD,
    adjust: str = settings.KLINE_ADJUST,
    limit: int = settings.KLINE_LIMIT,
    workers: int = settings.HTTP_MAX_WORKERS,
    on_progress: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """并发抓取多只股票 K 线。

    返回 (成功行, 失败清单)。单只失败不中断整体，失败项记录原因
    供重跑，这是长跑管道能跑完的关键。
    """
    all_rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    done = 0
    total = len(codes)

    def work(code: str) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            rows = fetch_kline(client, code, period=period, adjust=adjust, limit=limit)
            return code, rows, None
        except Exception as exc:  # noqa: BLE001
            return code, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, c): c for c in codes}
        for fut in as_completed(futures):
            code, rows, err = fut.result()
            done += 1
            if err:
                failures.append({"code": code, "error": err[:200]})
            all_rows.extend(rows)
            if on_progress and (done % 50 == 0 or done == total):
                on_progress(done, total, len(all_rows), len(failures))

    return all_rows, failures


# 腾讯快照字段位置（实测 88 个字段，此处只取稳定可用的部分）
_QUOTE_FIELD_MAP = {
    1: "name",
    2: "trade",
    3: "prev_close",
    4: "open",
    5: "volume",          # 手
    31: "change",
    32: "change_pct",
    33: "high",
    34: "low",
    37: "amount",         # 万元
    38: "turnoverratio",
    39: "per",
    43: "amplitude",
    44: "mktcap",         # 亿元
    45: "nmc",            # 亿元
}

_QUOTE_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


def fetch_quotes(client: HttpClient, codes: list[str]) -> list[dict[str, Any]]:
    """批量取实时快照。单次请求可带数十只，用于盘中刷新。"""
    symbols = [to_tencent_symbol(c) for c in codes]
    rows: list[dict[str, Any]] = []

    # 每批 60 只，避免 URL 过长被服务端拒绝
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        resp = client.get(
            settings.TENCENT_QUOTE_URL + ",".join(chunk),
            encoding="gbk",
        )
        for symbol, body in _QUOTE_LINE_RE.findall(resp.text):
            parts = body.split("~")
            if len(parts) < 46:
                continue
            row: dict[str, Any] = {"symbol": symbol, "code": symbol[2:]}
            for idx, field in _QUOTE_FIELD_MAP.items():
                row[field] = parts[idx] if idx < len(parts) else None
            rows.append(row)
    return rows
