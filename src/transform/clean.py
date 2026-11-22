# -*- coding: utf-8 -*-
"""清洗层：把上游原始字典列表标准化为强类型 DataFrame。

原则：
- 类型在这里确定，下游不再有 object 列。
- 空值与异常值统一成 NaN，不用 0 冒充缺失（0 会被误当成真实数值参与计算）。
- 每步保持幂等：同样的输入永远产出同样的输出。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 快照的数值列。缺失按 NaN 处理。
_SNAPSHOT_NUMERIC = [
    "trade", "pricechange", "changepercent", "buy", "sell", "settlement",
    "open", "high", "low", "volume", "amount", "per", "pb", "mktcap",
    "nmc", "turnoverratio",
]


def _to_numeric(series: pd.Series) -> pd.Series:
    """稳健转数值：非数字变 NaN，而不是抛错中断整批。"""
    return pd.to_numeric(series, errors="coerce")


def clean_snapshot(rows: list[dict], trade_date: str) -> pd.DataFrame:
    """清洗全市场快照。

    Args:
        rows: 新浪接口原始行
        trade_date: 交易日期 YYYY-MM-DD，作为分区键与主键组成部分

    Returns:
        含 trade_date 列、去重后的 DataFrame，主键为 (trade_date, symbol)
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # 补齐接口偶发缺失的列，保证 schema 稳定
    for col in ["symbol", "code", "name"]:
        if col not in df.columns:
            df[col] = None

    for col in _SNAPSHOT_NUMERIC:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = _to_numeric(df[col])

    df["symbol"] = df["symbol"].astype(str).str.strip().str.lower()
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df["name"] = df["name"].astype(str).str.strip()
    df["trade_date"] = pd.to_datetime(trade_date).date()

    # 过滤明显无效行：无代码、无价格
    before = len(df)
    df = df[df["code"].str.len() == 6]
    df = df[df["trade"].notna() & (df["trade"] > 0)]
    dropped = before - len(df)
    if dropped:
        logger.info("clean_snapshot: dropped %d invalid rows", dropped)

    # 主键去重：同一交易日内同一代码只保留最后一条
    df = df.drop_duplicates(subset=["trade_date", "code"], keep="last")

    # 市值接口单位是万元，转成亿元便于阅读
    for col in ("mktcap", "nmc"):
        if col in df.columns:
            df[col] = df[col] / 10000.0

    # 负 PE 代表亏损，保留原值但另设标记，避免下游误用
    df["is_loss"] = (df["per"] < 0).fillna(False)

    keep = [
        "trade_date", "symbol", "code", "name", "trade", "open", "high", "low",
        "settlement", "changepercent", "volume", "amount", "per", "pb",
        "mktcap", "nmc", "turnoverratio", "is_loss", "ticktime",
    ]
    keep = [c for c in keep if c in df.columns]
    return df[keep].reset_index(drop=True)


def clean_kline(rows: list[dict], trade_date: str) -> pd.DataFrame:
    """清洗 K 线数据。

    腾讯返回列顺序为 open/close/high/low/volume，此处已在上游显式映射，
    这里只做类型转换与约束校验。

    关键的脏数据检查：high 必须 >= max(open, close)，low 必须 <= min(open, close)。
    不满足说明接口返回错位或复权异常，直接丢弃该行并计数。
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["open", "close", "high", "low", "volume"]:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = _to_numeric(df[col])

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)

    before = len(df)
    df = df.dropna(subset=["date", "code"])
    df = df[df["close"].notna() & (df["close"] > 0)]

    # OHLC 一致性校验
    ohlc_ok = (
        (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["high"] >= df["low"])
    )
    bad = (~ohlc_ok).sum()
    if bad:
        logger.warning("clean_kline: %d rows failed OHLC sanity, dropped", bad)
    df = df[ohlc_ok]

    # 同一 (code, date) 去重
    df = df.drop_duplicates(subset=["code", "date"], keep="last")

    df["trade_date"] = pd.to_datetime(trade_date).date()
    df["turnover"] = df["volume"] * df["close"]  # 近似成交额，K线接口不单独给

    keep = [
        "code", "symbol", "date", "period", "adjust_type",
        "open", "high", "low", "close", "volume", "turnover", "trade_date",
    ]
    keep = [c for c in keep if c in df.columns]
    logger.info(
        "clean_kline: %d rows in -> %d rows out", before, len(df)
    )
    return df[keep].reset_index(drop=True)
