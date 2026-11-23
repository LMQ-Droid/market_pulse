# 让看板自动保持最新

看板部署在 Streamlit Cloud 上，它读取的是**你提交到 GitHub 的 parquet 文件**。
云端不会自己采集数据，所以每天需要有人在本地跑一次管道并推送。

本文档配好之后，这件事每天 16:30 自动发生，你什么都不用做。

---

## 快速验证：先手动跑一次

```powershell
cd C:\Users\28456\Desktop\deepseek\market_pulse
.\.venv\Scripts\python.exe daily_update.py
```

它做四件事：

```
1. 采集    run_all.py        全市场快照 + 行业 + 800只K线 + 因子    约 80 秒
2. 导出    export_for_deploy.py  精简 parquet 包                    约 2 秒
3. 提交    git add data/deploy && git commit
4. 推送    git push origin main（失败重试 3 次）
```

成功时最后几行长这样：

```
18:13:07 INFO  |   TOTAL                                           19.5 MB
18:13:08 INFO  | 已提交：data: 行情更新至 2026-11-23
18:13:11 INFO  | 推送成功
18:13:11 INFO  | ============================================================
18:13:11 INFO  | 全部完成，耗时 88.3s
```

推送成功后，Streamlit Cloud 会自动检测到 GitHub 更新并重新部署，大约 1–2 分钟后网页上的数据就是新的。

---

## 全自动：注册 Windows 定时任务

### 步骤

1. **用管理员权限打开 PowerShell**
   （开始菜单右键 → 终端(管理员) / Windows PowerShell(管理员)）

2. **执行**：

```powershell
cd C:\Users\28456\Desktop\deepseek\market_pulse
powershell -ExecutionPolicy Bypass -File register_task.ps1
```

看到「注册成功」即可。

### 任务配置

| 项 | 值 |
|---|---|
| 任务名 | `market_pulse_daily` |
| 触发 | 周一至周五 **16:30** |
| 执行 | `.venv\Scripts\python.exe daily_update.py` |
| 超时 | 30 分钟 |
| 补跑 | 到点时电脑关机，开机后补跑一次 |
| 并发 | 忽略新实例（不会撞车） |

**为什么是 16:30**：A 股 15:00 收盘，留 90 分钟等数据源更新完毕。太早抓不到当天数据，太晚没必要。

### 验证

```powershell
# 1. 立刻手动触发一次，看是否成功
Start-ScheduledTask -TaskName "market_pulse_daily"

# 2. 看日志（等 2 分钟）
Get-Content logs\daily_update.log -Tail 30

# 3. 看任务运行状态
Get-ScheduledTaskInfo -TaskName "market_pulse_daily"
```

第 3 条命令的 `LastTaskResult` 为 `0` 表示成功。

### 卸载

```powershell
Unregister-ScheduledTask -TaskName "market_pulse_daily" -Confirm:$false
```

---

## 推送为什么会失败

定时任务**无人值守**，所以推送必须免密码。你的项目用的是 SSH 方式（`git@github.com:...`），已经配好 key，正常情况下不需要输密码。

如果日志里出现：

```
fatal: Could not read from remote repository
```

说明 SSH key 失效了（比如换了电脑、删了 key）。重新配：

```powershell
ssh -T git@github.com     # 测试，应返回 "Hi LMQ-Droid!"
```

不通就重做一次 key 配置（见 `DEPLOY.md`）。

---

## 脚本做了哪些保护

这些都是踩过坑才加的，不是过度设计：

| 保护 | 原因 |
|---|---|
| **周末跳过** | A 股不开市，跑了也是抓旧数据，白刷日志 |
| **采集失败不推送** | 宁可数据旧一天，也不要把半截数据推上去——看板崩了比数据旧严重 |
| **推送重试 3 次** | 国内网络推 GitHub 经常偶发超时，一次失败不代表真失败 |
| **文件锁** | 防止定时任务和手动运行撞车，导致数据写坏 |
| **提交信息带日期** | `git log` 一眼看出哪天数据成功了，不用翻日志 |
| **JSONL 日志** | 每次一行，可以统计"近 30 天成功率" |

---

## 数据新鲜度检查

任何时候想看数据是否最新：

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect('data/market.duckdb',read_only=True); print('最新交易日:', c.execute('SELECT MAX(date) FROM mart_industry_daily').fetchone()[0])"
```

或在网页上看侧边栏的「数据截至」日期。

---

## 已知限制

**1. 电脑必须开机**

这是本地方案的固有局限。如果长期不开机，数据就停在那天。

替代方案是 GitHub Actions 云端采集，但云端 IP 与你家 IP 不同，可能被数据源单独限流——开发期间腾讯接口就封过 IP。相比之下，本地方案更稳。

**2. 只能在工作日更新**

节假日（春节、国庆等）A 股不开市，但脚本只认「周一至周五」。这些天会跑一次、发现没新数据、提交被跳过（`data/deploy 无变化`），不影响正确性，只是日志里多一行。

**3. 免费版应用有冷启动**

Streamlit 免费版闲置一段时间会休眠，访问时需要 30–60 秒唤醒。这是平台限制，不是数据问题。

---

## 改时间 / 改频率

编辑 `register_task.ps1` 里的这一行：

```powershell
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:30"
```

改完重新执行注册脚本即可（会先删除旧任务）。
