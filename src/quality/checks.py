# -*- coding: utf-8 -*-
"""数据质量断言与运行日志。

设计理念：**坏数据比没数据更危险**。
宁可管道报错中止，也不要让错误的数字流进看板——看板上一个错的
涨跌幅，会让整份分析结论不可信。

断言分三级：
- CRITICAL: 不通过则中止，绝不写库
- WARNING:  记录并继续，事后可查
- INFO:     仅记录
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


class QualityError(Exception):
    """CRITICAL 级别断言失败，调用方应中止管道。"""


@dataclass
class CheckResult:
    name: str
    level: str          # CRITICAL / WARNING / INFO
    passed: bool
    message: str
    value: Any = None


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def critical_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == "CRITICAL" and not r.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == "WARNING" and not r.passed]

    def add(self, result: CheckResult) -> None:
        self.results.append(result)
        icon = "PASS" if result.passed else ("FAIL" if result.level == "CRITICAL" else "WARN")
        logger.info("[%s] %s: %s", icon, result.name, result.message)

    def raise_if_critical(self) -> None:
        fails = self.critical_failures
        if fails:
            detail = "; ".join(f"{r.name}({r.message})" for r in fails)
            raise QualityError(f"critical data quality failures: {detail}")


# ---------------------------------------------------------------- 通用断言


def check_row_count(
    df: pd.DataFrame,
    name: str,
    min_rows: int,
    max_rows: int | None = None,
    level: str = "CRITICAL",
) -> CheckResult:
    """行数在合理区间。

    下限防「接口返回空/报错但被当成空列表」；上限防「分页死循环
    把同一页重复抓了几十遍」。
    """
    n = len(df)
    passed = n >= min_rows and (max_rows is None or n <= max_rows)
    return CheckResult(
        name=name,
        level=level,
        passed=passed,
        message=f"rows={n}, expected [{min_rows}, {max_rows or 'inf'}]",
        value=n,
    )


def check_not_null(
    df: pd.DataFrame,
    columns: list[str],
    name: str = "not_null",
    level: str = "CRITICAL",
) -> CheckResult:
    """关键列不得有空值。"""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        return CheckResult(
            name=name, level=level, passed=False,
            message=f"missing columns: {missing}",
        )
    null_counts = {c: int(df[c].isna().sum()) for c in columns}
    total_null = sum(null_counts.values())
    bad = {c: n for c, n in null_counts.items() if n > 0}
    return CheckResult(
        name=name, level=level, passed=(total_null == 0),
        message=f"nulls={bad or 0}", value=total_null,
    )


def check_unique_key(
    df: pd.DataFrame,
    keys: list[str],
    name: str = "unique_key",
    level: str = "CRITICAL",
) -> CheckResult:
    """主键唯一性。重复主键会导致 upsert 时静默覆盖或翻倍。"""
    missing = [c for c in keys if c not in df.columns]
    if missing:
        return CheckResult(
            name=name, level=level, passed=False,
            message=f"missing key columns: {missing}",
        )
    dup = int(df.duplicated(subset=keys).sum())
    return CheckResult(
        name=name, level=level, passed=(dup == 0),
        message=f"duplicates={dup} on {keys}", value=dup,
    )


def check_value_range(
    df: pd.DataFrame,
    column: str,
    low: float,
    high: float,
    name: str | None = None,
    level: str = "WARNING",
) -> CheckResult:
    """取值落在合理区间，用于识别单位错误或数据错位。"""
    name = name or f"range_{column}"
    if column not in df.columns:
        return CheckResult(name=name, level=level, passed=False,
                           message=f"column {column} missing")
    s = pd.to_numeric(df[column], errors="coerce").dropna()
    if s.empty:
        return CheckResult(name=name, level=level, passed=False,
                           message="no numeric values")
    out = int(((s < low) | (s > high)).sum())
    return CheckResult(
        name=name, level=level, passed=(out == 0),
        message=f"{out}/{len(s)} out of [{low}, {high}]", value=out,
    )


def check_ohlc_consistency(
    df: pd.DataFrame,
    name: str = "ohlc_consistency",
    level: str = "CRITICAL",
) -> CheckResult:
    """high >= max(open, close) 且 low <= min(open, close)。

    这条最容易被忽略，但错了整个技术指标全废——腾讯接口列序是
    open/close/high/low，顺序映射错就会得到 high<low 的荒谬数据。
    """
    need = ["open", "high", "low", "close"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        return CheckResult(name=name, level=level, passed=False,
                           message=f"missing columns: {missing}")
    d = df.dropna(subset=need)
    bad = (
        (d["high"] < d[["open", "close"]].max(axis=1))
        | (d["low"] > d[["open", "close"]].min(axis=1))
        | (d["high"] < d["low"])
    ).sum()
    return CheckResult(
        name=name, level=level, passed=(bad == 0),
        message=f"{bad} invalid bars out of {len(d)}", value=int(bad),
    )


def check_no_future_leak(
    df: pd.DataFrame,
    date_column: str = "date",
    as_of: str | None = None,
    name: str = "no_future_leak",
    level: str = "CRITICAL",
) -> CheckResult:
    """确保数据里不含「未来」的日期。

    训练模型时混入未来数据（未来函数）是量化领域最常见的致命错误，
    这条断言把它变成管道级的硬约束。
    """
    if date_column not in df.columns:
        return CheckResult(name=name, level=level, passed=False,
                           message=f"column {date_column} missing")
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    dates = pd.to_datetime(df[date_column], errors="coerce")
    future = int((dates > pd.Timestamp(as_of)).sum())
    return CheckResult(
        name=name, level=level, passed=(future == 0),
        message=f"{future} rows after {as_of} (as_of={as_of})", value=future,
    )


# ---------------------------------------------------------------- 运行日志


RUN_LOG_SCHEMA = (
    "run_id", "task", "started_at", "finished_at", "status",
    "rows_in", "rows_out", "failures", "seconds", "detail",
)


class RunLogger:
    """双写运行日志：JSONL 文件（人看）+ DuckDB 表（SQL 查）。

    为什么两边都写：JSONL 方便出错时直接 tail 看；DuckDB 表方便
    在 BI 里做「近 30 天任务成功率」这类监控图。
    """

    def __init__(self, task: str) -> None:
        self.task = task
        self.run_id = uuid.uuid4().hex[:12]
        self.started_at = datetime.now()
        self._t0 = time.monotonic()
        self._finished = False
        self.detail: dict[str, Any] = {}

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        # 正常路径下 finish() 已被显式调用过，这里只兜住异常路径，
        # 避免同一次运行产生两条日志。
        if self._finished:
            return
        status = "success" if exc_type is None else "failed"
        if exc_type is not None:
            self.detail["error"] = f"{exc_type.__name__}: {self.last_error()}"
        self.finish(status)

    def last_error(self) -> str:
        import sys
        exc = sys.exc_info()[1]
        return str(exc)[:500] if exc else ""

    def note(self, **kwargs: Any) -> None:
        self.detail.update(kwargs)

    def finish(
        self,
        status: str,
        rows_in: int = 0,
        rows_out: int = 0,
        failures: int = 0,
    ) -> dict[str, Any]:
        self._finished = True
        record = {
            "run_id": self.run_id,
            "task": self.task,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "rows_in": rows_in,
            "rows_out": rows_out,
            "failures": failures,
            "seconds": round(time.monotonic() - self._t0, 3),
            "detail": json.dumps(self.detail, ensure_ascii=False),
        }

        settings.RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with settings.RUN_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info(
            "run %s [%s] %.2fs rows_in=%d rows_out=%d fail=%d",
            self.run_id, status, record["seconds"], rows_in, rows_out, failures,
        )
        return record


def persist_run_log(conn: Any, record: dict[str, Any]) -> None:
    """把运行记录写进 DuckDB，供 BI 做任务健康监控。"""
    conn.execute(
        """
        INSERT INTO etl_run_log
        (run_id, task, started_at, finished_at, status,
         rows_in, rows_out, failures, seconds, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record["run_id"], record["task"], record["started_at"],
            record["finished_at"], record["status"], record["rows_in"],
            record["rows_out"], record["failures"], record["seconds"],
            record["detail"],
        ],
    )
