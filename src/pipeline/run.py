# -*- coding: utf-8 -*-
"""管道 CLI 入口：串联采集 → 清洗 → 校验 → 加载 → 建模。

用法：
    python -m src.pipeline.run --task snapshot
    python -m src.pipeline.run --task kline --limit 500
    python -m src.pipeline.run --task industry
    python -m src.pipeline.run --task build
    python -m src.pipeline.run --task all --limit 300

每个 task 都是独立可重跑的，且失败不会留下半截脏数据
（写库前先过质量断言，事务内 upsert）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

import pandas as pd

from config import settings
from src.extract import sina, source_health, tencent
from src.extract.http_client import HttpClient
from src.features import factors as feat
from src.load import warehouse as wh
from src.quality import checks
from src.transform import clean

logger = logging.getLogger("pipeline")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 第三方库日志太吵，压到 WARNING
    for noisy in ("urllib3", "requests", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- 任务


def resolve_trade_date(client: HttpClient | None = None) -> str:
    """确定本次快照对应的**交易日**，而不是运行日。

    为什么要区分：周末或盘后运行管道时，「今天」并不是交易日。
    如果直接用 date.today()，快照表会出现 2026-11-22（周六）这样的日期，
    而 K 线的最后交易日是 2026-11-20（周五），两张表日期对不上，
    下游按日期 join / 过滤就会全部落空。

    做法：取一只高流动性样本股的最近 K 线日期作为交易日。
    两个源依次尝试；都取不到时退回今天，并在日志里明确告警。
    """
    if client is None:
        return date.today().isoformat()

    probe_codes = ["600519", "000001", "601398"]

    # 先试新浪（实测最稳）
    for code in probe_codes:
        try:
            rows = sina.fetch_kline(client, code, limit=5, min_bars=1)
            if rows:
                logger.info("trade date resolved via sina %s: %s", code, rows[-1]["date"])
                return str(rows[-1]["date"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("sina probe %s failed: %s", code, exc)

    # 再试腾讯
    for code in probe_codes:
        try:
            rows = tencent.fetch_kline(client, code, limit=5, min_bars=1)
            if rows:
                logger.info("trade date resolved via tencent %s: %s", code, rows[-1]["date"])
                return str(rows[-1]["date"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("tencent probe %s failed: %s", code, exc)

    logger.warning("cannot resolve trade date from any kline source, using today")
    return date.today().isoformat()


def task_snapshot(client: HttpClient) -> dict:
    """采集全市场截面快照 → 清洗 → 校验 → raw + staging。"""
    # 用交易日而非运行日，保证与 K 线表日期口径一致
    today = resolve_trade_date(client)
    logger.info("resolved trade_date=%s", today)

    with checks.RunLogger("snapshot") as run:
        logger.info("fetching full-market snapshot ...")
        rows = sina.fetch_snapshot_parallel(client)
        logger.info("fetched %d raw rows", len(rows))

        if not rows:
            raise checks.QualityError("snapshot returned zero rows")

        # raw 层先落地，保证即使后续校验失败也有原始数据可重放
        wh.write_raw("sina_snapshot", rows, today)

        df = clean.clean_snapshot(rows, today)

        report = checks.CheckReport()
        report.add(checks.check_row_count(
            df, "snapshot_row_count",
            settings.SNAPSHOT_MIN_ROWS, settings.SNAPSHOT_MAX_ROWS,
        ))
        report.add(checks.check_not_null(df, ["code", "name", "trade"]))
        report.add(checks.check_unique_key(df, ["trade_date", "code"]))
        report.add(checks.check_value_range(df, "changepercent", -21, 21))
        report.add(checks.check_value_range(df, "per", -5000, 5000, level="WARNING"))
        report.raise_if_critical()

        wh.write_raw_parquet(df, "snapshot_clean", today)

        conn = wh.connect()
        try:
            wh.init_schema(conn)
            n = wh.upsert_snapshot(conn, df)
            run.note(pages_fetched=len(rows) // settings.SINA_PAGE_SIZE + 1,
                     http=client.stats.as_dict(),
                     warnings=[r.name for r in report.warnings])
            record = run.finish("success", rows_in=len(rows), rows_out=n)
            checks.persist_run_log(conn, record)
        finally:
            conn.close()

    return {"rows_in": len(rows), "rows_out": n}


def task_industry(client: HttpClient) -> dict:
    """采集行业分类树与成分股映射。

    49 个行业 × 最多若干页，全部走并发，约 1-2 分钟。
    """
    with checks.RunLogger("industry") as run:
        nodes = sina.fetch_industry_nodes(client)
        if not nodes:
            raise checks.QualityError("industry nodes empty")

        members: list[dict] = []
        for node in nodes:
            try:
                rows = sina.fetch_industry_members(client, node["node"])
                for r in rows:
                    r["node"] = node["node"]
                    r["industry_name"] = node["name"]
                members.extend(rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("industry %s failed: %s", node["node"], exc)

        logger.info("industry members: %d rows", len(members))

        conn = wh.connect()
        try:
            wh.init_schema(conn)
            n_nodes = wh.upsert_dim_industry(conn, nodes)

            mem_df = pd.DataFrame(members)
            if not mem_df.empty:
                mem_df["code"] = mem_df["code"].astype(str).str.zfill(6)
                mem_df = mem_df.drop_duplicates(subset=["code", "node"])
                n_members = wh.upsert_stock_industry(conn, mem_df)
            else:
                n_members = 0

            run.note(industry_nodes=n_nodes, member_rows=n_members)
            record = run.finish("success", rows_in=len(members), rows_out=n_members)
            checks.persist_run_log(conn, record)
        finally:
            conn.close()

    return {"nodes": n_nodes, "members": n_members}


def task_kline(client: HttpClient, limit: int | None = None) -> dict:
    """采集股票池的 K 线 → 清洗 → 校验 → raw + staging。

    limit=None 时取全市场快照中市值最大的前 N 只（默认 800），
    避免一次抓 5500 只耗时半小时。
    """
    if limit is None:
        limit = 800

    # trade_date 在这里只是「数据写入时间」的标记，真正的交易日期在每行
    # 的 date 列里（来自 K 线本身），所以用运行日即可。
    today = date.today().isoformat()

    with checks.RunLogger("kline") as run:
        conn = wh.connect()
        try:
            wh.init_schema(conn)
            # 从快照里挑流动性最好的股票，保证数据有分析价值。
            #
            # 关键过滤：排除北交所（43/83/87/88/92 开头）。实测腾讯 K 线接口
            # 对北交所只返回 0~1 根，抓了也是空表，白耗请求与时间。
            # 排除后留出的额度用来多抓几只主板/创业板股票。
            codes = [
                r[0] for r in conn.execute(
                    """
                    SELECT code FROM stg_snapshot
                    WHERE trade_date = (SELECT MAX(trade_date) FROM stg_snapshot)
                      AND trade > 1
                      AND amount > 0
                      AND NOT (code LIKE '43%' OR code LIKE '83%' OR code LIKE '87%'
                               OR code LIKE '88%' OR code LIKE '92%')
                    ORDER BY amount DESC NULLS LAST
                    LIMIT ?
                    """,
                    [limit],
                ).fetchall()
            ]
        finally:
            conn.close()

        if not codes:
            raise checks.QualityError(
                "no codes available; run `--task snapshot` first"
            )

        logger.info("fetching kline for %d stocks ...", len(codes))

        # 采集前先探测源健康度并自动选源。单源脚本活不过一天，
        # 这一步让管道在某个源被封时自动降级，而不是整批失败。
        kline_source = source_health.pick_kline_source(client)

        def progress(done: int, total: int, rows: int, fails: int) -> None:
            logger.info(
                "  kline progress %d/%d (%.0f%%) rows=%d fails=%d",
                done, total, 100 * done / total, rows, fails,
            )

        if kline_source == "sina":
            rows, failures = sina.fetch_kline_batch(
                client, codes, limit=settings.KLINE_LIMIT,
                min_bars=tencent.MIN_VALID_BARS, on_progress=progress,
            )
        else:
            rows, failures = tencent.fetch_kline_batch(
                client, codes, on_progress=progress
            )
        logger.info("kline fetched: %d rows, %d failures", len(rows), len(failures))

        if not rows:
            raise checks.QualityError("kline returned zero rows")

        wh.write_raw("tencent_kline", rows[:2000], today)  # raw 抽样，控体积

        df = clean.clean_kline(rows, today)

        report = checks.CheckReport()
        report.add(checks.check_row_count(df, "kline_row_count", 1000))
        report.add(checks.check_not_null(df, ["code", "date", "close"]))
        report.add(checks.check_unique_key(df, ["code", "date", "period"]))
        report.add(checks.check_ohlc_consistency(df))
        report.add(checks.check_value_range(df, "close", 0.01, 100000))
        report.raise_if_critical()

        wh.write_raw_parquet(df, "kline_clean", today)

        conn = wh.connect()
        try:
            n = wh.upsert_kline(conn, df)
            run.note(
                codes=len(codes),
                kline_source=kline_source,
                failures=len(failures),
                failure_samples=failures[:5],
                http=client.stats.as_dict(),
            )
            record = run.finish("success", len(rows), n, failures=len(failures))
            checks.persist_run_log(conn, record)
        finally:
            conn.close()

    return {"rows": n, "failures": len(failures)}


def task_build() -> dict:
    """从 staging 派生因子与 mart 宽表。纯本地计算，不联网。"""
    with checks.RunLogger("build") as run:
        conn = wh.connect()
        try:
            wh.init_schema(conn)

            kline = conn.execute("SELECT * FROM stg_kline WHERE period='day'").df()
            if kline.empty:
                raise checks.QualityError("stg_kline empty; run --task kline first")

            logger.info("computing stock factors from %d bars ...", len(kline))
            stock_factors = feat.compute_stock_factors(kline)

            try:
                stock_industry = conn.execute(
                    "SELECT code, industry_name FROM dim_stock_industry"
                ).df()
            except Exception:
                stock_industry = pd.DataFrame()

            logger.info("aggregating industry factors ...")
            industry_factors = feat.compute_industry_factors(
                conn, stock_factors, stock_industry
            )

            feat.build_mart(conn, stock_factors, industry_factors)

            run.note(
                stock_factor_rows=len(stock_factors),
                industry_factor_rows=len(industry_factors),
            )
            record = run.finish(
                "success", len(kline), len(stock_factors)
            )
            checks.persist_run_log(conn, record)
        finally:
            conn.close()

    return {"stock_rows": len(stock_factors), "industry_rows": len(industry_factors)}


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="market_pulse ETL pipeline")
    parser.add_argument(
        "--task",
        choices=["snapshot", "kline", "industry", "build", "all"],
        required=True,
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="kline: number of stocks to fetch (default 800)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    started = datetime.now()
    logger.info("=" * 60)
    logger.info("task=%s limit=%s", args.task, args.limit)

    try:
        if args.task == "build":
            # 建模不联网，不需要 HTTP 客户端
            result = task_build()
        elif args.task == "all":
            with HttpClient() as client:
                r1 = task_snapshot(client)
                r2 = task_industry(client)
                r3 = task_kline(client, args.limit)
            result = {"snapshot": r1, "industry": r2, "kline": r3, "build": task_build()}
        else:
            with HttpClient() as client:
                if args.task == "snapshot":
                    result = task_snapshot(client)
                elif args.task == "industry":
                    result = task_industry(client)
                else:
                    result = task_kline(client, args.limit)

        elapsed = (datetime.now() - started).total_seconds()
        logger.info("=" * 60)
        logger.info("DONE in %.1fs: %s", elapsed, result)
        return 0

    except checks.QualityError as exc:
        logger.error("DATA QUALITY FAILURE: %s", exc)
        logger.error("pipeline aborted; nothing written downstream")
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
