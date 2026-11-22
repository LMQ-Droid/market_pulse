# -*- coding: utf-8 -*-
"""新浪行情数据源。

提供两类数据：
1. 全市场截面快照（`Market_Center.getHQNodeData`）——含 PE/PB/市值/换手率。
2. 行业分类树（`Market_Center.getHQNodes`）——49 个行业节点。

注意：该接口单页硬上限 100 条，传更大的 num 仍只返回 100，必须分页。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import settings
from src.extract.http_client import HttpClient

logger = logging.getLogger(__name__)

# 接口返回的字段是我们关心的全部列；显式列出便于下游对齐 schema。
SNAPSHOT_COLUMNS = [
    "symbol", "code", "name", "trade", "pricechange", "changepercent",
    "buy", "sell", "settlement", "open", "high", "low", "volume", "amount",
    "ticktime", "per", "pb", "mktcap", "nmc", "turnoverratio",
]


def _fetch_page(client: HttpClient, node: str, page: int, size: int) -> list[dict[str, Any]]:
    """取一页快照。空页返回空列表（这是翻页终止的正常信号）。"""
    resp = client.get(
        settings.SINA_PAGE_URL,
        params={
            "page": page,
            "num": size,
            "sort": "symbol",
            "asc": 1,
            "node": node,
        },
        headers={"Referer": settings.SINA_REFERER},
    )
    text = resp.text.strip()
    if not text or text == "null" or text == "[]":
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("page %d returned non-JSON (len=%d), treating as empty", page, len(text))
        return []
    return data if isinstance(data, list) else []


def fetch_snapshot(
    client: HttpClient,
    node: str = "hs_a",
    page_size: int = settings.SINA_PAGE_SIZE,
    max_pages: int = settings.SINA_MAX_PAGES,
    workers: int = settings.HTTP_MAX_WORKERS,
) -> list[dict[str, Any]]:
    """并发分页抓取全市场快照。

    先串行探明总页数（空页即止），再并发拉取剩余页。
    这样既不会因猜测页数而浪费请求，又能拿到并发收益。
    """
    # 第一页用于确认接口可用并探测总页数
    first = _fetch_page(client, node, 1, page_size)
    if not first:
        return []

    # 串行向后探测，直到空页。56 页时约 70s；改用并发探测会因
    # 无法提前预知边界而发出大量无效请求，反而更慢。
    pages: dict[int, list[dict[str, Any]]] = {1: first}
    page = 2
    while page <= max_pages:
        rows = _fetch_page(client, node, page, page_size)
        if not rows:
            break
        pages[page] = rows
        page += 1

    logger.info("sina snapshot: %d pages corroborated", len(pages))

    # 探测阶段已串行拉完，此处不再重复请求；保留 workers 参数供将来
    # 改为「预知总页数」策略时使用。
    _ = workers

    rows: list[dict[str, Any]] = []
    for p in sorted(pages):
        rows.extend(pages[p])
    return rows


def fetch_snapshot_parallel(
    client: HttpClient,
    node: str = "hs_a",
    page_size: int = settings.SINA_PAGE_SIZE,
    max_pages: int = settings.SINA_MAX_PAGES,
    workers: int = settings.HTTP_MAX_WORKERS,
) -> list[dict[str, Any]]:
    """并发抓取：先用一页确定总数，再并发拉取其余页。

    接口响应里不含总数，因此采用「探测 + 并发 + 溢出检测」策略：
    并发拉取到 max_pages，遇到空页即停，最后按页码排序拼接。
    """
    pages: dict[int, list[dict[str, Any]]] = {}

    def work(p: int) -> tuple[int, list[dict[str, Any]]]:
        return p, _fetch_page(client, node, p, page_size)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, p): p for p in range(1, max_pages + 1)}
        for fut in as_completed(futures):
            try:
                p, rows = fut.result()
                pages[p] = rows
            except Exception as exc:  # noqa: BLE001
                logger.warning("page %s failed: %s", futures[fut], exc)

    # 找到第一个空页作为真实边界，丢弃其后的所有结果
    empty_pages = sorted(p for p, rows in pages.items() if not rows)
    boundary = empty_pages[0] if empty_pages else max_pages + 1

    ordered: list[dict[str, Any]] = []
    for p in range(1, boundary):
        ordered.extend(pages.get(p, []))
    logger.info(
        "sina snapshot parallel: %d pages (boundary=%d), %d rows",
        boundary - 1, boundary, len(ordered),
    )
    return ordered


def fetch_industry_nodes(client: HttpClient) -> list[dict[str, str]]:
    """抓取行业分类树，返回 [{name, code, node}] 扁平列表。

    接口返回嵌套数组，行业叶子节点形如 ["玻璃行业", "", "new_blhy"]。
    用递归兜住结构变化，避免层级调整时直接崩掉。
    """
    resp = client.get(
        settings.SINA_NODES_URL,
        headers={"Referer": settings.SINA_REFERER},
    )
    tree = json.loads(resp.text)

    found: list[dict[str, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            # 形如 ["玻璃行业", "", "new_blhy"] 的三元组即一个行业
            if (
                len(x) == 3
                and all(isinstance(i, str) for i in x)
                and x[2].startswith("new_")
            ):
                found.append({"name": x[0], "code": x[1], "node": x[2]})
                return
            for item in x:
                walk(item)

    walk(tree)
    logger.info("sina industry nodes: %d", len(found))
    return found


def fetch_industry_members(
    client: HttpClient,
    node: str,
    page_size: int = settings.SINA_PAGE_SIZE,
    max_pages: int = 30,
) -> list[dict[str, Any]]:
    """抓取某行业的成分股列表。"""
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        batch = _fetch_page(client, node, page, page_size)
        if not batch:
            break
        rows.extend(batch)
    return rows


# ---------------------------------------------------------------- K 线

SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)

# 新浪 K 线的返回字段（顺序无关，都是具名键）
SINA_KLINE_FIELDS = ["day", "open", "high", "low", "close", "volume"]


def to_sina_symbol(code: str) -> str:
    """转成新浪的 sh/sz 前缀格式（新浪不提供北交所历史 K 线）。"""
    from src.extract.tencent import to_tencent_symbol
    return to_tencent_symbol(code)


def fetch_kline(
    client: HttpClient,
    code: str,
    limit: int = settings.KLINE_LIMIT,
    min_bars: int = 25,
) -> list[dict[str, Any]]:
    """从新浪抓取单只股票日 K 线。

    **重要口径差异**：新浪返回的是**不复权**价格（原始成交价）。
    腾讯给的是前复权价。两者不能混用：
    - 不复权价在除权除息日会出现跳空，直接算动量会得到假的暴跌。
    - 因此下游必须标记 `adjust_type='none'`，并知道这个局限。

    面试若被问「为什么用不复权数据」，诚实回答：
    免费公开源里稳定可用的复权数据很难长期拿到；本项目以
    「能持续跑通」优先，并在文档里明确标注该限制，
    而不是假装自己拿到了复权数据。
    """
    symbol = to_sina_symbol(code)
    resp = client.get(
        SINA_KLINE_URL,
        params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": limit},
        headers={"Referer": settings.SINA_REFERER},
    )

    if _looks_like_html(resp):
        raise ValueError(
            f"sina kline returned HTML for {code} (likely rate-limited); "
            f"head={resp.text[:80]!r}"
        )

    text = resp.text.strip()
    if not text or text == "null":
        return []

    try:
        bars = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("sina kline non-JSON for %s: %r", code, text[:80])
        return []

    if not isinstance(bars, list) or len(bars) < min_bars:
        logger.debug("%s: only %d bars (< %d), skipped",
                     code, len(bars) if isinstance(bars, list) else 0, min_bars)
        return []

    rows: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict) or "day" not in bar:
            continue
        rows.append({
            "date": bar["day"],
            "open": bar.get("open"),
            "close": bar.get("close"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "volume": bar.get("volume"),
            "code": symbol[2:],
            "symbol": symbol,
            "period": "day",
            "adjust_type": "none",   # 新浪为不复权数据
        })
    return rows


def _looks_like_html(resp: Any) -> bool:
    """识别 HTML 拦截页（封禁时常返回 200/501 + HTML 而非 JSON）。"""
    body = resp.text.lstrip()[:200]
    return body.startswith("<") or "btoa" in body


def fetch_kline_batch(
    client: HttpClient,
    codes: list[str],
    limit: int = settings.KLINE_LIMIT,
    min_bars: int = 25,
    workers: int = settings.HTTP_MAX_WORKERS,
    on_progress: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """并发抓取多只股票 K 线，单只失败不中断整体。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    done = 0
    total = len(codes)

    def work(code: str) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            rows = fetch_kline(client, code, limit=limit, min_bars=min_bars)
            return code, rows, None
        except Exception as exc:  # noqa: BLE001
            return code, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, c): c for c in codes}
        for fut in as_completed(futures):
            code, rows, err = fut.result()
            done += 1
            if err:
                failures.append({"code": code, "error": err[:200]})
            all_rows.extend(rows)
            if on_progress and (done % 100 == 0 or done == total):
                on_progress(done, total, len(all_rows), len(failures))

    return all_rows, failures
