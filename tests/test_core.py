# -*- coding: utf-8 -*-
"""核心逻辑的单元测试。

跑法：python -m pytest tests/ -v
或（无 pytest 时）：python tests/test_core.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features import factors as feat        # noqa: E402
from src.features import ml_experiment as mle   # noqa: E402
from src.quality import checks                  # noqa: E402
from src.transform import clean                 # noqa: E402


# ---------------------------------------------------------------- 清洗层


def test_clean_snapshot_drops_invalid():
    rows = [
        {"symbol": "sh600519", "code": "600519", "name": "贵州茅台",
         "trade": "1500", "changepercent": "1.5", "per": "30", "pb": "8",
         "mktcap": "200000000", "nmc": "200000000"},
        {"symbol": "sh000000", "code": "000000", "name": "无效",
         "trade": "0", "changepercent": "0"},
    ]
    df = clean.clean_snapshot(rows, "2024-01-02")
    assert len(df) == 1, f"expected 1 valid row, got {len(df)}"
    assert df.iloc[0]["code"] == "600519"


def test_clean_snapshot_mktcap_unit():
    """市值从万元转亿元：2 亿万元 = 20000 亿元。"""
    rows = [{"symbol": "sh600519", "code": "600519", "name": "x",
             "trade": "10", "mktcap": "20000", "nmc": "20000"}]
    df = clean.clean_snapshot(rows, "2024-01-02")
    assert abs(df.iloc[0]["mktcap"] - 2.0) < 1e-9


def test_clean_kline_rejects_bad_ohlc():
    """high < low 的荒谬数据必须被丢掉。"""
    rows = [
        {"code": "600519", "symbol": "sh600519", "date": "2024-01-02",
         "open": 10, "close": 11, "high": 12, "low": 9, "volume": 100,
         "period": "day", "adjust_type": "qfq"},  # 正常
        {"code": "600519", "symbol": "sh600519", "date": "2024-01-03",
         "open": 10, "close": 11, "high": 8, "low": 9, "volume": 100,
         "period": "day", "adjust_type": "qfq"},  # high < low，应丢
    ]
    df = clean.clean_kline(rows, "2024-01-03")
    assert len(df) == 1, f"bad bar not dropped, got {len(df)}"


def test_clean_kline_dedup():
    rows = [
        {"code": "600519", "symbol": "sh600519", "date": "2024-01-02",
         "open": 10, "close": 11, "high": 12, "low": 9, "volume": 100,
         "period": "day", "adjust_type": "qfq"},
    ] * 2
    df = clean.clean_kline(rows, "2024-01-02")
    assert len(df) == 1, "duplicate rows not deduped"


# ---------------------------------------------------------------- 质量断言


def test_check_unique_key_detects_dupes():
    df = pd.DataFrame({"code": ["A", "A"], "date": ["2024-01-01", "2024-01-01"]})
    r = checks.check_unique_key(df, ["code", "date"])
    assert not r.passed
    assert r.value == 1


def test_check_no_future_leak():
    """含未来日期的数据必须被识别出来。"""
    df = pd.DataFrame({"date": ["2020-01-01", "2099-01-01"]})
    r = checks.check_no_future_leak(df, as_of="2024-01-01")
    assert not r.passed
    assert r.value == 1


def test_check_ohlc_consistency():
    good = pd.DataFrame({
        "open": [10], "high": [12], "low": [9], "close": [11],
    })
    assert checks.check_ohlc_consistency(good).passed

    bad = pd.DataFrame({
        "open": [10], "high": [8], "low": [9], "close": [11],
    })
    assert not checks.check_ohlc_consistency(bad).passed


# ---------------------------------------------------------------- 因子


def _make_kline(n: int = 200) -> pd.DataFrame:
    """构造单只股票的合成 K 线，用于验证因子逻辑。"""
    dates = pd.bdate_range("2023-01-02", periods=n)
    close = 10 * np.cumprod(1 + np.random.default_rng(42).normal(0, 0.01, n))
    return pd.DataFrame({
        "code": "600519",
        "date": dates,
        "close": close,
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "volume": np.full(n, 1_000_000),
    })


def test_factors_no_future_leak_in_momentum():
    """动量必须是「向后看」，即用 t 和 t-n，不能用 t+n。"""
    df = _make_kline(120)
    out = feat.compute_stock_factors(df)

    # 前几个值应该是 NaN（没有足够历史）
    assert out["mom_20d"].iloc[:20].isna().all(), "momentum leaked future data"

    # 手算校验第 30 行的 mom_20d
    expected = df["close"].iloc[30] / df["close"].iloc[10] - 1
    assert abs(out["mom_20d"].iloc[30] - expected) < 1e-9


def test_future_return_is_forward_looking():
    """future_return 是标签，必须是「向前看」，且最后一行为 NaN。"""
    df = _make_kline(60)
    out = feat.compute_stock_factors(df)

    assert pd.isna(out["future_return"].iloc[-1]), "last row future_return should be NaN"

    expected = df["close"].iloc[5] / df["close"].iloc[0] - 1
    assert abs(out["future_return"].iloc[0] - expected) < 1e-9


def test_volatility_positive():
    df = _make_kline(100)
    out = feat.compute_stock_factors(df)
    vol = out["vol_20d"].dropna()
    assert (vol >= 0).all(), "volatility must be non-negative"


# ---------------------------------------------------------------- ML 时序切分


def _make_panel(n_dates: int = 60, n_stocks: int = 20) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    for d in pd.bdate_range("2023-01-02", periods=n_dates):
        for s in range(n_stocks):
            rows.append({
                "code": f"{600000 + s:06d}",
                "date": d,
                "ret_1d": rng.normal(0, 0.01),
                "vol_20d": abs(rng.normal(0.2, 0.05)),
                "ma20_dev": rng.normal(0, 0.02),
                "ma60_dev": rng.normal(0, 0.03),
                "dd_20d": -abs(rng.normal(0.05, 0.02)),
                "volume_ratio": abs(rng.normal(1, 0.2)),
                "mom_5d": rng.normal(0, 0.02),
                "mom_20d": rng.normal(0, 0.04),
                "mom_60d": rng.normal(0, 0.08),
                "mom_120d": rng.normal(0, 0.12),
                "future_return": rng.normal(0, 0.03),
            })
    return pd.DataFrame(rows)


def test_folds_are_time_ordered():
    """最关键的测试：训练集的时间必须整体早于测试集。"""
    panel = _make_panel()
    folds = mle.make_folds(panel, n_folds=3)
    assert len(folds) == 3

    for i, (train, test) in enumerate(folds, 1):
        train_max = pd.to_datetime(train["date"]).max()
        test_min = pd.to_datetime(test["date"]).min()
        assert train_max < test_min, (
            f"fold {i}: train max {train_max} >= test min {test_min} "
            "=> FUTURE LEAKAGE"
        )


def test_folds_expanding_window():
    """扩张窗口：后一折的训练集必须包含前一折的训练集。"""
    panel = _make_panel()
    folds = mle.make_folds(panel, n_folds=3)

    for (tr_prev, _), (tr_next, _) in zip(folds, folds[1:]):
        prev_dates = set(pd.to_datetime(tr_prev["date"]).unique())
        next_dates = set(pd.to_datetime(tr_next["date"]).unique())
        assert prev_dates.issubset(next_dates), "window is not expanding"


def test_experiment_runs_and_reports():
    panel = _make_panel()
    report = mle.run_experiment(panel, model_name="ridge", n_folds=3)
    assert len(report.folds) == 3
    assert not np.isnan(report.mean_ic)
    assert isinstance(mle.verdict(report), str)
    assert len(mle.verdict(report)) > 20


def test_baseline_comparison_is_honest():
    """在纯噪声数据上，模型不应显著优于基线。

    这条测试防「自我欺骗」：如果随机标签上模型还能大幅跑赢基线，
    说明评估代码有 bug（典型是数据泄漏）。
    """
    panel = _make_panel()
    report = mle.run_experiment(panel, model_name="ridge", n_folds=3)
    # 纯噪声下 IC 应接近 0
    assert abs(report.mean_ic) < 0.15, (
        f"IC={report.mean_ic:.3f} on pure noise is suspiciously high "
        "=> possible leakage in evaluation"
    )


# ---------------------------------------------------------------- runner


def main() -> int:
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    failures: list[str] = []

    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {name}: {exc}")
            failures.append(name)
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    if failures:
        print("failed:", ", ".join(failures))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
