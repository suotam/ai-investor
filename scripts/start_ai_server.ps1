# Investor OS - optional local llama.cpp server helper.
# Binds to 127.0.0.1 ONLY. Never expose this server publicly.
# Reads defaults from config/settings.yaml (ai.server_* keys); override via parameters:
#   .\scripts\start_ai_server.ps1 -Model "C:\models\glimmer-30b-q4_k_m.gguf" -Context 16384
param(
    [string]$Executable = "",
    [string]$Model = "",
    [int]$Context = 0,
    [int]$Port = 8080
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

# pull defaults from Investor OS config (no secrets involved)
$cfg = & $Python -c "from src.config import load_settings; s=load_settings(); print(s.ai_server_executable); print(s.ai_server_model); print(s.ai_server_context); print(s.ai_base_url)" 2>$null
$cfgLines = $cfg -split "`r?`n"
if (-not $Executable) { $Executable = $cfgLines[0] }
if (-not $Model)      { $Model      = $cfgLines[1] }
if ($Context -eq 0)   { $Context    = [int]$cfgLines[2] }

if (-not (Test-Path $Executable)) {
    Write-Error "llama-server executable not found: $Executable (set ai.server_executable in config/settings.yaml)"
    exit 2
}
if (-not $Model) {
    Write-Error "No model configured. Set ai.server_model in config/settings.yaml or pass -Model."
    exit 2
}

# already running? (health check on the local port only)
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/models" -TimeoutSec 3 -UseBasicParsing
    Write-Host "AI server already running on 127.0.0.1:$Port - nothing to do."
    exit 0
} catch { }

Write-Host "Starting llama-server on 127.0.0.1:$Port (model: $Model, ctx: $Context)..."
# --host 127.0.0.1: local only, by design
Start-Process -FilePath $Executable -ArgumentList @(
    "-m", $Model, "--host", "127.0.0.1", "--port", "$Port", "-c", "$Context"
) -WindowStyle Minimized

# wait for health (up to 120 s - model load can be slow)
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/models" -TimeoutSec 3 -UseBasicParsing
        Write-Host "AI server is up."
        exit 0
    } catch { }
}
Write-Warning "AI server did not respond within 120 s; Investor OS will run deterministically."
exit 1
