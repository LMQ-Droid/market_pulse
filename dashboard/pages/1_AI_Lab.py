# -*- coding: utf-8 -*-
"""AI 实验页：因子有效性检验。

这一页存在的意义不是「展示模型多准」，而是展示**方法论严谨**：
- 时序切分而非随机切分
- 必打败基线
- 报告多折均值±标准差而非单点数字
- 敢给负面结论
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import data_access as da  # noqa: E402
from src.features import ml_experiment as mle  # noqa: E402


@st.cache_data(ttl=600)
def load_factors() -> pd.DataFrame:
    try:
        return da.query("SELECT * FROM mart_stock_daily")
    except Exception:
        return pd.DataFrame()


st.title("因子有效性检验")
st.caption(
    "用因子预测未来 5 日收益。**重点在验证方法，不在模型精度**——"
    "随机切分会得到虚假的高准确率，这里用严格的时序切分。"
)

factors = load_factors()

if factors.empty or "future_return" not in factors.columns:
    st.error("无因子数据。请先运行 `python -m src.pipeline.run --task build`")
    st.stop()

# 未跑实验时给出明确指引——否则页面看起来是空的，
# 第一次来的人不知道要往下滚才找得到按钮。
if "report" not in st.session_state:
    st.info(
        "👇 **往下滚动，点击「运行实验」按钮**，约 5–20 秒后会出现 "
        "**IC（信息系数）** 等指标。\n\n"
        "IC 用来衡量因子预测能力的强弱：0 表示跟瞎猜差不多，"
        "1 表示完全预测准确。"
    )

# ---------------------------------------------------------------- 方法说明

with st.expander("📐 实验设计", expanded=True):
    st.markdown("""
**数据切分：扩张窗口时序验证**

```
|----训练----|--测试--|          fold 1
|------训练------|--测试--|      fold 2
|--------训练--------|--测试--|  fold 3
|----------训练----------|--测试--|  fold 4
```

时间永远是单向的：只用过去训练，预测未来。`sklearn.model_selection.KFold(shuffle=True)`
这种随机切分会把未来信息泄漏进训练集，得到漂亮但无意义的指标——这是量化研究里最常见的错误。

**评估指标**

| 指标 | 含义 | 判读 |
|---|---|---|
| **IC** | 预测值与真实收益的 Spearman 秩相关 | \\|IC\\| < 0.02 基本等于随机 |
| **RMSE vs 基线** | 对比「一律预测 0」 | 打不过基线说明模型无用 |
| **头尾 10% 收益差** | 多空组合收益 | 只关心排序能力，不关心绝对精度 |

**为什么用秩相关而不是 Pearson**：收益分布厚尾，极端值主导 Pearson；
实际研究只关心排序是否正确，所以秩相关更贴近用法。
""")

st.divider()

# ---------------------------------------------------------------- 参数

c1, c2, c3 = st.columns(3)
model_name = c1.selectbox(
    "模型", ["ridge", "gbr"],
    format_func=lambda m: {"ridge": "Ridge 回归（线性基线）",
                           "gbr": "梯度提升树（非线性）"}[m],
)
n_folds = c2.slider("时序切分段数", 2, 6, 4,
                    help="折数越多，每折训练集越大但测试集越小")
run_btn = c3.button("运行实验", type="primary")

avail_features = [c for c in mle.FEATURE_COLS if c in factors.columns]
st.caption(f"可用特征 {len(avail_features)} 个：{', '.join(avail_features)}")

# ---------------------------------------------------------------- 执行

if run_btn:
    with st.spinner("正在按时间顺序切分并训练..."):
        report = mle.run_experiment(
            factors, model_name=model_name, n_folds=n_folds
        )
    st.session_state["report"] = report
    st.session_state["importance"] = mle.feature_importance(factors)

report = st.session_state.get("report")

if report and report.folds:
    st.divider()
    st.subheader("结果")

    # 核心指标卡片
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("平均 IC", f"{report.mean_ic:.4f}", delta=f"±{report.std_ic:.4f}")
    m2.metric(
        "多空收益（5日）",
        f"{report.mean_long_short*100:+.2f}%",
        delta="头10% - 尾10%",
        delta_color="off",
    )
    avg_rmse = sum(f.rmse_model for f in report.folds) / len(report.folds)
    avg_base = sum(f.rmse_zero for f in report.folds) / len(report.folds)
    m3.metric(
        "RMSE vs 基线",
        f"{avg_rmse:.5f}",
        delta=f"基线 {avg_base:.5f}",
        delta_color="inverse" if report.beats_baseline else "off",
    )
    m4.metric(
        "是否打败基线",
        "✅ 是" if report.beats_baseline else "❌ 否",
    )

    # 结论
    v = mle.verdict(report)
    if report.beats_baseline and abs(report.mean_ic) >= 0.02:
        st.success(v)
    else:
        st.warning(v)
    st.caption(
        "结论以可复现的形式记录，包括负面结果——"
        "「已验证无效」与「报告高准确率」相比，前者可被他人独立检验。"
    )

    st.divider()

    # 分折明细
    st.subheader("分折明细")
    fold_df = report.to_frame()
    show = fold_df[[
        "fold", "train_start", "train_end", "test_start", "test_end",
        "train_rows", "test_rows", "ic", "rmse_model", "rmse_zero",
        "long_short",
    ]].copy()
    show["long_short"] = (show["long_short"] * 100).round(2)
    show = show.rename(columns={
        "fold": "折", "train_start": "训练起", "train_end": "训练止",
        "test_start": "测试起", "test_end": "测试止",
        "train_rows": "训练样本", "test_rows": "测试样本",
        "ic": "IC", "rmse_model": "RMSE模型", "rmse_zero": "RMSE基线",
        "long_short": "多空收益%",
    })
    st.dataframe(show, use_container_width=True, hide_index=True)

    # IC 稳定性图
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[f"fold {f.fold}" for f in report.folds],
        y=[f.ic for f in report.folds],
        name="IC",
        marker_color=["#d73027" if f.ic > 0 else "#4575b4" for f in report.folds],
        text=[f"{f.ic:.4f}" for f in report.folds],
        textposition="outside",
    ))
    fig.add_hline(y=0, line_color="black", line_width=1)
    fig.add_hline(y=0.02, line_dash="dash", line_color="gray",
                  annotation_text="|IC|=0.02 弱有效门槛")
    fig.update_layout(
        height=320, yaxis_title="IC（秩相关）",
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "重点关注**符号是否稳定**：各折 IC 正负交替，说明信号是噪声；"
        "持续同号且幅度相近，才算有稳定预测力。"
    )

    st.divider()

    # 特征重要性
    imp = st.session_state.get("importance")
    if imp is not None and not imp.empty:
        st.subheader("特征重要性")
        st.caption(
            "树模型的特征重要性。**重要 ≠ 能赚钱**——"
            "它只说明模型在训练集上依赖该特征，需结合 IC 与分组收益判断。"
        )
        fig = px.bar(
            imp, x="importance_pct", y="feature", orientation="h",
            text="importance_pct",
            labels={"importance_pct": "重要性占比 %", "feature": ""},
            color="importance_pct", color_continuous_scale="Blues",
        )
        fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig.update_layout(height=400, showlegend=False, yaxis={"autorange": "reversed"})
        st.plotly_chart(fig, use_container_width=True)

else:
    st.info("点击「运行实验」开始。首次运行约需 5–20 秒（取决于 K 线数据量）。")

st.divider()
with st.expander("📋 方法与口径说明"):
    st.markdown("""
