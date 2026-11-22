# 部署到公网（手机可看 + 简历可放链接）

目标：拿到一个**永久 https 地址**，面试官点开就能看，不需要你的电脑开着。

推荐方案是 **Streamlit Community Cloud**——免费、永久、无需信用卡、自动 https。

---

## 前置说明：为什么不能直接上传现在的项目

| 问题 | 现状 | 解决 |
|---|---|---|
| DuckDB 文件太大 | `data/market.duckdb` 是 **156 MB**，GitHub 单文件上限 100 MB | 导出精简 Parquet（**18 MB**） |
| 云端没有采集环境 | 云端只跑看板，不跑 ETL | 看板自动降级读 Parquet |
| 云端没有你的数据目录 | `data/` 被 gitignore | 放行 `data/deploy/` |

本项目已经处理好上述三点：`dashboard/data_access.py` 会自动检测本地 DuckDB 是否存在，不存在则读 Parquet。**两种模式的 SQL 完全一致**，不需要维护两套代码。

---

## 第一步：导出精简数据

```powershell
cd C:\Users\28456\Desktop\deepseek\market_pulse
.\.venv\Scripts\python.exe export_for_deploy.py
```

输出应该是：

```
  industry_daily.parquet          34,803 rows      1527 KB
  breadth.parquet                      1 rows         1 KB
  industry_dim.parquet                48 rows         1 KB
  stock_industry.parquet           2,925 rows         9 KB
  snapshot.parquet                 5,576 rows       236 KB
  stock_daily.parquet            211,160 rows     16687 KB
  TOTAL                                           18.0 MB
```

包超过 50 MB 时脚本会返回警告——减少 `--days` 参数即可。

---

## 第二步：推到 GitHub

```powershell
cd C:\Users\28456\Desktop\deepseek\market_pulse

git init
git add .
# 确认 parquet 被纳入（这一步很重要，漏了云端会没数据）
git status --short | Select-String "deploy"

git commit -m "feat: A股行业轮动看板 - 采集/建模/BI 全链路"
```

在 GitHub 建一个**公开**仓库（Private 也能部署，但公开更好放简历），然后：

```powershell
git remote add origin https://github.com/<你的用户名>/market_pulse.git
git branch -M main
git push -u origin main
```

> ⚠️ **必查**：在 GitHub 网页上看 `data/deploy/` 目录，确认 6 个 `.parquet` 文件都在。
> 如果缺失，说明 `.gitignore` 规则没生效，云端会显示「数据未就绪」。

---

## 第三步：部署到 Streamlit Cloud

1. 打开 <https://share.streamlit.io>
2. 用 **GitHub 账号**登录
3. 点 **Create app** → **Deploy a public app from GitHub**
4. 填写：

   | 字段 | 值 |
   |---|---|
   | Repository | `<你的用户名>/market_pulse` |
   | Branch | `main` |
   | **Main file path** | `streamlit_app.py` |
   | App URL | 自定义，例如 `market-pulse-lmq` |

5. 点 **Deploy**，等 2–4 分钟构建

成功后地址形如：

```
https://market-pulse-lmq.streamlit.app
```

**这个地址就是永久链接**，手机浏览器直接打开，也可以写进简历。

---

## 第四步（可选）：进阶设置

### 改主题配色
`.streamlit/config.toml` 已配好（红涨绿跌的中式配色）。改完推上去会自动重新部署。

### 自定义域名
免费版不支持。想要 `pulse.你的域名.com` 需要自己的服务器 + 反向代理。

### 数据更新怎么办
云端是**只读快照**——它读的是你提交进仓库的 Parquet，不会自己采集。

更新流程：

```powershell
# 本地跑完整管道（会更新 market.duckdb）
python run_all.py

# 重新导出精简包
python export_for_deploy.py

# 推上去，云端自动重新部署
git add data/deploy/
git commit -m "data: 更新至 2026-11-20"
git push
```

建议每周更新一次。想全自动的话，用 GitHub Actions 定时任务跑导出——但采集依赖外部接口，云端 IP 可能被限流，不如本地稳。

---

## 备选方案对比

| 方案 | 成本 | 永久性 | 手机访问 | 适合 |
|---|---|---|---|---|
| **Streamlit Cloud**（本文） | 免费 | ✅ 永久 | ✅ 公网 | **推荐，简历可直接放** |
| 局域网 IP | 免费 | ✅ | ❌ 需同一 WiFi | 自己家里看 |
| Cloudflare Tunnel | 免费 | ⚠️ 地址每次变 | ✅ 公网 | 临时演示（见下） |
| 自己买服务器 | ~50元/月 | ✅ | ✅ 需备案 | 商用才考虑 |

### 临时方案：Cloudflare Tunnel（5 分钟，给面试官临时看一眼）

适合「现在就要发个链接」的场景，但**地址重启即失效**。

```powershell
# 下载 cloudflared（单文件 exe，约 40 MB）
# 方式 A：官方 GitHub（国内经常超时，实测本机 ReadTimeout）
#   https://github.com/cloudflare/cloudflared/releases/latest
#   选 cloudflared-windows-amd64.exe，重命名为 cloudflared.exe
#
# 方式 B：国内镜像加速（推荐）
#   https://mirror.ghproxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe

# 先启动看板
.\.venv\Scripts\streamlit.exe run dashboard/app.py --server.port 8501

# 另开一个窗口，建立隧道
.\cloudflared.exe tunnel --url http://127.0.0.1:8501
```

会打印一个 `https://xxx-xxx-xxx.trycloudflare.com` 地址，手机可以直接打开。

**限制**：关掉窗口就失效，不适合长期放简历。

### 局域网方案（最简单，零配置）

手机连**同一个 WiFi**，浏览器打开：

```
http://10.10.205.79:8501
```

已确认当前 Streamlit 监听在 `0.0.0.0`，局域网可直接访问。

如果打不开，多半是 Windows 防火墙拦了 8501 端口，用**管理员 PowerShell** 放行：

```powershell
New-NetFirewallRule -DisplayName "Streamlit 8501" -Direction Inbound -LocalPort 8501 -Protocol TCP -Action Allow
```

---

## 排查清单

| 现象 | 原因 | 解决 |
|---|---|---|
| 打开显示「数据未就绪」 | Parquet 没推上去 | 检查 GitHub 上 `data/deploy/` 是否有 6 个文件 |
| 构建失败 `ModuleNotFoundError` | 依赖漏了 | 检查 `requirements.txt`，或改用 `requirements-cloud.txt` |
| 页面空白 / 一直转圈 | 云端内存不足（免费版 1GB） | 减少 `--days`，把 `stock_daily.parquet` 压到 10 MB 内 |
| 图表报 `colorscale` 错误 | plotly 版本差异 | 已统一用 `color_continuous_scale`，若仍报错请锁定 `plotly<8` |
| 手机打开很慢 | 首次加载需下载 16 MB Parquet | 正常现象，之后有缓存会快 |

---

## 关于隐私

公开仓库意味着代码和这 18 MB 数据都可见。本项目数据全部来自公开行情接口，不含任何个人信息，可以公开。

如果你的路径/用户名出现在代码里让你不安，检查这几处：
- `README.md` 的绝对路径示例（已用相对路径）
- `logs/`（已 gitignore）
- `.streamlit/config.toml`（无敏感信息）
