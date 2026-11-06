# Local setup (Windows / PowerShell)

Requirements: `uv` (`winget install astral-sh.uv`) and a bastion host key already in `%USERPROFILE%\.ssh\known_hosts`. No global Python packages are installed.

## 1. Install

```powershell
cd C:\Projects\4ps\tuloy\new\tuloy-api-main
.\scripts\setup.ps1          # .venv with Python 3.12 + hashed requirements.lock
.\scripts\setup.ps1 -Dev     # also pytest, httpx2, pip-audit (requirements-dev.txt)
Copy-Item .env.example .env  # first time only, then fill in values (never commit .env)
```

`APP_ENV` has no default. Set `APP_ENV=local` for a workstation.

The bastion's host key must already be trusted. If it isn't, verify the fingerprint with the infrastructure owner first, then add it:

```powershell
ssh-keygen -F <bastion-host> -l   # shows pinned fingerprints, if any (use the SSH_HOST value)
```

Never disable host-key checking. The API and the check script both refuse to connect without a pinned key.

## 2. Check the connection (read-only, staged)

```powershell
.\scripts\check-connection.ps1
```

| Stage | What it proves |
|---|---|
| 0 config | `.env` parses and is consistent (values are not printed) |
| a | TCP to the bastion |
| b | Host key matches `known_hosts`, and SSH auth succeeds |
| c | Forward from `127.0.0.1:<ephemeral>` to the DB host and port |
| d | MySQL authentication |
| e | `SELECT 1` on a `READ ONLY` session with a statement time limit |
| f | Required views and tables exist, the view has the expected columns, and a redacted grants listing (warns on write/DDL privileges) |

Exit code is 0 when every stage passes; otherwise it's the number of the first failing stage. The tunnel is always closed at the end.

## 3. Start, smoke test, stop

```powershell
.\scripts\start.ps1                 # background on 127.0.0.1:8000; logs .run\api.log
.\scripts\smoke.ps1                 # health, readiness, OpenAPI, auth path; no writes
.\scripts\stop.ps1                  # ends the process tree; verifies the port is free

.\scripts\start.ps1 -Foreground     # Ctrl+C = graceful shutdown (closes tunnel and pools)
```

- Docs are at `http://127.0.0.1:8000/docs` when `APP_ENV` is `local` or `dev`.
- The Partner routes appear only with `PARTNER_API_ENABLED=true`.
- If `SECRET_KEY` is empty locally, each start generates a random key, so tokens die on restart.
- For the smoke test's DB-backed auth check, run the API and the smoke test in the same PowerShell session with `SECRET_KEY` set.
- For the authenticated read check, set `$env:SMOKE_USERNAME` and `$env:SMOKE_PASSWORD` to a provisioned test account. The check calls `/VerifyMemberBene` with a synthetic person and expects "Not Found".

## 4. Tests

```powershell
.\scripts\test.ps1                  # unit tests, offline (no .env is read)
.\scripts\test.ps1 -Integration     # plus tests against a throwaway LOCAL MySQL
```

The integration tests skip themselves unless `TEST_MYSQL_HOST` is a loopback address. To create a throwaway server (it never touches a shared database):

```powershell
$d = "$env:TEMP\tuloy-mysql-test"
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe" --no-defaults --initialize-insecure --datadir="$d\data"
Start-Process "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe" -ArgumentList "--no-defaults","--datadir=$d\data","--port=3407","--bind-address=127.0.0.1","--mysqlx=OFF"
# then, as root on 127.0.0.1:3407: create database tuloy_test; a migration user with ALL on tuloy_test;
# a runtime user with SELECT on tuloy_test, then (after migrations) INSERT on api_users,
# matching_transaction_CHED_tbl, enroll_matched_CHED_tbl. Put both in TEST_MYSQL_* variables.
```

## 5. Migrations (application tables only)

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate status
.\.venv\Scripts\python.exe -m scripts.migrate apply            # loopback DB only, asks first
```

- The API never runs migrations.
- The runner uses `MIGRATION_DB_USER`, never the runtime account.
- A remote target needs `--allow-remote` and typing the database name. Do this only with DBA approval, a verified backup and a maintenance window (ADR 2026-10-22).
- `0001` mirrors the live tables. `0002` and `0003` are proposals.

## 6. Troubleshooting

| Symptom | Stage / cause |
|---|---|
| `config error: ...` | `.env` is missing a value or has an invalid one; the message names the variable |
| `FAIL a` | No network path to the bastion (VPN or firewall) |
| `FAIL b ... not in known_hosts` / `HOST KEY MISMATCH` | Stop. Verify the fingerprint out of band |
| `FAIL b authentication rejected` | SSH credentials wrong or rotated |
| `FAIL d MySQL error 1045` | DB user or password wrong |
| `config error: APP_ENV must be one of ...` | Add `APP_ENV=local` to `.env` |
| `/readyz` 503 | Tunnel or DB down. The API keeps running; after a failed connect it answers `503 DB_UNAVAILABLE` at once for 5 s, then retries |
| `503 DB_BUSY` | Pool exhausted. Raise `DB_POOL_SIZE` within the DB connection budget |
| `503 DB_TIMEOUT` | A statement exceeded `DB_QUERY_TIMEOUT_MS` |
