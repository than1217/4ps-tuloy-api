# Stops the background API started by start.ps1 and verifies the port is released.
param([int]$Port = 8000)
Set-Location (Split-Path $PSScriptRoot -Parent)
$pidFile = ".run\api.pid"
if (-not (Test-Path $pidFile)) { Write-Host "No PID file; nothing to stop."; exit 0 }
$procId = [int](Get-Content $pidFile)
$p = Get-Process -Id $procId -ErrorAction SilentlyContinue
$venvPython = (Resolve-Path ".venv\Scripts\python.exe").Path
if ($p -and $p.Path -ne $venvPython) {
    # PID reused by an unrelated process since start.ps1 ran: never kill it.
    Write-Host "PID $procId is not this project's API ($($p.ProcessName)); removing stale PID file only." -ForegroundColor Yellow
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 1
}
if ($p) {
    # Windows cannot deliver Ctrl+C to a detached process; ending it closes its sockets,
    # including the SSH tunnel. Use start.ps1 -Foreground when a graceful shutdown matters.
    # The venv python.exe is a launcher with a child server process: end the whole tree.
    taskkill /PID $procId /T /F | Out-Null
    $p.WaitForExit(10000) | Out-Null
}
Remove-Item $pidFile -ErrorAction SilentlyContinue
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) { Write-Host "Port $Port still in use by PID $($still.OwningProcess)" -ForegroundColor Yellow; exit 1 }
Write-Host "API stopped; port $Port free."
