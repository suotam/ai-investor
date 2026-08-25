# Investor OS - weekly pipeline (Task Scheduler friendly)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("pipeline-weekly-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

if (-not (Test-Path $Python)) {
    "$(Get-Date -Format o) FATAL: venv python not found at $Python" | Out-File -Append -Encoding utf8 $LogFile
    exit 2
}

Set-Location $ProjectRoot
"$(Get-Date -Format o) starting weekly pipeline" | Out-File -Append -Encoding utf8 $LogFile
& $Python -m src.main run weekly *>> $LogFile
$code = $LASTEXITCODE
"$(Get-Date -Format o) weekly pipeline finished with exit code $code" | Out-File -Append -Encoding utf8 $LogFile
exit $code
