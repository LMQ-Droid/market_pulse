# -*- coding: utf-8 -*-
"""数据源可用性探测。

为什么需要这个模块：数据源封禁是数据工程的日常，不是意外。
实测记录（本项目开发期间真实发生）：

    2026-11-22 上午   东财 push2 被限流（连接被重置）
    2026-11-22 中午   腾讯 web.ifzq 开始返回 501 HTML 拦截页
    2026-11-22 下午   腾讯 ifzq / web3.ifzq 也一并 501
    全程            新浪 K 线与快照接口稳定

结论：单源脚本活不过一天。必须有探测 + 自动切换，
并让「当前用哪个源」成为运行日志里可见的事实，而不是隐性故障。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from config import settings
from src.extract.http_client import HttpClient

logger = logging.getLogger(__name__)


@dataclass
class SourceHealth:
    name: str
    available: bool
    latency_ms: float
    detail: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _looks_blocked(resp) -> bool:
    """识别「HTTP 200/501 但返回 HTML 拦截页」这种伪成功。

    很多数据源不对封禁返回 429，而是回一段带 JS 跳转的 HTML。
    如果只看 status_code，会得到「请求成功」的错误结论。
    """
    body = resp.text.lstrip()[:200]
    return body.startswith("<") or "btoa" in body or "location.href" in body


def check_sina_kline(client: HttpClient, symbol: str = "sh600519") -> SourceHealth:
    """探测新浪 K 线接口。"""
    t0 = datetime.now()
    try:
        resp = client.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData",
            params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": 5},
            headers={"Referer": settings.SINA_REFERER},
        )
        latency = (datetime.now() - t0).total_seconds() * 1000
        if _looks_blocked(resp):
            return SourceHealth("sina_kline", False, latency, "blocked: HTML response")
        data = resp.json()
        ok = isinstance(data, list) and len(data) > 0
        return SourceHealth("sina_kline", ok, latency,
                            f"bars={len(data) if isinstance(data, list) else 0}")
    except Exception as exc:  # noqa: BLE001
        latency = (datetime.now() - t0).total_seconds() * 1000
        return SourceHealth("sina_kline", False, latency, f"{type(exc).__name__}: {exc}"[:150])


def check_tencent_kline(client: HttpClient, symbol: str = "sh600519") -> SourceHealth:
    """探测腾讯 K 线接口。"""
    t0 = datetime.now()
    try:
        resp = client.get(
            settings.TENCENT_KLINE_URL,
            params={"param": f"{symbol},day,,,5,qfq"},
            headers={"Referer": "https://gu.qq.com/"},
        )
        latency = (datetime.now() - t0).total_seconds() * 1000
        if _looks_blocked(resp):
            return SourceHealth("tencent_kline", False, latency, "blocked: HTML response")
        payload = resp.json()
        node = (payload.get("data") or {}).get(symbol) or {}
        bars = node.get("qfqday") or node.get("day") or []
        return SourceHealth("tencent_kline", len(bars) > 0, latency, f"bars={len(bars)}")
    except Exception as exc:  # noqa: BLE001
        latency = (datetime.now() - t0).total_seconds() * 1000
        return SourceHealth("tencent_kline", False, latency, f"{type(exc).__name__}: {exc}"[:150])


def probe_all(client: HttpClient) -> list[SourceHealth]:
    """并发探测所有源，返回健康度列表。"""
    results: list[SourceHealth] = []
    lock = threading.Lock()

    def run(fn) -> None:
        h = fn(client)
        with lock:
            results.append(h)
        logger.info(
            "source %-16s available=%-5s %6.0fms %s",
            h.name, h.available, h.latency_ms, h.detail,
        )

    threads = [
        threading.Thread(target=run, args=(check_sina_kline,)),
        threading.Thread(target=run, args=(check_tencent_kline,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results


def pick_kline_source(client: HttpClient) -> str:
    """选择可用的 K 线源，优先新浪（实测更稳、且能取 800 根）。

    返回 "sina" 或 "tencent"；两者都不可用时抛异常，
    让管道明确失败，而不是写出一堆空数据假装成功。
    """
    health = {h.name: h for h in probe_all(client)}

    sina_ok = health.get("sina_kline", SourceHealth("sina_kline", False, 0)).available
    tencent_ok = health.get("tencent_kline", SourceHealth("tencent_kline", False, 0)).available

    if sina_ok:
        logger.info("kline source selected: sina (primary)")
        return "sina"
    if tencent_ok:
        logger.warning("sina unavailable, falling back to tencent")
        return "tencent"

    detail = "; ".join(f"{h.name}={h.detail}" for h in health.values())
    raise RuntimeError(f"all kline sources unavailable: {detail}")
