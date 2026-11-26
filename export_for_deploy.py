# -*- coding: utf-8 -*-
"""把本地 DuckDB 导出为云端部署用的精简 Parquet 包。

为什么需要这一步：
- Streamlit Community Cloud 从 GitHub 拉代码，仓库里放不下 155MB 的 DuckDB；
- 云端也没有本机的采集环境，无法现场跑 ETL；
- 但看板只需要 mart 层的聚合结果，原始 K 线完全不必上传。

因此导出「看板真正会查的字段」，压到 20MB 以内，随代码一起进仓库。

用法：
    python export_for_deploy.py              # 全量导出
    python export_for_deploy.py --days 250   # 个股只保留最近 N 个交易日
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "market.duckdb"
OUT_DIR = ROOT / "data" / "deploy"

# 每张表的导出定义：目标文件名 -> SQL
# 只选看板用到的列，多余的列会让文件变大几十 MB 却没任何用处。
EXPORTS: dict[str, str] = {
    "industry_daily.parquet": """
        SELECT date, industry_name, n_stocks,
               mom_5d, mom_20d, mom_60d, vol_20d, ret_1d, future_return
        FROM mart_industry_daily
    """,
    "breadth.parquet": """
        SELECT trade_date, n_stocks, n_up, n_down, n_limit_up, n_limit_down,
               avg_change, total_amount_yi, median_per, median_pb
        FROM mart_market_breadth
    """,
    "quality.parquet": """
        SELECT trade_date, n_snapshot, n_compared, coverage_pct,
               mean_dev_pct, pct_over_1, quality
        FROM mart_data_quality
    """,
    "industry_dim.parquet": """
        SELECT node, industry_name, industry_code FROM dim_industry
    """,
    "stock_industry.parquet": """
        SELECT code, industry_name, node FROM dim_stock_industry
    """,
    "snapshot.parquet": """
        SELECT s.trade_date, s.code, s.name, s.trade, s.changepercent,
               s.per, s.pb, s.mktcap, s.nmc, s.turnoverratio,
               si.industry_name
        FROM stg_snapshot s
        LEFT JOIN (
            SELECT code, MIN(industry_name) AS industry_name
            FROM dim_stock_industry GROUP BY code
        ) si ON si.code = s.code
        WHERE s.trade_date = (SELECT MAX(trade_date) FROM stg_snapshot)
    """,
}


def export_stock_daily(conn: duckdb.DuckDBPyConnection, days: int) -> str:
    """个股日表单独处理：按天数裁剪，它是体积最大的表。"""
    return f"""
        SELECT code, date, name, industry_name, close, ret_1d,
               mom_5d, mom_20d, mom_60d, mom_120d,
               vol_20d, ma20_dev, ma60_dev, dd_20d, volume_ratio, future_return
        FROM mart_stock_daily
        WHERE date >= (SELECT MAX(date) - INTERVAL {days} DAY FROM mart_stock_daily)
    """


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=400,
                        help="个股日表保留最近多少自然日")
    args = parser.parse_args()

    if not DB.is_file():
        print(f"ERROR: {DB} not found. Run the pipeline first.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 清掉旧文件，避免残留过期表误导看板
    for old in OUT_DIR.glob("*.parquet"):
        old.unlink()

    conn = duckdb.connect(str(DB), read_only=True)
    try:
        jobs = dict(EXPORTS)
        jobs["stock_daily.parquet"] = export_stock_daily(conn, args.days)

        total = 0
        print(f"exporting to {OUT_DIR}")
        print("-" * 62)

        for fname, sql in jobs.items():
            target = OUT_DIR / fname
            conn.execute(
                f"COPY ({sql}) TO '{target.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            size = target.stat().st_size
            total += size
            rows = conn.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
            print(f"  {fname:28s} {rows:>9,} rows  {size/1024:>8.0f} KB")

        print("-" * 62)
        print(f"  {'TOTAL':28s} {'':>9}       {total/1024/1024:>7.1f} MB")

        # GitHub 单文件硬上限 100MB，整体建议控制在 50MB 内
        if total > 50 * 1024 * 1024:
            print("\nWARNING: package exceeds 50MB; consider reducing --days")
            return 2
        print("\nOK: package is small enough for GitHub / Streamlit Cloud")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
