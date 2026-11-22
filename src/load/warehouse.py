# -*- coding: utf-8 -*-
"""DuckDB 数仓层：raw 落地、staging 幂等写入、mart 建表。

三层职责严格分离：
- raw:   原始 JSON 按 dt= 分区落 Parquet，只追加、永不覆盖。
- staging: 清洗后的强类型表，按业务主键 MERGE（幂等）。
- mart:  宽表与因子表，只由 staging 派生，BI 只读这一层。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- raw 层


def write_raw(topic: str, payload: Any, trade_date: str) -> Path:
    """把原始响应落到 data/raw/{topic}/dt=YYYY-MM-DD/part-0.json

    只追加不覆盖：同一天重复采集会得到 part-1.json、part-2.json……
    这样即使解析逻辑出了 bug，也能回到原始数据重放，而不必重新抓网。
    """
    target_dir = settings.RAW_DIR / topic / f"dt={trade_date}"
    target_dir.mkdir(parents=True, exist_ok=True)

    idx = 0
    while (target_dir / f"part-{idx}.json").exists():
        idx += 1
    path = target_dir / f"part-{idx}.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    logger.info("raw written: %s (%d bytes)", path, path.stat().st_size)
    return path


def write_raw_parquet(df: pd.DataFrame, topic: str, trade_date: str) -> Path:
    """清洗后数据直接落 Parquet 到 raw 层，便于后续快速重放。"""
    target_dir = settings.RAW_DIR / topic / f"dt={trade_date}"
    target_dir.mkdir(parents=True, exist_ok=True)

    idx = 0
    while (target_dir / f"part-{idx}.parquet").exists():
        idx += 1
    path = target_dir / f"part-{idx}.parquet"
    df.to_parquet(path, index=False)
    logger.info("raw parquet written: %s (%d rows)", path, len(df))
    return path


# ---------------------------------------------------------------- 连接


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接。BI 侧应以 read_only=True 打开，避免写锁冲突。"""
    settings.DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(settings.DUCKDB_PATH), read_only=read_only)


# ---------------------------------------------------------------- schema

DDL = """
CREATE TABLE IF NOT EXISTS stg_snapshot (
    trade_date      DATE        NOT NULL,
    symbol          VARCHAR     NOT NULL,
    code            VARCHAR     NOT NULL,
    name            VARCHAR,
    trade           DOUBLE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    settlement      DOUBLE,
    changepercent   DOUBLE,
    volume          DOUBLE,
    amount          DOUBLE,
    per             DOUBLE,
    pb              DOUBLE,
    mktcap          DOUBLE,
    nmc             DOUBLE,
    turnoverratio   DOUBLE,
    is_loss         BOOLEAN,
    ticktime        VARCHAR,
    _loaded_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS stg_kline (
    code            VARCHAR     NOT NULL,
    symbol          VARCHAR,
    date            DATE        NOT NULL,
    period          VARCHAR,
    adjust_type     VARCHAR,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          DOUBLE,
    turnover        DOUBLE,
    trade_date      DATE,
    _loaded_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (code, date, period)
);

CREATE TABLE IF NOT EXISTS dim_industry (
    node            VARCHAR     PRIMARY KEY,
    industry_name   VARCHAR,
    industry_code   VARCHAR
);

CREATE TABLE IF NOT EXISTS dim_stock_industry (
    code            VARCHAR     NOT NULL,
    industry_name   VARCHAR,
    node            VARCHAR,
    PRIMARY KEY (code, node)
);

CREATE TABLE IF NOT EXISTS etl_run_log (
    run_id          VARCHAR,
    task            VARCHAR,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    status          VARCHAR,
    rows_in         BIGINT,
    rows_out        BIGINT,
    failures        BIGINT,
    seconds         DOUBLE,
    detail          VARCHAR
);
"""


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(DDL)
    logger.info("schema ensured")


# ---------------------------------------------------------------- upsert


def upsert_snapshot(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """按 (trade_date, code) 幂等写入快照。

    用 DELETE + INSERT 在同一事务内完成，DuckDB 的 ON CONFLICT 对
    复合主键支持有限，此写法在单机场景等价且更直观。
    """
    if df.empty:
        return 0

    dates = df["trade_date"].unique().tolist()
    conn.execute("BEGIN TRANSACTION")
    try:
        for d in dates:
            conn.execute("DELETE FROM stg_snapshot WHERE trade_date = ?", [d])
        conn.register("_incoming_snapshot", df)
        conn.execute("""
            INSERT INTO stg_snapshot (
                trade_date, symbol, code, name, trade, open, high, low,
                settlement, changepercent, volume, amount, per, pb,
                mktcap, nmc, turnoverratio, is_loss, ticktime
            )
            SELECT trade_date, symbol, code, name, trade, open, high, low,
                   settlement, changepercent, volume, amount, per, pb,
                   mktcap, nmc, turnoverratio, is_loss, ticktime
            FROM _incoming_snapshot
        """)
        conn.unregister("_incoming_snapshot")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(df)


def upsert_kline(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """按 (code, date, period) 幂等写入 K 线。

    只删除「来数据的那段时间窗」，避免把窗口外的历史数据一起清掉。
    """
    if df.empty:
        return 0

    conn.execute("BEGIN TRANSACTION")
    try:
        pairs = df[["code", "date"]].drop_duplicates()
        conn.register("_incoming_keys", pairs)
        conn.execute("""
            DELETE FROM stg_kline
            WHERE (code, date, period) IN (
                SELECT k.code, k.date, ? FROM _incoming_keys k
            )
        """, [df["period"].iloc[0]])
        conn.unregister("_incoming_keys")

        conn.register("_incoming_kline", df)
        conn.execute("""
            INSERT INTO stg_kline (
                code, symbol, date, period, adjust_type,
                open, high, low, close, volume, turnover, trade_date
            )
            SELECT code, symbol, date, period, adjust_type,
                   open, high, low, close, volume, turnover, trade_date
            FROM _incoming_kline
        """)
        conn.unregister("_incoming_kline")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(df)


def upsert_dim_industry(conn: duckdb.DuckDBPyConnection, nodes: list[dict[str, str]]) -> int:
    if not nodes:
        return 0
    df = pd.DataFrame(nodes).rename(
        columns={"name": "industry_name", "code": "industry_code"}
    )[["node", "industry_name", "industry_code"]]

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM dim_industry")
        conn.register("_incoming_industry", df)
        conn.execute("INSERT INTO dim_industry SELECT * FROM _incoming_industry")
        conn.unregister("_incoming_industry")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(df)


def upsert_stock_industry(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    conn.execute("BEGIN TRANSACTION")
    try:
        nodes = df["node"].unique().tolist()
        placeholders = ",".join("?" * len(nodes))
        conn.execute(f"DELETE FROM dim_stock_industry WHERE node IN ({placeholders})", nodes)
        conn.register("_incoming_stock_industry", df[["code", "industry_name", "node"]])
        conn.execute("""
            INSERT INTO dim_stock_industry
            SELECT code, industry_name, node FROM _incoming_stock_industry
        """)
        conn.unregister("_incoming_stock_industry")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(df)


def load_raw_parquet(pattern: str) -> pd.DataFrame:
    """从 raw 层回放数据，用于重建 staging 而不重新抓网。"""
    conn = duckdb.connect()
    try:
        return conn.execute(
            f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
        ).df()
    finally:
        conn.close()
