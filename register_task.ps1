# Register a Windows scheduled task that refreshes market_pulse data daily.
#
# Run in an ADMIN PowerShell:
#   cd C:\Users\28456\Desktop\deepseek\market_pulse
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
#
# Uninstall:
#   Unregister-ScheduledTask -TaskName "market_pulse_daily" -Confirm:$false
#
# NOTE: this file must stay pure ASCII. PowerShell 5.1 reads BOM-less files
# as GBK on Chinese Windows; non-ASCII bytes corrupt the parser and break
# script blocks (this actually happened during development).

$ErrorActionPreference = "Stop"

$TaskName    = "market_pulse_daily"
$ProjectRoot = "C:\Users\28456\Desktop\deepseek\market_pulse"
$PythonExe   = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ScriptPath  = Join-Path $ProjectRoot "daily_update.py"

Write-Host ("=" * 64)
Write-Host "Register scheduled task: $TaskName"
Write-Host ("=" * 64)

if (-not (Test-Path $PythonExe)) {
    Write-Host "ERROR: python not found: $PythonExe" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: script not found: $ScriptPath" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    Write-Host "ERROR: not a git repo: $ProjectRoot" -ForegroundColor Red
    exit 1
}

Write-Host "Python : $PythonExe"
Write-Host "Script : $ScriptPath"
Write-Host ""

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument ('"' + $ScriptPath + '"') `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "16:30"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$result = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "market_pulse daily data refresh: fetch -> export -> push" `
    -Force

if ($null -eq $result) {
    Write-Host "REGISTER FAILED" -ForegroundColor Red
    exit 1
}

Write-Host "REGISTERED OK" -ForegroundColor Green
Write-Host ""
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Trigger : weekdays 16:30"
Write-Host ""
Write-Host "Verify with:"
Write-Host '  Start-ScheduledTask -TaskName "market_pulse_daily"'
Write-Host '  Get-Content logs\daily_update.log -Tail 30'
Write-Host '  Get-ScheduledTaskInfo -TaskName "market_pulse_daily"'
Write-Host ""
Write-Host "Note: the task runs as your current user and needs push access."
Write-Host "      SSH key or stored git credential must already be configured."
