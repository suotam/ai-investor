# Investor OS - daily pipeline (Task Scheduler friendly)
# Registers via scripts\register_tasks.ps1 (explicit user action). No secrets are printed.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("pipeline-daily-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

if (-not (Test-Path $Python)) {
    "$(Get-Date -Format o) FATAL: venv python not found at $Python" | Out-File -Append -Encoding utf8 $LogFile
    exit 2
}

Set-Location $ProjectRoot
"$(Get-Date -Format o) starting daily pipeline" | Out-File -Append -Encoding utf8 $LogFile
& $Python -m src.main run daily *>> $LogFile
$code = $LASTEXITCODE
"$(Get-Date -Format o) daily pipeline finished with exit code $code" | Out-File -Append -Encoding utf8 $LogFile
exit $code
