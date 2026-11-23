# 注册 Windows 定时任务：每个交易日自动更新看板数据
#
# 用法（必须用管理员权限的 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
#
# 卸载：
#   Unregister-ScheduledTask -TaskName "market_pulse_daily" -Confirm:$false

$ErrorActionPreference = "Stop"

$TaskName    = "market_pulse_daily"
$ProjectRoot = "C:\Users\28456\Desktop\deepseek\market_pulse"
$PythonExe   = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ScriptPath  = Join-Path $ProjectRoot "daily_update.py"

Write-Host "=" * 64
Write-Host "注册定时任务: $TaskName"
Write-Host "=" * 64

# ---------- 前置检查 ----------
if (-not (Test-Path $PythonExe)) {
    Write-Error "找不到 Python: $PythonExe"
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "找不到脚本: $ScriptPath"
}
if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    Write-Error "项目目录不是 git 仓库，无法自动推送"
}

Write-Host "Python : $PythonExe"
Write-Host "脚本   : $ScriptPath"
Write-Host ""

# ---------- 动作 ----------
# 用 run_all 的父目录作为工作目录，保证 git 命令能找到仓库
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $ProjectRoot

# ---------- 触发（每个交易日 16:30）----------
# A 股 15:00 收盘，留 90 分钟等数据源更新完毕。
# 周末在脚本内部跳过，这里一并配置以简化逻辑。
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:30"

# ---------- 设置 ----------
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# StartWhenAvailable = 如果到点时电脑关机，开机后补跑一次

# ---------- 注册 ----------
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "A股看板每日数据更新：采集->导出->推送GitHub" | Out-Null

    Write-Host "注册成功" -ForegroundColor Green
    Write-Host ""
    Write-Host "任务信息："
    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName, State |
        Format-Table -AutoSize

    Write-Host "触发时间：工作日 16:30"
    Write-Host ""
    Write-Host "验证方式："
    Write-Host "  1. 手动跑一次看是否成功："
    Write-Host "     Start-ScheduledTask -TaskName `"$TaskName`""
    Write-Host "  2. 看运行日志："
    Write-Host "     Get-Content `"$ProjectRoot\logs\daily_update.log`" -Tail 30"
    Write-Host "  3. 看任务历史："
    Write-Host "     Get-ScheduledTaskInfo -TaskName `"$TaskName`""
    Write-Host ""
    Write-Host "注意：任务按你的当前用户身份运行，不需要输密码。"
    Write-Host "      但推送 GitHub 依赖已经配好的 SSH key 或凭据。"
}
catch {
    Write-Host "注册失败: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "常见原因："
    Write-Host "  - 没有用管理员权限运行 PowerShell"
    Write-Host "  - 系统禁用了任务计划服务"
    exit 1
}
