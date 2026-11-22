# -*- coding: utf-8 -*-
"""market_pulse 看板：行业轮动与因子洞察。

设计原则：
- 只读 mart 层，不碰 raw/staging，保证口径唯一。
- 每个图回答一个具体问题，不是为了把页面填满。
- 页面上直接给结论文字，不要求读者自己从图里推断。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import data_access as da  # noqa: E402

st.set_page_config(
    page_title="market_pulse · A股行业轮动",
    page_icon="📈",
    layout="wide",
)


# ---------------------------------------------------------------- 数据访问


@st.cache_data(ttl=300)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """带缓存的统一查询。数据源由 data_access 决定（DuckDB / Parquet）。"""
    return da.query(sql, params=params)


def table_exists(name: str) -> bool:
    return da.has_table(name)


def fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%" if pd.notna(x) else "—"


# ---------------------------------------------------------------- 侧边栏

with st.sidebar:
    st.markdown("### 📈 market_pulse")
    st.caption("A股行业轮动与因子洞察")
    st.divider()

    if not table_exists("mart_industry_daily"):
        st.error("数据未就绪")
        st.code(
            "python -m src.pipeline.run --task snapshot\n"
            "python -m src.pipeline.run --task industry\n"
            "python -m src.pipeline.run --task kline --limit 500\n"
            "python -m src.pipeline.run --task build",
            language="bash",
        )
        st.stop()

    # 可用日期，供全局时间筛选
    dates = q("SELECT DISTINCT date FROM mart_industry_daily ORDER BY date DESC")
    date_list = dates["date"].tolist() if not dates.empty else []
    if not date_list:
        st.error("mart_industry_daily 为空，请先运行 --task build")
        st.stop()

    as_of = st.selectbox(
        "观察日（as of）",
        options=date_list,
        index=0,
        format_func=lambda d: str(d),
        help="所有图表只用该日及之前的数据，避免未来函数",
    )

    st.divider()
    st.caption(f"数据截至 **{as_of}**")
    n_ind = q(
        "SELECT COUNT(DISTINCT industry_name) n FROM mart_industry_daily WHERE date <= ?",
        (as_of,),
    )["n"].iloc[0]
    st.caption(f"行业数 **{n_ind}**")
    st.caption(f"数据源：{da.backend_label()}")


# ---------------------------------------------------------------- 顶部指标

st.title("A股行业轮动看板")
st.caption(
    f"观察日 {as_of} · 等权行业收益 · 数据源：新浪快照 + 腾讯K线"
)

head = q(
    """
    SELECT * FROM mart_market_breadth
    WHERE trade_date <= ?
    ORDER BY trade_date DESC LIMIT 1
    """,
    (as_of,),
)

if not head.empty:
    h = head.iloc[0]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("全市场股票数", f"{int(h['n_stocks']):,}")
    c2.metric(
        "上涨 / 下跌",
        f"{int(h['n_up']):,} / {int(h['n_down']):,}",
        delta=f"{h['n_up'] / max(h['n_stocks'], 1) * 100:.0f}% 上涨",
    )
    c3.metric("涨停 / 跌停", f"{int(h['n_limit_up'])} / {int(h['n_limit_down'])}")
    c4.metric(
        "成交额",
        f"{h['total_amount_yi']:.0f} 亿" if pd.notna(h["total_amount_yi"]) else "—",
    )
    pe, pb = h.get("median_per"), h.get("median_pb")
    c5.metric(
        "估值中位数 PE / PB",
        f"{pe:.1f} / {pb:.2f}" if pd.notna(pe) and pd.notna(pb) else "—",
    )

st.divider()

# ---------------------------------------------------------------- 一、行业轮动热力图

st.subheader("① 行业动量排名")
st.caption("按不同回看窗口的行业等权收益排序。三个窗口方向不一致时，说明板块正在切换。")

mom = q(
    """
    SELECT industry_name, mom_5d, mom_20d, mom_60d, n_stocks
    FROM mart_industry_daily
    WHERE date = ?
    ORDER BY mom_20d DESC NULLS LAST
    """,
    (as_of,),
)

if not mom.empty:
    windows = st.multiselect(
        "选择回看窗口",
        options=["mom_5d", "mom_20d", "mom_60d"],
        default=["mom_5d", "mom_20d", "mom_60d"],
        format_func=lambda c: c.replace("mom_", "近").replace("d", "日"),
    )

    if windows:
        melted = mom.melt(
            id_vars=["industry_name", "n_stocks"],
            value_vars=windows,
            var_name="window",
            value_name="return",
        )
        melted["window"] = melted["window"].str.replace("mom_", "近").str.replace("d", "日")

        # 用 DataFrame 透视成矩阵，行=行业，列=窗口，便于热力图对齐
        mat = melted.pivot(index="industry_name", columns="window", values="return")
        mat = mat.loc[mom["industry_name"].tolist()]  # 保持按 20 日排序

        fig = go.Figure(data=go.Heatmap(
            z=mat.values,
            x=list(mat.columns),
            y=list(mat.index),
            colorscale=[(0.0, "#1a9850"), (0.5, "#f7f7f7"), (1.0, "#d73027")],
            zmid=0,
            text=[[fmt_pct(v) for v in row] for row in mat.values],
            texttemplate="%{text}",
            textfont={"size": 10},
            colorbar={"title": "收益"},
            hovertemplate="%{y}<br>%{x}: %{text}<extra></extra>",
        ))
        fig.update_layout(
            height=max(400, len(mat) * 22),
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            yaxis={"autorange": "reversed"},
        )
        st.plotly_chart(fig, use_container_width=True)
        # 红绿说明：国内市场习惯红涨绿跌
        st.caption("配色按国内习惯：红涨绿跌。")

    # 直接给出结论，不让读者自己数格子
    if len(mom) >= 3:
        top = mom.iloc[0]
        bottom = mom.iloc[-1]
        st.info(
            f"**结论**：近 20 日领涨行业是 **{top['industry_name']}**"
            f"（{fmt_pct(top['mom_20d'])}），垫底的是 **{bottom['industry_name']}**"
            f"（{fmt_pct(bottom['mom_20d'])}），"
            f"强弱差 **{(top['mom_20d'] - bottom['mom_20d']) * 100:.1f}** 个百分点。"
        )

st.divider()

# ---------------------------------------------------------------- 二、动量加速

st.subheader("② 动量加速：谁在变强")
st.caption(
    "横轴是长期动量（20日），纵轴是短期动量（5日）。"
    "落在右上角且 y > x 的，是近期正在加速的行业。"
)

accel = q(
    """
    SELECT industry_name, mom_5d, mom_20d, n_stocks, vol_20d
    FROM mart_industry_daily
    WHERE date = ? AND mom_5d IS NOT NULL AND mom_20d IS NOT NULL
    """,
    (as_of,),
)

if not accel.empty:
    accel["is_accelerating"] = accel["mom_5d"] > accel["mom_20d"]
    accel["label"] = accel["is_accelerating"].map({True: "加速中", False: "减速中"})

    fig = px.scatter(
        accel,
        x="mom_20d",
        y="mom_5d",
        size="n_stocks",
        color="label",
        color_discrete_map={"加速中": "#d73027", "减速中": "#4575b4"},
        hover_name="industry_name",
        hover_data={"vol_20d": ":.3f", "n_stocks": True},
        labels={"mom_20d": "近20日收益", "mom_5d": "近5日收益", "n_stocks": "成分股数"},
        size_max=30,
    )
    fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)
    fig.add_vline(x=0, line_dash="dot", line_color="gray", opacity=0.5)
    fig.update_layout(height=520, legend_title="")
    st.plotly_chart(fig, use_container_width=True)

    n_acc = int(accel["is_accelerating"].sum())
    st.info(
        f"**结论**：{len(accel)} 个行业中 {n_acc} 个处于加速状态"
        f"（短期动量超过长期）。加速并不等于会继续涨——见下方因子有效性检验。"
    )

st.divider()

# ---------------------------------------------------------------- 三、估值分布

st.subheader("③ 估值：贵还是便宜")
st.caption("行业 PE 中位数，已剔除亏损股（负 PE 会让中位数失真）。")

val = q(
    """
    SELECT
        si.industry_name,
        MEDIAN(s.per) FILTER (WHERE s.per > 0) AS pe_median,
        MEDIAN(s.pb) FILTER (WHERE s.pb > 0) AS pb_median,
        COUNT(*) AS n
    FROM stg_snapshot s
    JOIN (
        SELECT code, MIN(industry_name) AS industry_name
        FROM dim_stock_industry GROUP BY code
    ) si ON si.code = s.code
    WHERE s.trade_date = (SELECT MAX(trade_date) FROM stg_snapshot WHERE trade_date <= ?)
    GROUP BY si.industry_name
    HAVING COUNT(*) >= 3
    ORDER BY pe_median DESC
    """,
    (as_of,),
)

if not val.empty:
    left, right = st.columns(2)

    with left:
        st.markdown("**行业 PE 中位数（高 → 低）**")
        fig = px.bar(
            val,
            x="pe_median",
            y="industry_name",
            orientation="h",
            color="pe_median",
            color_continuous_scale="RdYlGn_r",
            labels={"pe_median": "PE 中位数", "industry_name": ""},
        )
        fig.update_layout(height=max(400, len(val) * 20), showlegend=False,
                          margin={"l": 10, "r": 10, "t": 10, "b": 10})
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.markdown("**估值 vs 动量（四象限）**")
        merged = val.merge(
            mom[["industry_name", "mom_20d"]], on="industry_name", how="inner"
        ).dropna(subset=["mom_20d", "pe_median"])
        if not merged.empty:
            fig = px.scatter(
                merged,
                x="pe_median",
                y="mom_20d",
                text="industry_name",
                size="n",
                labels={"pe_median": "PE 中位数", "mom_20d": "近20日收益"},
                color="mom_20d",
                color_continuous_scale="RdYlGn",
            )
            fig.update_traces(textposition="top center", textfont_size=9)
            fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)
            fig.update_layout(height=max(400, len(val) * 20))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "左下角（低估值 + 弱动量）是「便宜但没人买」；"
                "右上角（高估值 + 强动量）是「贵但有资金追」。"
            )

st.divider()

# ---------------------------------------------------------------- 四、市场宽度趋势

st.subheader("④ 市场宽度趋势")
st.caption("上涨家数占比是判断「指数涨但个股跌」这类结构分化的关键。")

breadth = q(
    """
    SELECT trade_date, n_stocks, n_up, n_down, avg_change, total_amount_yi
    FROM mart_market_breadth
    WHERE trade_date <= ?
    ORDER BY trade_date
    """,
    (as_of,),
)

# 宽度表与行业表可能因交易日口径不同而错开（例如快照日期为运行日、
# K 线为最后交易日）。当按 as_of 过滤为空时，退回取全表最近数据，
# 避免整块区域空白。
if breadth.empty:
    breadth = q(
        """
        SELECT trade_date, n_stocks, n_up, n_down, avg_change, total_amount_yi
        FROM mart_market_breadth
        ORDER BY trade_date
        """
    )
    if not breadth.empty:
        latest_b = breadth.iloc[-1]["trade_date"]
        st.caption(
            f"注：按观察日 {as_of} 过滤无数据，以下显示宽度表最近日期 {latest_b} 的数据。"
        )

if len(breadth) >= 2:
    breadth["up_ratio"] = breadth["n_up"] / breadth["n_stocks"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=breadth["trade_date"], y=breadth["total_amount_yi"],
        name="成交额（亿）", yaxis="y2", marker_color="rgba(150,150,150,0.35)",
    ))
    fig.add_trace(go.Scatter(
        x=breadth["trade_date"], y=breadth["up_ratio"],
        name="上涨家数占比", mode="lines+markers", line={"color": "#d73027", "width": 2},
    ))
    fig.add_hline(y=0.5, line_dash="dash", line_color="gray", opacity=0.6,
                  annotation_text="50% 荣枯线")
    fig.update_layout(
        height=420,
        yaxis={"title": "上涨家数占比", "tickformat": ".0%", "range": [0, 1]},
        yaxis2={"title": "成交额（亿）", "overlaying": "y", "side": "right",
                "showgrid": False},
        legend={"orientation": "h", "y": 1.12},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
    )
    st.plotly_chart(fig, use_container_width=True)

    latest = breadth.iloc[-1]
    st.info(
        f"**结论**：{latest['trade_date']} 上涨家数占比 **{latest['up_ratio']*100:.1f}%**，"
        f"平均涨跌 {latest['avg_change']:+.2f}%。"
        f"占比长期低于 50% 而指数不跌，通常意味着资金集中在少数权重股。"
    )
elif len(breadth) == 1:
    # 单日快照无法画趋势——如实说明，而不是画一条无意义的点
    b = breadth.iloc[0]
    ratio = b["n_up"] / b["n_stocks"] * 100
    st.info(
        f"**当前仅有一天快照**（{b['trade_date']}）：上涨 {int(b['n_up']):,} 家 / "
        f"下跌 {int(b['n_down']):,} 家，上涨占比 **{ratio:.1f}%**，"
        f"全市场平均涨跌 {b['avg_change']:+.2f}%。\n\n"
        f"趋势图需要至少两个交易日的数据。若要让此图生效，"
        f"请每天运行 `python run_all.py`（或挂到 Windows 任务计划），"
        f"累积几天后自动出现走势。"
    )
else:
    st.warning("暂无市场宽度数据，需要至少一天的快照。")

st.divider()

# ---------------------------------------------------------------- 五、个股筛选

st.subheader("⑤ 个股筛选")

if table_exists("mart_stock_daily"):
    industries = ["全部"] + sorted(
        q("SELECT DISTINCT industry_name FROM mart_stock_daily "
          "WHERE industry_name IS NOT NULL")["industry_name"].tolist()
    )
    c1, c2, c3 = st.columns([2, 2, 2])
    pick_ind = c1.selectbox("行业", industries)
    sort_col = c2.selectbox(
        "排序字段",
        ["mom_20d", "mom_5d", "vol_20d", "ma20_dev", "volume_ratio"],
        format_func=lambda c: {
            "mom_20d": "近20日动量", "mom_5d": "近5日动量",
            "vol_20d": "年化波动率", "ma20_dev": "20日均线偏离",
            "volume_ratio": "量比",
        }.get(c, c),
    )
    top_n = c3.slider("显示条数", 10, 100, 30, step=10)

    where = "date = ?"
    params: list = [as_of]
    if pick_ind != "全部":
        where += " AND industry_name = ?"
        params.append(pick_ind)

    stocks = q(
        f"""
        SELECT code, name, industry_name, close, ret_1d,
               mom_5d, mom_20d, vol_20d, ma20_dev, volume_ratio, dd_20d
        FROM mart_stock_daily
        WHERE {where}
        ORDER BY {sort_col} DESC NULLS LAST
        LIMIT ?
        """,
        tuple(params + [top_n]),
    )

    if not stocks.empty:
        show = stocks.copy()
        for col in ["ret_1d", "mom_5d", "mom_20d", "vol_20d", "ma20_dev", "dd_20d"]:
            if col in show.columns:
                show[col] = (show[col] * 100).round(2)
        show = show.rename(columns={
            "code": "代码", "name": "名称", "industry_name": "行业",
            "close": "收盘", "ret_1d": "日涨跌%", "mom_5d": "近5日%",
            "mom_20d": "近20日%", "vol_20d": "波动率%", "ma20_dev": "均线偏离%",
            "volume_ratio": "量比", "dd_20d": "回撤%",
        })
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.download_button(
            "下载 CSV",
            show.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"stocks_{as_of}.csv",
            mime="text/csv",
        )
    else:
        st.warning("该筛选条件下无数据。")
else:
    st.warning("mart_stock_daily 未生成，请运行 --task build。")

# ---------------------------------------------------------------- 页脚

st.divider()
with st.expander("📋 数据口径与已知限制"):
    st.markdown("""
**口径**
- 行业收益：成分股**等权**平均，不用市值加权（避免少数超大盘股主导）
- 动量：`close_t / close_{t-N} - 1`
- 波动率：20 日收益率标准差 × √250
- 估值：行业内 PE 中位数，剔除负 PE（亏损股）

**已知限制**（面试被问到时主动说出来，比藏起来好）
1. 新浪快照是**盘中/收盘快照**，非严格历史序列，宽度趋势需要持续采集才准
2. 行业分类用新浪行业（49 个），不是申万/中信标准分类
3. K线取近 3 年，长周期因子（如 250 日动量）样本不足
4. 未做交易成本、涨跌停不可成交等回测细节
""")
