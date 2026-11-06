# Creates .venv (Python 3.12 via uv) and installs pinned dependencies. Touches nothing global.
# Usage: .\scripts\setup.ps1 [-Dev]
param([switch]$Dev)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv is required: winget install astral-sh.uv" }
if (-not (Test-Path .\.venv\Scripts\python.exe)) { uv venv .venv --python 3.12 --seed }
$req = if ($Dev) { "requirements-dev.txt" } else { "requirements.lock" }
uv pip install --python .\.venv\Scripts\python.exe -r $req
.\.venv\Scripts\python.exe -m pip check
if (-not (Test-Path .env)) { Write-Host "No .env found. Copy .env.example to .env and fill it in." -ForegroundColor Yellow }
