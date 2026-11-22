# -*- coding: utf-8 -*-
"""AI 模块：因子预测未来收益，重点是方法论而非模型复杂度。

核心原则（面试时这就是你的差异化）：

1. **严格时序切分**。绝不用 random_split——那会让「未来」参与训练，
   得到一个漂亮但完全虚假的准确率。时间序列必须按时间点切。
2. **必须打败基线**。全买（市场平均）和随机选是两个下限。
   跑不赢基线就要如实说跑不赢。
3. **报告不确定性**。单一数字没有意义，要给多次切分的均值±标准差。
4. **敢说无效**。A股短期收益接近随机游走，弱预测力是大概率结果，
   而「证明它无效」本身是有价值的结论。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

# 特征列：全部是「当日及之前」可知的因子。
# future_return 是标签，绝不能出现在这里。
FEATURE_COLS = [
    "ret_1d", "vol_20d", "ma20_dev", "ma60_dev", "dd_20d", "volume_ratio",
    "mom_5d", "mom_20d", "mom_60d", "mom_120d",
]

LABEL_COL = "future_return"


@dataclass
class FoldResult:
    """单个时间切分的评估结果。"""

    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_rows: int
    test_rows: int
    rmse_model: float
    rmse_zero: float          # 基线：一律预测 0（等价于「市场平均」）
    ic: float                 # 信息系数：预测值与真实值的秩相关
    top_decile_return: float  # 按预测值取前 10% 的平均真实收益
    bottom_decile_return: float
    long_short: float         # 多空组合收益


@dataclass
class ExperimentReport:
    model_name: str
    folds: list[FoldResult] = field(default_factory=list)

    @property
    def mean_ic(self) -> float:
        return float(np.mean([f.ic for f in self.folds])) if self.folds else float("nan")

    @property
    def std_ic(self) -> float:
        return float(np.std([f.ic for f in self.folds])) if self.folds else float("nan")

    @property
    def mean_long_short(self) -> float:
        return float(np.mean([f.long_short for f in self.folds])) if self.folds else float("nan")

    @property
    def beats_baseline(self) -> bool:
        """RMSE 是否优于「全预测 0」的基线。"""
        if not self.folds:
            return False
        return np.mean([f.rmse_model for f in self.folds]) < np.mean(
            [f.rmse_zero for f in self.folds]
        )

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([f.__dict__ for f in self.folds])


def make_folds(
    df: pd.DataFrame,
    n_folds: int = 4,
    date_col: str = "date",
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """按时间顺序切成 n_folds 个「训练在前、测试在后」的段。

    做法：把时间轴分成 n_folds+1 段，第 i 折用前 i 段训练、第 i+1 段测试。
    这是 expanding window（扩张窗口），最贴近真实交易场景——
    你只能用已经发生的历史去预测未来。
    """
    d = df.copy()
    d[date_col] = pd.to_datetime(d[date_col])
    unique_dates = np.sort(d[date_col].unique())

    if len(unique_dates) < n_folds + 1:
        return []

    # 边界：保证每段至少有一天
    bounds = np.linspace(0, len(unique_dates), n_folds + 2).astype(int)

    folds = []
    for i in range(1, n_folds + 1):
        train_dates = unique_dates[: bounds[i]]
        test_dates = unique_dates[bounds[i]: bounds[i + 1]]
        if len(test_dates) == 0:
            continue
        train = d[d[date_col].isin(train_dates)]
        test = d[d[date_col].isin(test_dates)]
        if len(train) and len(test):
            folds.append((train, test))

    logger.info("built %d time-ordered folds", len(folds))
    return folds


def _information_coefficient(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """IC = 预测值与真实值的 Spearman 秩相关。

    为什么用秩相关而非 Pearson：收益分布有厚尾，极端值会主导 Pearson，
    而实际交易只关心「排序对不对」，秩相关更贴近用法。
    """
    if len(y_true) < 3:
        return float("nan")
    s_true = pd.Series(y_true).rank()
    s_pred = pd.Series(y_pred).rank()
    if s_true.std() == 0 or s_pred.std() == 0:
        return 0.0
    return float(np.corrcoef(s_true, s_pred)[0, 1])


def _evaluate_fold(
    fold_idx: int,
    train: pd.DataFrame,
    test: pd.DataFrame,
    model: Pipeline | GradientBoostingRegressor,
    feature_cols: list[str],
    max_train_rows: int | None = None,
) -> FoldResult:
    """在单个时间切分上训练并评估。

    `max_train_rows` 用于控制训练成本：60 万行以上时梯度提升树的
    训练时间呈线性增长，而因子研究里 15 万行的统计量已经足够稳定。
    抽样时按时间**等距**抽取而非随机，保持时间分布不变。
    """
    tr = train.dropna(subset=feature_cols + [LABEL_COL])
    te = test.dropna(subset=feature_cols + [LABEL_COL])

    if tr.empty or te.empty:
        raise ValueError(f"fold {fold_idx}: empty train/test after dropna")

    if max_train_rows and len(tr) > max_train_rows:
        # 等距抽样：先按时间排序，再取等间隔的行，保持时间分布
        tr = tr.sort_values("date")
        step = max(1, len(tr) // max_train_rows)
        tr = tr.iloc[::step].head(max_train_rows)

    X_tr, y_tr = tr[feature_cols].to_numpy(), tr[LABEL_COL].to_numpy()
    X_te, y_te = te[feature_cols].to_numpy(), te[LABEL_COL].to_numpy()

    model.fit(X_tr, y_tr)
    pred = model.predict(X_te)

    rmse_model = float(np.sqrt(mean_squared_error(y_te, pred)))
    rmse_zero = float(np.sqrt(mean_squared_error(y_te, np.zeros_like(y_te))))

    ic = _information_coefficient(y_te, pred)

    # 分组检验：按预测值排序，看头尾 10% 的真实收益差
    n = len(pred)
    k = max(1, n // 10)
    order = np.argsort(pred)
    bottom_ret = float(np.mean(y_te[order[:k]]))
    top_ret = float(np.mean(y_te[order[-k:]]))

    return FoldResult(
        fold=fold_idx,
        train_start=str(pd.to_datetime(tr["date"]).min().date()),
        train_end=str(pd.to_datetime(tr["date"]).max().date()),
        test_start=str(pd.to_datetime(te["date"]).min().date()),
        test_end=str(pd.to_datetime(te["date"]).max().date()),
        train_rows=len(tr),
        test_rows=len(te),
        rmse_model=rmse_model,
        rmse_zero=rmse_zero,
        ic=ic,
        top_decile_return=top_ret,
        bottom_decile_return=bottom_ret,
        long_short=top_ret - bottom_ret,
    )


def run_experiment(
    factors: pd.DataFrame,
    model_name: str = "ridge",
    n_folds: int = 4,
    feature_cols: list[str] | None = None,
    max_train_rows: int | None = 150_000,
) -> ExperimentReport:
    """跑完整的时序交叉验证实验。

    Args:
        factors: 含特征列与 future_return 的因子表
        model_name: ridge（线性基线）或 gbr（非线性）
        n_folds: 时间切分段数
        max_train_rows: 每折训练样本上限（控制 GBR 训练耗时）
    """
    feature_cols = feature_cols or [c for c in FEATURE_COLS if c in factors.columns]
    logger.info("experiment=%s features=%d", model_name, len(feature_cols))

    if model_name == "ridge":
        # 标准化是必须的：动量和波动率量纲差一个数量级，
        # 不标准化时 L2 惩罚会不公平地压制小量纲特征。
        model: Pipeline | GradientBoostingRegressor = Pipeline([
            ("scaler", StandardScaler()),
            ("reg", Ridge(alpha=1.0)),
        ])
    elif model_name == "gbr":
        model = GradientBoostingRegressor(
            n_estimators=120, max_depth=3, learning_rate=0.05,
            subsample=0.8, random_state=42,
        )
    else:
        raise ValueError(f"unknown model: {model_name}")

    report = ExperimentReport(model_name=model_name)
    folds = make_folds(factors, n_folds=n_folds)

    if not folds:
        logger.warning("not enough dates for %d folds", n_folds)
        return report

    for i, (train, test) in enumerate(folds, start=1):
        try:
            res = _evaluate_fold(
                i, train, test, model, feature_cols,
                max_train_rows=max_train_rows,
            )
            report.folds.append(res)
            logger.info(
                "fold %d: train=%s~%s test=%s~%s IC=%.4f RMSE=%.5f (base %.5f)",
                i, res.train_start, res.train_end, res.test_start, res.test_end,
                res.ic, res.rmse_model, res.rmse_zero,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fold %d failed: %s", i, exc)

    return report


def feature_importance(
    factors: pd.DataFrame,
    feature_cols: list[str] | None = None,
    max_rows: int = 150_000,
) -> pd.DataFrame:
    """用树模型看特征重要性。

    注意：重要性高不等于「能赚钱」，只说明模型在训练集上依赖它。
    要结合 IC 和分组收益一起看，这也是面试常考的辨析点。
    """
    feature_cols = feature_cols or [c for c in FEATURE_COLS if c in factors.columns]
    d = factors.dropna(subset=feature_cols + [LABEL_COL])
    if d.empty:
        return pd.DataFrame(columns=["feature", "importance"])

    if len(d) > max_rows:
        d = d.sort_values("date").iloc[:: max(1, len(d) // max_rows)].head(max_rows)

    model = GradientBoostingRegressor(
        n_estimators=120, max_depth=3, learning_rate=0.05,
        subsample=0.8, random_state=42,
    )
    model.fit(d[feature_cols].to_numpy(), d[LABEL_COL].to_numpy())

    imp = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    total = imp["importance"].sum()
    imp["importance_pct"] = (imp["importance"] / total * 100).round(2)
    return imp.reset_index(drop=True)


def verdict(report: ExperimentReport) -> str:
    """把实验结果翻译成一句人话结论。

    这段文字是给简历和面试用的：明确、可证伪、不吹牛。
    """
    if not report.folds:
        return "样本不足，未能完成时间序列验证。"

    ic, std = report.mean_ic, report.std_ic
    ls = report.mean_long_short

    if abs(ic) < 0.02:
        strength = "几乎无预测力（|IC| < 0.02，接近随机）"
    elif abs(ic) < 0.05:
        strength = "有微弱预测力（0.02 ≤ |IC| < 0.05）"
    elif abs(ic) < 0.10:
        strength = f"有中等预测力（|IC| = {abs(ic):.3f}）"
    else:
        strength = f"预测力较强（|IC| = {abs(ic):.3f}），需警惕数据泄露"

    parts = [
        f"模型 `{report.model_name}` 在 {len(report.folds)} 段时序切分上平均 IC = {ic:.4f}"
        f"（±{std:.4f}），{strength}。",
        f"按预测值多空分组，头尾 10% 组收益差 {ls*100:+.2f}%（5 日持有）。",
    ]

    if report.beats_baseline:
        parts.append("RMSE 优于「一律预测 0」的基线。")
    else:
        parts.append(
            "⚠️ RMSE **未能优于**「一律预测 0」的基线——"
            "这说明当前因子组合不足以稳定预测未来 5 日收益，"
            "诚实记录该负面结果。"
        )

    if abs(ic) > 0.10:
        parts.append(
            "IC 偏高，建议复核是否存在未来函数（如特征里混入了当日收盘后信息）。"
        )

    return "".join(parts)
