# Investor OS - register Windows Task Scheduler jobs (EXPLICIT user action; nothing is
# registered automatically). Run from an elevated or normal PowerShell:
#   .\scripts\register_tasks.ps1                 # daily 07:00, weekly Sunday 08:00
#   .\scripts\register_tasks.ps1 -DailyTime 06:30 -WeeklyDay SUN -WeeklyTime 09:00
#   .\scripts\register_tasks.ps1 -Unregister     # remove both tasks
param(
    [string]$DailyTime = "07:00",
    [string]$WeeklyDay = "SUN",
    [string]$WeeklyTime = "08:00",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DailyScript = Join-Path $ProjectRoot "scripts\run_daily.ps1"
$WeeklyScript = Join-Path $ProjectRoot "scripts\run_weekly.ps1"

if ($Unregister) {
    schtasks /Delete /TN "InvestorOS Daily" /F 2>$null
    schtasks /Delete /TN "InvestorOS Weekly" /F 2>$null
    Write-Host "Investor OS tasks removed."
    exit 0
}

# /SC DAILY at local $DailyTime; the time is user-defined - no timezone assumptions in code.
schtasks /Create /F /TN "InvestorOS Daily" /SC DAILY /ST $DailyTime `
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$DailyScript`""
schtasks /Create /F /TN "InvestorOS Weekly" /SC WEEKLY /D $WeeklyDay /ST $WeeklyTime `
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$WeeklyScript`""
Write-Host "Registered: 'InvestorOS Daily' ($DailyTime) and 'InvestorOS Weekly' ($WeeklyDay $WeeklyTime)."
Write-Host "Inspect with: schtasks /Query /TN `"InvestorOS Daily`" /V"
