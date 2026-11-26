# -*- coding: utf-8 -*-
"""看板数据访问层：同时支持 DuckDB（本地）与 Parquet（云端部署）。

为什么要这层抽象：
- 本地开发时有完整 DuckDB，查询快、字段全。
- 云端（Streamlit Community Cloud）不能放 155MB 的库文件，
  只能用导出的精简 Parquet。

如果看板里直接写 duckdb.connect()，云端必然崩。
所以把所有数据访问收口到这里，由 DATA_MODE 决定走哪条路。

本地行为完全不变（自动检测到 market.duckdb 就用它）。
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent   # 项目根目录（不是 dashboard/）

# Parquet 导出目录：项目根的 data/deploy。
# 注意不能写成 ROOT/"data"，因为 ROOT 已上溯到项目根。
PARQUET_DIR = ROOT / "data" / "deploy"


# ---------------------------------------------------------------- 模式判定


def _find_duckdb() -> Path | None:
    """找本地 DuckDB。存在就用它，不存在说明是云端精简模式。

    环境变量 MARKET_PULSE_DB 支持特殊值：
      "off" / "none" / "parquet" -> 强制走 Parquet 模式（用于测试云端行为）
      其他路径                     -> 用指定的库文件
    """
    env_db = os.getenv("MARKET_PULSE_DB", "").strip().lower()
    if env_db in {"off", "none", "parquet", "0", "false"}:
        return None

    candidates: list[Path] = []
    if env_db:
        candidates.append(Path(os.getenv("MARKET_PULSE_DB", "").strip()))
    candidates.append(ROOT / "data" / "market.duckdb")
    candidates.append(ROOT.parent / "data" / "market.duckdb")

    for c in candidates:
        # 必须同时是文件且非空，否则会误把目录当成库文件
        if c.is_file() and c.stat().st_size > 1024:
            return c
    return None


DB_PATH = _find_duckdb()
DATA_MODE = "duckdb" if DB_PATH else "parquet"

# 云端只读，本地也可能被 ETL 占用写锁，统一用只读打开
_conn: duckdb.DuckDBPyConnection | None = None


def _get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        _conn = duckdb.connect(str(DB_PATH), read_only=True)
    return _conn


# ---------------------------------------------------------------- 统一查询


def query(sql_duckdb: str, params: tuple = (),
          sql_parquet: str | None = None) -> pd.DataFrame:
    """执行查询。

    Args:
        sql_duckdb: 针对 DuckDB 表名的 SQL
        params: 查询参数。**必须用关键字传参**，避免与 sql_parquet 混淆。
        sql_parquet: 可选，Parquet 模式下的替代 SQL（表名与 DuckDB 一致，
                     通常不需要传）

    Raises:
        TypeError: params 传了非元组/列表，常见于把 SQL 误传成第二个位置参数。
    """
    if isinstance(params, str):
        raise TypeError(
            "params 必须是元组或列表，收到的是字符串。"
            "如果要传 Parquet 专用 SQL，请用 sql_parquet=... 关键字参数。"
        )
    params = tuple(params) if params else ()

    if DATA_MODE == "duckdb":
        # DuckDB 在没有参数时不能调用 execute(sql, ())，会报
        # "Values were not provided for prepared statement parameters"。
        if params:
            return _get_conn().execute(sql_duckdb, params).df()
        return _get_conn().execute(sql_duckdb).df()

    # Parquet 模式：用内存 DuckDB 把 parquet 注册成视图后执行同一条 SQL，
    # 两种模式共用 SQL，避免维护两套逻辑。
    sql = sql_parquet or sql_duckdb
    conn = duckdb.connect()
    conn.execute("SET threads=2")
    for name, fname in _PARQUET_VIEWS.items():
        path = PARQUET_DIR / fname
        if path.exists():
            conn.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{path.as_posix()}')"
            )
        else:
            # 视图缺文件时建空表，让看板优雅降级而不是崩掉
            conn.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT NULL AS _empty WHERE 1=0"
            )
    try:
        if params:
            return conn.execute(sql, params).df()
        return conn.execute(sql).df()
    finally:
        conn.close()


# 视图名 -> 文件名。表名保持与 DuckDB 一致，SQL 才能复用。
_PARQUET_VIEWS = {
    "mart_industry_daily": "industry_daily.parquet",
    "mart_stock_daily": "stock_daily.parquet",
    "mart_market_breadth": "breadth.parquet",
    "mart_data_quality": "quality.parquet",
    "stg_snapshot": "snapshot.parquet",
    "dim_industry": "industry_dim.parquet",
    "dim_stock_industry": "stock_industry.parquet",
}


def has_table(name: str) -> bool:
    """检查某张表/视图是否可用。"""
    if DATA_MODE == "duckdb":
        try:
            df = _get_conn().execute(
                "SELECT table_name FROM information_schema.tables"
            ).df()
            return name in set(df["table_name"])
        except Exception:
            return False

    fname = _PARQUET_VIEWS.get(name)
    if not fname:
        return False
    p = PARQUET_DIR / fname
    return p.exists() and p.stat().st_size > 0


def backend_label() -> str:
    """给看板显示当前数据来源，避免用户搞不清看的哪份数据。"""
    if DATA_MODE == "duckdb":
        size = DB_PATH.stat().st_size / 1024 / 1024
        return f"本地 DuckDB（{size:.0f} MB）"
    return "精简 Parquet（云端只读）"
