# -*- coding: utf-8 -*-
"""每日自动更新：采集 → 导出 → 提交 → 推送。

这是把看板从"静态快照"变成"实时行情工具"的关键脚本。
Windows 任务计划每天调用它，全程无人值守。

设计要点（都是踩过坑才加的）：

1. **交易日判断**：周末和节假日不跑，省得白抓一次还把日志刷满。
   判断依据是数据源返回的最新交易日，不是系统日期。

2. **失败不推送**：采集中途失败时保留旧数据、不发 commit。
   宁可数据旧一天，也不要把半截数据推上去——看板崩了比数据旧严重。

3. **提交信息带数据日期**：git log 一眼能看出哪天的数据成功了，
   不需要翻日志。

4. **并发锁**：防止定时任务和手动运行撞车。
   用文件锁而非进程检查，跨会话也有效。

5. **全量日志**：每次运行写一行 JSON 到 logs/daily_update.jsonl，
   失败时能定位到具体哪一步。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOCK_FILE = ROOT / ".update.lock"
RUN_LOG = LOG_DIR / "daily_update.jsonl"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "daily_update.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("daily")


# ---------------------------------------------------------------- 锁


def acquire_lock() -> bool:
    """用创建独占文件的方式加锁（Windows 下 msvcrt 更标准，但文件存在判断足够）。

    返回 True 表示拿到锁。锁文件超过 2 小时视为残留（异常退出没清）。
    """
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 7200:
            logger.warning("已有更新任务在跑（锁文件 %.0f 秒前创建）", age)
            return False
        logger.warning("锁文件已存在 %.0f 秒，视为残留，清除后继续", age)
        LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- git


def git(*args: str) -> tuple[int, str]:
    """执行 git 命令，返回 (退出码, 输出)。"""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "git 命令超时"
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def has_changes() -> bool:
    """只检查 data/deploy 有没有变化，避免无关文件触发提交。"""
    code, out = git("status", "--porcelain", "data/deploy")
    return code == 0 and bool(out.strip())


# ---------------------------------------------------------------- 步骤


def run_pipeline() -> tuple[bool, str]:
    """跑 ETL。返回 (是否成功, 最新交易日)。"""
    logger.info("=" * 60)
    logger.info("步骤 1/4：采集与建模")
    logger.info("=" * 60)

    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "run_all.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.time() - t0

    if p.returncode != 0:
        logger.error("ETL 失败（%.1fs），退出码 %d", elapsed, p.returncode)
        tail = "\n".join((p.stdout or "").splitlines()[-15:])
        logger.error("输出尾部:\n%s", tail)
        return False, ""

    logger.info("ETL 完成，耗时 %.1fs", elapsed)

    # 从数据库读最新交易日，作为提交信息的依据
    try:
        import duckdb

        conn = duckdb.connect(str(ROOT / "data" / "market.duckdb"), read_only=True)
        row = conn.execute("SELECT MAX(date) FROM mart_industry_daily").fetchone()
        conn.close()
        latest = str(row[0])[:10] if row and row[0] else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取最新交易日失败: %s", exc)
        latest = ""

    logger.info("最新交易日：%s", latest or "未知")
    return True, latest


def run_export() -> bool:
    """导出精简 parquet 包。"""
    logger.info("=" * 60)
    logger.info("步骤 2/4：导出部署包")
    logger.info("=" * 60)

    p = subprocess.run(
        [sys.executable, "export_for_deploy.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        logger.error("导出失败（退出码 %d）:\n%s", p.returncode,
                     (p.stdout or "")[-800:])
        return False

    for line in (p.stdout or "").splitlines():
        if "TOTAL" in line or "OK:" in line:
            logger.info("  %s", line.strip())
    return True


def commit_and_push(latest: str) -> bool:
    """提交并推送。只有数据真的变了才提交。"""
    logger.info("=" * 60)
    logger.info("步骤 3/4：提交变更")
    logger.info("=" * 60)

    if not has_changes():
        logger.info("data/deploy 无变化，跳过提交")
        return True

    code, out = git("add", "data/deploy")
    if code != 0:
        logger.error("git add 失败：%s", out[:300])
        return False

    msg = f"data: 行情更新至 {latest}" if latest else "data: 行情数据更新"
    code, out = git(
        "-c", "user.name=market_pulse-bot",
        "-c", "user.email=bot@localhost",
        "commit", "-m", msg,
    )
    if code != 0:
        logger.error("git commit 失败：%s", out[:300])
        return False
    logger.info("已提交：%s", msg)

    logger.info("=" * 60)
    logger.info("步骤 4/4：推送到 GitHub")
    logger.info("=" * 60)

    # 重试 3 次——国内网络推送经常偶发超时
    for attempt in range(1, 4):
        code, out = git("push", "origin", "main")
        if code == 0:
            logger.info("推送成功")
            return True
        logger.warning("推送第 %d 次失败：%s", attempt, out.strip()[:200])
        if attempt < 3:
            time.sleep(5 * attempt)

    logger.error("推送三次均失败，数据已提交但未上云")
    return False


def write_run_log(success: bool, latest: str, detail: str = "") -> None:
    """写一行运行记录。"""
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "trade_date": latest,
        "status": "ok" if success else "failed",
        "detail": detail,
    }
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    started = time.time()
    logger.info("=" * 60)
    logger.info("market_pulse 每日更新  开始于 %s",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    # 周末不跑（A 股不开市）。这里只是快速跳过，真正的交易日
    # 判断交给数据源——节假日系统不知道，但抓完发现没新数据也不会推。
    if date.today().weekday() >= 5:
        logger.info("今天是周末（%s），跳过", date.today().strftime("%A"))
        write_run_log(True, "", "weekend skip")
        return 0

    if not acquire_lock():
        write_run_log(False, "", "locked")
        return 1

    try:
        ok, latest = run_pipeline()
        if not ok:
            write_run_log(False, "", "pipeline failed")
            logger.error("采集失败，保留旧数据，不推送")
            return 2

        if not run_export():
            write_run_log(False, latest, "export failed")
            return 3

        if not commit_and_push(latest):
            write_run_log(False, latest, "push failed")
            return 4

        elapsed = time.time() - started
        logger.info("=" * 60)
        logger.info("全部完成，耗时 %.1fs", elapsed)
        logger.info("看板：https://market-pulse-lmq.streamlit.app")
        logger.info("=" * 60)

        write_run_log(True, latest, f"{elapsed:.0f}s")
        return 0

    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
