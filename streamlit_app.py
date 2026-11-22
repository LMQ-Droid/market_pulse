# -*- coding: utf-8 -*-
"""Streamlit Community Cloud 入口文件。

云端只认仓库根目录下的入口脚本，且强制从根目录找依赖，
因此这个文件的作用是把 dashboard/app.py 转出去，
同时把项目根加入 sys.path，让 app.py 里的
`from config import settings` / `import data_access` 能找到模块。

本地也可以直接 `streamlit run streamlit_app.py`，行为一致。
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 关键：保证 src/、config/、dashboard/ 都能被导入
for p in (ROOT, ROOT / "dashboard"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# 以 __main__ 语义执行看板脚本，等价于直接运行它
runpy.run_path(str(ROOT / "dashboard" / "app.py"), run_name="__main__")
