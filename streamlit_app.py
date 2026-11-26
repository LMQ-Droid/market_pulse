# -*- coding: utf-8 -*-
"""Streamlit Community Cloud 入口 + 显式多页导航。

背景（踩过的坑）：
Streamlit 的「自动多页」机制以**主脚本所在目录**为准去找 `pages/` 子目录。
云端要求入口必须是仓库根目录下的文件，而看板代码放在 `dashboard/` 里，
于是主脚本在根目录、页面在 `dashboard/pages/` —— 两者对不上，
**子页面在云端完全不会出现在侧边栏**，本地却正常（因为本地可以直接
`streamlit run dashboard/app.py`）。

解决办法：用 `st.navigation` 做**显式**导航，手工指定每个页面的路径，
不再依赖目录约定。这是 Streamlit 官方推荐的多页方案。

本地同样可用：
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"

# 保证 src/、config/、dashboard/ 都能被导入
for p in (ROOT, DASHBOARD):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# 显式声明页面。路径用绝对路径，避免工作目录差异导致找不到文件。
pages = [
    st.Page(
        str(DASHBOARD / "app.py"),
        title="行业轮动看板",
        icon="📈",
        default=True,
    ),
    st.Page(
        str(DASHBOARD / "pages" / "1_AI_Lab.py"),
        title="因子有效性检验",
        icon="🔬",
    ),
]

nav = st.navigation(pages, position="sidebar")

# set_page_config 必须在页面脚本执行之前调用；
# 各页面自己内部也有调用，Streamlit 允许重复调用同一配置，
# 但为了不冲突，这里只设置导航相关的基础项。
nav.run()
