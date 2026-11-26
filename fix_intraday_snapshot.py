# -*- coding: utf-8 -*-
"""用全市场 K 线收盘价重建 11-25 的收盘快照。

背景：11-25 那次采集在盘中运行（14:15），快照存的是中间价，
导致当日涨跌幅、市场宽度、估值全部失真。

重建思路：
  快照表里缺的是「收盘价」。而 K 线表能提供当日收盘价。
  → 抓取全市场 K 线，取 11-25 的收盘价，重建整行快照。

关键约束：
  - 快照的原始行里其它字段（PE/PB/市值/换手率）是盘中值，无法回溯修正。
  - 这些字段与收盘价同源，理论上应同步缩放，但比例不可知。
  - 因此本脚本只修正「价格类」字段（trade/changepercent），
    估值类字段保持原值，并在 quality 表标注「价格已修正、估值未修正」。

这是诚实的做法：不假装全部修正了。
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, r"C:\Users\28456\Desktop\deepseek\market_pulse")

import duckdb

from src.extract import sina, source_health
from src.extract.http_client import HttpClient

DB = r"C:\Users\28456\Desktop\deepseek\market_pulse\data\market.duckdb"
TARGET_DATE = "2026-11-25"
BJ_PREFIX = ("43", "83", "87", "88", "92")


def main() -> int:
    # 1. 取需要重建的代码
    conn = duckdb.connect(DB, read_only=True)
    rows = conn.execute(
        "SELECT code, trade, settlement, high, low, open "
        "FROM stg_snapshot WHERE trade_date = ?",
        [TARGET_DATE],
    ).fetchall()
    conn.close()

    all_codes = [r[0] for r in rows]
    valid = [c for c in all_codes if not c.startswith(BJ_PREFIX)]
    print(f"快照 {len(all_codes)} 只，可修 {len(valid)} 只（排除北交所 {len(all_codes)-len(valid)} 只）")

    # 2. 抓 K 线收盘价
    close_map: dict[str, float] = {}
    prev_map: dict[str, float] = {}   # 前一交易日收盘，用于算涨跌幅
    failures: list[str] = []

    def work(client: HttpClient, code: str):
        try:
            bars = sina.fetch_kline(client, code, limit=12, min_bars=2)
            if not bars:
                return code, None, None
            # 找目标日与前一交易日
            dates = [str(b["date"]) for b in bars]
            if TARGET_DATE not in dates:
                return code, None, None
            i = dates.index(TARGET_DATE)
            close = float(bars[i]["close"])
            prev = float(bars[i - 1]["close"]) if i > 0 else None
            return code, close, prev
        except Exception:
            return code, None, None

    print("[1/3] 抓取全市场 K 线收盘价（约 5-6 分钟）...")
    t0 = time.time()
    done = 0

    with HttpClient() as client:
        health = source_health.probe_all(client)
        for h in health:
            print(f"  数据源 {h.name}: available={h.available}")
        if not any(h.available for h in health):
            print("所有数据源不可用，中止")
            return 1

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(work, client, c): c for c in valid}
            for fut in as_completed(futs):
                code, close, prev = fut.result()
                done += 1
                if close is not None:
                    close_map[code] = close
                    if prev:
                        prev_map[code] = prev
                else:
                    failures.append(code)
                if done % 500 == 0 or done == len(valid):
                    el = time.time() - t0
                    print(f"  {done}/{len(valid)} 命中 {len(close_map)} "
                          f"失败 {len(failures)} ({el:.0f}s)")

    print(f"[1/3] 完成：命中 {len(close_map)} 只，耗时 {time.time()-t0:.0f}s")
    if len(close_map) < len(valid) * 0.8:
        print("命中率过低，中止以免产生大范围空值")
        return 2

    # 3. 重建快照表
    print("[2/3] 重建 11-25 快照...")
    conn = duckdb.connect(DB)
    try:
        # 用 K 线收盘价覆盖 trade，并重算 changepercent
        conn.execute("BEGIN TRANSACTION")

        # 建临时表装新价格
        conn.execute("DROP TABLE IF EXISTS _new_prices")
        conn.execute(
            "CREATE TEMP TABLE _new_prices (code VARCHAR, close DOUBLE, prev DOUBLE)"
        )

        data = [
            (c, close_map[c], prev_map.get(c))
            for c in close_map
        ]
        conn.executemany("INSERT INTO _new_prices VALUES (?, ?, ?)", data)

        # 更新快照：trade 改为收盘价，changepercent 用 (close-prev)/prev*100 重算
        conn.execute("""
            UPDATE stg_snapshot AS s
            SET trade = p.close,
                changepercent = CASE
                    WHEN p.prev IS NOT NULL AND p.prev > 0
                    THEN (p.close - p.prev) / p.prev * 100
                    ELSE s.changepercent
                END
            FROM _new_prices AS p
            WHERE s.code = p.code AND s.trade_date = ?
        """, [TARGET_DATE])

        n = conn.execute(
            "SELECT COUNT(*) FROM stg_snapshot WHERE trade_date = ?", [TARGET_DATE]
        ).fetchone()[0]

        # 记录修正范围：多少只被修正、多少只仍是盘中价
        fixed = len(close_map)
        unfixed = n - fixed

        conn.execute("DROP TABLE IF EXISTS _new_prices")
        conn.execute("COMMIT")
        print(f"[2/3] 完成：更新 {fixed} 只，剩余 {unfixed} 只未修正（无 K 线）")
    except Exception as exc:
        conn.execute("ROLLBACK")
        print(f"[2/3] 失败：{exc}")
        conn.close()
        return 3

    # 4. 标记修正记录
    print("[3/3] 记录修正日志...")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_fix_log (
                trade_date DATE, fixed_rows BIGINT, unfixed_rows BIGINT,
                fixed_at TIMESTAMP, method VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO snapshot_fix_log VALUES (?, ?, ?, current_timestamp, ?)
        """, [TARGET_DATE, fixed, unfixed,
              "用K线收盘价覆盖trade并重算changepercent；PE/PB/市值为盘中值未修正"])
        print("[3/3] 已记录到 snapshot_fix_log")
    finally:
        conn.close()

    print(f"\n完成。总耗时 {time.time()-t0:.0f}s")
    print("下一步：运行 build 重算因子与市场宽度，然后导出推送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
