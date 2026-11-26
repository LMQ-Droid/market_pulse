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

with st.expander("📐 实验设计（面试会问，先讲清楚）", expanded=True):
    st.markdown("""
**数据切分：扩张窗口时序验证**

```
|----训练----|--测试--|          fold 1
|------训练------|--测试--|      fold 2
|--------训练--------|--测试--|  fold 3
|----------训练----------|--测试--|  fold 4
```

时间永远是单向的：只用过去训练，预测未来。`sklearn.model_selection.KFold(shuffle=True)`
这种随机切分会把未来信息泄漏进训练集，得到漂亮但无意义的指标——这是量化里最常见的错误。

**评估指标**

| 指标 | 含义 | 判读 |
|---|---|---|
| **IC** | 预测值与真实收益的 Spearman 秩相关 | \\|IC\\| < 0.02 基本等于随机 |
| **RMSE vs 基线** | 对比「一律预测 0」 | 打不过基线说明模型无用 |
| **头尾 10% 收益差** | 多空组合收益 | 只关心排序能力，不关心绝对精度 |

**为什么用秩相关而不是 Pearson**：收益分布厚尾，极端值主导 Pearson；
实际交易只关心排序对不对，所以秩相关更贴近用法。
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
        "**这条结论可以直接写进简历**——包括负面的那些。"
        "「我验证了因子无效」比「我的模型准确率 85%」可信得多。"
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
with st.expander("🎤 面试可能被追问的问题"):
    st.markdown("""
**Q: 为什么 IC 只有 0.03，是不是项目失败了？**
A: 不是。学术与业界共识是 A 股短期收益接近随机游走，单因子 IC 在 0.02–0.05
已具备实用价值（顶级量化的多因子组合 IC 也就 0.05–0.10）。有意义的是
**这个数字是用正确方法测出来的**，而不是「我准确率 90%」那种互相矛盾的表述。

**Q: 怎么保证没有未来函数？**
A: 三层防护：① 特征计算全部用 `shift(n)` 向后看，滚动窗口不越界；
② 时序切分保证训练集时间全早于测试集；③ 管道里有 `check_no_future_leak`
断言，数据里出现未来日期直接中止写库。

**Q: 这个模型能实盘吗？**
A: 不能。缺少交易成本、滑点、涨跌停不可成交、停牌、复权因子变化的处理，
且样本只有 3 年、单市场。当前定位是「因子有效性研究」，不是策略。

**Q: 为什么不用 LSTM/Transformer？**
A: 数据量（约 3 年日频）撑不起深度模型，参数量远大于有效样本量。
先用线性模型建立基线，如果线性都没有 IC，换复杂模型只会过拟合得更好看。
先证明简单方法有效，再考虑复杂度——这是顺序问题。
""")
