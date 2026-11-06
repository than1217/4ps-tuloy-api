# Read-only smoke test against the running local API. Optional: $env:SMOKE_USERNAME / $env:SMOKE_PASSWORD.
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python.exe -m scripts.smoke
exit $LASTEXITCODE
