# Staged read-only connectivity check: config, bastion, host key + auth, forward, DB auth, queries, grants.
# Prints PASS/FAIL per stage; never prints secrets. Exit code = first failing stage (0 = all passed).
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python.exe -m scripts.check_connection
exit $LASTEXITCODE
