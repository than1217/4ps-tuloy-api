# Starts the API on 127.0.0.1 only.
#   .\scripts\start.ps1              background; logs in .run\api.log, PID in .run\api.pid
#   .\scripts\start.ps1 -Foreground  Ctrl+C shuts down gracefully (closes the SSH tunnel and pools)
param([switch]$Foreground, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$uvArgs = @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "$Port", "--workers", "1",
            "--timeout-graceful-shutdown", "15", "--no-server-header", "--log-level", "info")
if ($Foreground) { & $py @uvArgs; exit $LASTEXITCODE }
New-Item -ItemType Directory -Force .run | Out-Null
$pidFile = ".run\api.pid"
if (Test-Path $pidFile) {
    $old = Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue
    if ($old) { throw "API already running (PID $($old.Id)). Run .\scripts\stop.ps1 first." }
}
$p = Start-Process -FilePath $py -ArgumentList $uvArgs -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput ".run\api.log" -RedirectStandardError ".run\api.err.log"
$p.Id | Set-Content $pidFile
$healthy = $false
foreach ($i in 1..30) {
    try { $r = Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { $healthy = $true; break } } catch { }
    if ($p.HasExited) { throw "API exited during startup; see .run\api.err.log" }
    Start-Sleep -Milliseconds 500
}
if (-not $healthy) {
    taskkill /PID $p.Id /T /F | Out-Null
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    throw "API did not answer /healthz within 15 s; stopped it. See .run\api.err.log"
}
Write-Host "API started (PID $($p.Id)) at http://127.0.0.1:$Port  -  readiness: http://127.0.0.1:$Port/readyz"
