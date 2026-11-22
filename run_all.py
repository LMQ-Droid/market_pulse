# -*- coding: utf-8 -*-
"""一键跑完整管道（Windows 任务计划可每天调用）。

用法：
    python run_all.py              # 快照 + 行业 + 800只K线 + 建模
    python run_all.py --limit 300  # 少抓一些，跑得快
    python run_all.py --skip-fetch # 只重建因子与宽表，不联网

配合 Windows 任务计划程序：
    程序:      D:\\python.exe
    参数:      run_all.py
    起始于:    C:\\Users\\<你>\\Desktop\\deepseek\\market_pulse
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def step(title: str, args: list[str]) -> int:
    print("\n" + "=" * 70)
    print(f"[{datetime.now():%H:%M:%S}] {title}")
    print("=" * 70)
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "src.pipeline.run", *args],
        cwd=ROOT,
    )
    print(f"-> exit={proc.returncode} in {time.monotonic()-t0:.1f}s")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=800,
                        help="抓取 K 线的股票数（按成交额排序取前 N）")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="跳过采集，只重建因子与宽表")
    args = parser.parse_args()

    started = datetime.now()
    print(f"market_pulse daily run started at {started:%Y-%m-%d %H:%M:%S}")

    codes: list[int] = []

    if not args.skip_fetch:
        codes.append(step("1/4 全市场快照", ["--task", "snapshot"]))
        if codes[-1] != 0:
            print("snapshot failed, aborting")
            return codes[-1]

        codes.append(step("2/4 行业分类", ["--task", "industry"]))
        # 行业失败不致命，因子仍可算个股层
        if codes[-1] != 0:
            print("industry failed, continuing without industry mapping")

        codes.append(step(f"3/4 K线采集 ({args.limit} 只)",
                          ["--task", "kline", "--limit", str(args.limit)]))

    codes.append(step("4/4 因子与宽表", ["--task", "build"]))

    elapsed = (datetime.now() - started).total_seconds()
    ok = all(c == 0 for c in codes)
    print("\n" + "=" * 70)
    print(f"{'SUCCESS' if ok else 'PARTIAL FAILURE'} in {elapsed:.1f}s "
          f"({elapsed/60:.1f} min)")
    print(f"看板启动: streamlit run dashboard/app.py")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
