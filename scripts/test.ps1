# Unit tests (no network). Add -Integration to also run the local-MySQL tests; that needs the
# TEST_MYSQL_* variables pointing at a throwaway LOCAL database (see docs\LOCAL_SETUP.md).
param([switch]$Integration)
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not $Integration) { $env:TEST_MYSQL_HOST = "" }
.\.venv\Scripts\python.exe -m pytest
exit $LASTEXITCODE
