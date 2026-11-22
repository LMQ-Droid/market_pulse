# -*- coding: utf-8 -*-
"""因子层：从 K 线和快照派生分析用宽表。

所有因子计算严格遵守「只用当日及之前数据」，防止未来函数：
- 收益类因子用 shift 向后看
- 滚动窗口用 min_periods 限制，避免早期样本被少数点撑满
- 预测目标（future_return）单独成列并明确标注，绝不可作为特征
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # 仅用于类型注解，运行时不需要 duckdb
    import duckdb

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 个股因子


def compute_stock_factors(kline: pd.DataFrame) -> pd.DataFrame:
    """按股票分组计算动量、波动、均线偏离等因子。

    输入需含 code/date/close/high/low/volume，输出为同粒度因子表。
    """
    if kline.empty:
        return pd.DataFrame()

    df = kline.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    g = df.groupby("code", group_keys=False)

    # 日收益率
    df["ret_1d"] = g["close"].pct_change()

    # 多周期动量：过去 N 日累计收益
    for n in settings.MOMENTUM_WINDOWS:
        df[f"mom_{n}d"] = g["close"].transform(lambda s, n=n: s / s.shift(n) - 1)

    # 年化波动率：20 日收益率标准差 × sqrt(250)
    df["vol_20d"] = g["ret_1d"].transform(
        lambda s: s.rolling(settings.VOLATILITY_WINDOW, min_periods=10).std()
        * np.sqrt(settings.TRADING_DAYS_PER_YEAR)
    )

    # 均线偏离度：收盘价相对 20/60 日均线的偏离
    for n in (20, 60):
        ma = g["close"].transform(lambda s, n=n: s.rolling(n, min_periods=n // 2).mean())
        df[f"ma{n}_dev"] = df["close"] / ma - 1

    # 最大回撤（20 日窗口内）
    def rolling_dd(s: pd.Series) -> pd.Series:
        roll_max = s.rolling(20, min_periods=10).max()
        return s / roll_max - 1

    df["dd_20d"] = g["close"].transform(rolling_dd)

    # 成交量比率：当前量 / 20 日均量，衡量放量程度
    vol_ma = g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["volume_ratio"] = df["volume"] / vol_ma

    # 预测目标：未来 5 日收益。这是 label，不是特征，下游必须排除。
    df["future_return"] = g["close"].transform(
        lambda s: s.shift(-settings.FUTURE_RETURN_DAYS) / s - 1
    )

    factor_cols = [
        "code", "date", "close",
        "ret_1d", "vol_20d", "ma20_dev", "ma60_dev", "dd_20d", "volume_ratio",
        "future_return",
    ] + [f"mom_{n}d" for n in settings.MOMENTUM_WINDOWS]

    return df[[c for c in factor_cols if c in df.columns]].reset_index(drop=True)


# ---------------------------------------------------------------- 行业因子


def compute_industry_factors(
    conn: "duckdb.DuckDBPyConnection",
    factors: pd.DataFrame,
    stock_industry: pd.DataFrame,
) -> pd.DataFrame:
    """把个股因子聚合成行业因子：等权平均 + 成分股数量。

    行业收益采用等权而非市值加权，理由是等权更能反映「板块整体情绪」，
    且不受少数超大盘股左右，适合做轮动判断。
    """
    if factors.empty or stock_industry.empty:
        return pd.DataFrame()

    merged = factors.merge(
        stock_industry[["code", "industry_name"]].drop_duplicates("code"),
        on="code",
        how="inner",
    )
    if merged.empty:
        return pd.DataFrame()

    agg_cols = ["ret_1d", "vol_20d", "ma20_dev", "future_return"] + [
        f"mom_{n}d" for n in settings.MOMENTUM_WINDOWS
    ]
    agg_cols = [c for c in agg_cols if c in merged.columns]

    grouped = merged.groupby(["date", "industry_name"], as_index=False).agg(
        {**{c: "mean" for c in agg_cols}, "code": "nunique"}
    ).rename(columns={"code": "n_stocks"})

    return grouped


# ---------------------------------------------------------------- mart 建表


MART_DDL = """
CREATE OR REPLACE TABLE mart_stock_daily AS
SELECT
    f.code,
    f.date,
    s.name,
    si.industry_name,
    f.close,
    f.ret_1d,
    f.vol_20d,
    f.ma20_dev,
    f.ma60_dev,
    f.dd_20d,
    f.volume_ratio,
    {momentum_cols},
    f.future_return
FROM stock_factors f
LEFT JOIN stg_snapshot s
    ON s.code = f.code
    AND s.trade_date = (SELECT MAX(trade_date) FROM stg_snapshot)
LEFT JOIN (
    SELECT code, MIN(industry_name) AS industry_name
    FROM dim_stock_industry GROUP BY code
) si ON si.code = f.code;

CREATE OR REPLACE TABLE mart_industry_daily AS
SELECT * FROM industry_factors;

CREATE OR REPLACE TABLE mart_market_breadth AS
SELECT
    trade_date,
    COUNT(*)                                    AS n_stocks,
    SUM(CASE WHEN changepercent > 0 THEN 1 ELSE 0 END)  AS n_up,
    SUM(CASE WHEN changepercent < 0 THEN 1 ELSE 0 END)  AS n_down,
    SUM(CASE WHEN changepercent >= 9.8 THEN 1 ELSE 0 END) AS n_limit_up,
    SUM(CASE WHEN changepercent <= -9.8 THEN 1 ELSE 0 END) AS n_limit_down,
    AVG(changepercent)                          AS avg_change,
    SUM(amount) / 1e8                           AS total_amount_yi,
    MEDIAN(per)                                 AS median_per,
    MEDIAN(pb)                                  AS median_pb
FROM stg_snapshot
GROUP BY trade_date
ORDER BY trade_date;
"""


def build_mart(
    conn: "duckdb.DuckDBPyConnection",
    stock_factors: pd.DataFrame,
    industry_factors: pd.DataFrame,
) -> None:
    """物化 mart 层。BI 只读这三张表。"""
    momentum_cols = ", ".join(
        f"f.mom_{n}d" for n in settings.MOMENTUM_WINDOWS
    )

    conn.register("stock_factors", stock_factors)
    conn.register("industry_factors", industry_factors)
    try:
        conn.execute(MART_DDL.format(momentum_cols=momentum_cols))
    finally:
        conn.unregister("stock_factors")
        conn.unregister("industry_factors")

    for tbl in ("mart_stock_daily", "mart_industry_daily", "mart_market_breadth"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        logger.info("mart built: %s (%d rows)", tbl, n)