### 这份结果是怎么算出来的

**特征**（当日收盘后即可知，不含任何未来信息）

| 因子 | 含义 |
|---|---|
| `ret_1d` | 当日收益率 |
| `mom_5d / 20d / 60d / 120d` | 过去 N 个交易日累计收益 |
| `vol_20d` | 20 日收益率标准差 × √250，年化波动率 |
| `ma20_dev / ma60_dev` | 收盘价相对 20/60 日均线的偏离 |
| `dd_20d` | 20 日内从最高点回撤幅度 |
| `volume_ratio` | 当日成交量 / 20 日均量 |

**标签**：未来 5 个交易日的累计收益。该列仅用于训练与评估，**不会进入特征集**。

**切分方式**：扩张窗口时序验证。把时间轴分成 N+1 段，第 i 折用前 i 段训练、第 i+1 段测试。
训练集的最后一个日期始终早于测试集的第一个日期——这是防止未来信息泄漏的硬约束。

**评估指标**

| 指标 | 定义 | 读法 |
|---|---|---|
| IC | 预测值与真实收益的 Spearman 秩相关 | 0 表示与随机猜测无异；0.02–0.05 属微弱信号 |
| 多空收益 | 按预测值排序，头 10% 组与尾 10% 组的真实收益差 | 只反映排序能力，不含交易成本 |
| RMSE vs 基线 | 模型预测误差 对比「一律预测 0」的误差 | 优于基线才说明模型捕捉到了结构 |

### 关于这个结果的解读

**IC 在 0.02–0.03 区间，且后两折符号翻负——这更接近无信号，而非有效信号。**

学术与业界经验是 A 股短期收益接近随机游走。单因子 IC 达到 0.02–0.05 通常已具备一定区分度，
但前提是**符号稳定**。本项目的分折 IC 从 +0.07 翻转到 −0.01，说明信号不具持续性。

RMSE 未能优于「一律预测 0」的基线，进一步印证模型没有捕捉到可泛化的结构。

**这些负面结果是可以复现的**，因为：
- 时序切分严格，不存在未来信息泄漏
- 单元测试断言「训练集时间必须早于测试集」
- 单元测试断言「纯噪声数据上 IC 不应超过阈值」，防止评估代码本身存在泄漏

### 已知局限

1. **数据未经复权处理**：新浪源的日线为不复权价格，除权除息日会出现非真实跳空，会影响动量类因子
2. **样本期较短**：约 3 年日频、单一市场，无法支撑长周期因子（如 250 日动量）的稳健检验
3. **未纳入交易成本**：未考虑佣金、印花税、滑点、涨跌停无法成交、停牌等因素
4. **未做多重比较校正**：本页对 10 个因子逐一检验，未对多重比较导致的假阳性做校正
5. **行业分类口径**：采用新浪 48 类行业分类，非申万/中信标准

### 定位

本页是**因子有效性研究**，用于回答「这些量价指标是否存在可泛化的预测力」，
不构成任何策略或投资建议。
""")
