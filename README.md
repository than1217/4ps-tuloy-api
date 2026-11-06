# Tuloy API

FastAPI service that verifies 4Ps members against the ODS views, records CHED enrollment matches, and hosts the Partner API under `/v1/api/tuloy` (ADR 2026-10-26).

**Status:** local and staging-read verified. **Not production ready.** See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) for the blockers.

| Doc | What it covers |
|---|---|
| [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) | Install, connection check, start/smoke/stop, tests, migrations, troubleshooting |
| [docs/PARTNER_API_CONTRACT.md](docs/PARTNER_API_CONTRACT.md) | The six Partner operations, scopes, and what still needs approval |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | Architecture, database objects, findings, ODS guide gap assessment |
| [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) | Done vs proposed vs external, review findings, GRANT set, rollback |
| [docs/RBAC.md](docs/RBAC.md) | Scopes per route, grant lifecycle, `scripts/grants.py`, impact notice for legacy callers |
| [.env.example](.env.example) | Every setting, with the rules enforced outside local/dev |

## Quick start (Windows / PowerShell)

```powershell
.\scripts\setup.ps1 -Dev            # .venv (Python 3.12) from hashed requirements.lock
Copy-Item .env.example .env         # first time only; fill in; never commit .env
.\scripts\check-connection.ps1      # staged, read-only: bastion, host key, tunnel, MySQL, objects
.\scripts\start.ps1                 # 127.0.0.1:8000, background; logs in .run\
.\scripts\smoke.ps1                 # read-only checks, no writes
.\scripts\stop.ps1
.\scripts\test.ps1                  # unit tests, offline
```

`APP_ENV` is required. The API never creates or alters tables at startup. Schema changes go through `scripts/migrate.py` with a separate DDL account (see LOCAL_SETUP §5).

## Layout

| Path | Role |
|---|---|
| `main.py` | App factory, lifespan, middleware (request ID, body limit, rate limit, security headers, logging) |
| `config.py` | Typed settings; fails fast on invalid or unsafe values |
| `database.py` | Read-only and write pools, optional SSH tunnel, fail-fast cooldown, error mapping |
| `tunnel.py` | SSH forward on `127.0.0.1`, pinned host key required |
| `security.py` | JWT (iss/aud/exp/jti), bcrypt, `Principal`, `require_scope` |
| `errors.py`, `observability.py`, `ratelimit.py` | Error body, JSON logs with redaction and audit events, limiter |
| `routers/` | `/healthz`, `/readyz`, `/token`, `/signup`, legacy beneficiary routes |
| `partner/router.py` | The six Partner operations (authenticate + authorize, then `501` until approved) |
| `migrations/`, `scripts/` | SQL migrations and runner; connection check; smoke test; PowerShell helpers |
| `tests/` | Unit tests (fake DB) and integration tests (throwaway local MySQL only) |
| `others/` | CHED CSV import scripts. They post real data: DPO approval first |

## Endpoints

| Method and path | Auth | Notes |
|---|---|---|
| `GET /healthz` | none | Liveness. Touches no dependency |
| `GET /readyz` | none | Tunnel and a read-only `SELECT 1`; `503` when down |
| `POST /token` | none | OAuth2 password form. Returns `access_token`, `expires_in`. Throttled per IP and user |
| `POST /signup` | none | `404` unless `ALLOW_SIGNUP=true` (local/dev only) |
| `POST /VerifyMemberBene` | Bearer + `tuloy:legacy:member:verify` | `first_name`, `last_name`, `date_of_birth` required; returns `Matched`, `Multiple Match Found` or `Not Found` |
| `GET /GetEligibleBene` | Bearer + `tuloy:legacy:beneficiary:read` | `limit` (≤1000), `offset`; `has_more` / `next_offset`, no total count |
| `POST /PostEnrolleeMatchedCHED` | Bearer + `tuloy:legacy:ched:write` | Writes the CHED match tables. Not idempotent |
| `GET /GetMatchedChedBene` | Bearer + `tuloy:legacy:ched:read` | Pages `enroll_matched_CHED_tbl` by `id` |
| `/v1/api/tuloy/...` (six operations) | Bearer + scope | Mounted when `PARTNER_API_ENABLED=true`; see the contract |

Errors use one body: `{"detail": ..., "error_code": ..., "request_id": ...}`. Every response carries `X-Request-ID`.

Example (synthetic values):

```powershell
$tok = (Invoke-RestMethod -Method Post http://127.0.0.1:8000/token -Body @{username=$env:SMOKE_USERNAME; password=$env:SMOKE_PASSWORD}).access_token
Invoke-RestMethod -Method Post http://127.0.0.1:8000/VerifyMemberBene -Headers @{Authorization="Bearer $tok"} `
  -ContentType application/json -Body '{"first_name":"Zzsample","last_name":"Synthetic","date_of_birth":"1900-01-01"}'
```

## Data handling

Responses and the cache contain personal data. Never put real records in tests, docs, tickets or logs. Logs redact identifiers and record route templates, not concrete paths. `*.csv` and `.env*` (except `.env.example`) are git-ignored.
