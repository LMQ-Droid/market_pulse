# -*- coding: utf-8 -*-
"""快照数据质量评估。

背景：这个项目踩过一个真实的坑——在 14:15 手动跑采集，A股 15:00 才收盘，
抓到的「快照」其实是**盘中中间价**。后果是那一整天的涨跌幅、市场宽度、
估值中位数全部失真，而且看板上完全看不出来。

更糟的是：这些错误数据会永久留在库里，与收盘价数据混在一起。

解决办法：**给每一天的截面数据打质量标签**。

判定方法（不依赖模型，纯确定性）：
    同一天同一只股票，快照价 pace 与 K 线收盘价应当几乎相等。
    偏差大 = 快照取自盘中。

    ┌─ mean_dev < 0.2%  → close   （收盘价，可信）
    ├─ mean_dev ≥ 0.2%  → intraday（盘中价，仅供参考）
    └─ 无可比对样本     → unknown （无法判定）

为什么用「快照 vs K线收盘」而不是「当前时间是否收盘」：
时间只能判断"现在"，无法判断**历史某天**当时是不是盘中采的。
而价格比对是事后可验证的、对历史数据同样有效的证据。
"""
from __future__ import annotations

import logging

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

# 平均偏差阈值（百分比）。
# 实测收盘采集的偏差是 0.00%，盘中采集是 2%~3%，两者的区分度非常大，
# 取 0.2% 既不会被复权误差误判，也能抓住盘中数据。
INTRADAY_THRESHOLD_PCT = 0.2

# 单只股票"偏差过大"的判定门槛，用于算占比（辅助指标）
STOCK_DEVIATION_PCT = 1.0

# 至少需要多少只可比对样本才敢下结论
MIN_SAMPLES = 30


QUALITY_LABELS = {
    "close": "收盘价",
    "intraday": "盘中价",
    "unknown": "未校验",
}

# 供看板展示的修正建议
FIX_HINT = "收盘后（15:00 之后）重新运行 daily_update.py 可修正"


def assess(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """评估所有交易日的快照质量。

    Returns:
        DataFrame[trade_date, n_compared, mean_dev_pct, pct_over_1, quality]
    """
    try:
        df = conn.execute(
            """
            SELECT
                s.trade_date,
                COUNT(*)                                    AS n_compared,
                AVG(ABS(s.trade - k.close) / k.close * 100) AS mean_dev_pct,
                AVG(CASE WHEN ABS(s.trade - k.close) / k.close * 100
                         > ? THEN 1.0 ELSE 0.0 END)         AS pct_over_1
            FROM stg_snapshot s
            JOIN stg_kline k
              ON k.code = s.code AND k.date = s.trade_date
            WHERE k.close > 0 AND s.trade > 0
            GROUP BY s.trade_date
            ORDER BY s.trade_date
            """,
            [STOCK_DEVIATION_PCT],
        ).df()
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照质量评估失败（可能是表不存在）: %s", exc)
        return pd.DataFrame()

    if df.empty:
        return df

    def classify(row: pd.Series) -> str:
        if row["n_compared"] < MIN_SAMPLES:
            return "unknown"
        return "intraday" if row["mean_dev_pct"] > INTRADAY_THRESHOLD_PCT else "close"

    df["quality"] = df.apply(classify, axis=1)
    df["mean_dev_pct"] = df["mean_dev_pct"].round(3)
    df["pct_over_1"] = (df["pct_over_1"] * 100).round(1)
    return df


def build_quality_table(conn: duckdb.DuckDBPyConnection) -> int:
    """物化质量评估结果到 mart_data_quality。

    每次 build 都全量重算——这一天数级别的表重算成本可忽略，
    而且能自动修正"盘中数据事后被收盘数据覆盖"的情况。
    """
    df = assess(conn)
    if df.empty:
        logger.warning("无可评估的快照数据，跳过质量表")
        conn.execute(
            """
            CREATE OR REPLACE TABLE mart_data_quality AS
            SELECT NULL::DATE AS trade_date, 0::BIGINT AS n_compared,
                   0.0::DOUBLE AS mean_dev_pct, 0.0::DOUBLE AS pct_over_1,
                   ''::VARCHAR AS quality WHERE 1=0
            """
        )
        return 0

    conn.register("_quality_incoming", df)
    try:
        conn.execute(
            """
            CREATE OR REPLACE TABLE mart_data_quality AS
            SELECT trade_date, n_compared, mean_dev_pct, pct_over_1, quality
            FROM _quality_incoming
            """
        )
    finally:
        conn.unregister("_quality_incoming")

    counts = df["quality"].value_counts().to_dict()
    logger.info(
        "mart_data_quality 已重建：%d 天（%s）",
        len(df),
        "，".join(f"{QUALITY_LABELS.get(k, k)} {v} 天" for k, v in counts.items()),
    )

    bad = df[df["quality"] == "intraday"]
    if not bad.empty:
        dates = "、".join(str(d)[:10] for d in bad["trade_date"].tolist()[:5])
        logger.warning(
            "⚠️ 检测到 %d 天为盘中数据（%s），涨跌幅与估值可能失真",
            len(bad), dates,
        )

    return len(df)


def check_latest(conn: duckdb.DuckDBPyConnection) -> dict | None:
    """返回最新交易日的质量信息，供 daily_update 做收盘校验。

    优先读已建好的 mart_data_quality；表不存在时现场算一次。
    """
    try:
        row = conn.execute(
            """
            SELECT trade_date, n_compared, mean_dev_pct, quality
            FROM mart_data_quality
            WHERE trade_date = (SELECT MAX(trade_date) FROM mart_data_quality)
            """
        ).fetchone()
        if row:
            return {
                "trade_date": str(row[0])[:10],
                "n_compared": int(row[1]),
                "mean_dev_pct": float(row[2]),
                "quality": row[3],
            }
    except Exception:  # noqa: BLE001
        pass

    # 回退：现场算
    df = assess(conn)
    if df.empty:
        return None
    last = df.iloc[-1]
    return {
        "trade_date": str(last["trade_date"])[:10],
        "n_compared": int(last["n_compared"]),
        "mean_dev_pct": float(last["mean_dev_pct"]),
        "quality": last["quality"],
    }
