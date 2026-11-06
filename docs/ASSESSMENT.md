# tuloy-api-main: architecture and findings

Assessment date: 2026-10-26. Baseline: local commit `a4cb938` (the repository as received). Changes: `7b5449b` and later.
Line references marked *baseline* point at `a4cb938`; use `git show a4cb938:<file>` to view them.

## 1. Reference material

| Reference | Path | Version / date | Authority | Notes |
|---|---|---|---|---|
| ODS engineering guide | `C:\workspace\second-brain\30-Standards\ods-engineering-guide.md` | Last updated 2026-08-22, 1,423 lines | **Department baseline** (vault `CLAUDE.md`, `30-Standards/README.md`) | Byte-identical (MD5 `3183560d…`) to the candidate `C:\Projects\4ps\tuloy-delivery\docs\standards\ODS_ENGINEERING_GUIDE.md`, so that candidate is verified as the authoritative guide. It targets Next.js + PostgreSQL; FastAPI mapping in `tuloy-delivery\docs\standards\FASTAPI_REACT_ADAPTATION.md`. |
| Tuloy decisions (ADRs) | `C:\workspace\second-brain\40-Decisions\2026-10-1*-tuloy-*.md`, `2026-10-2*-tuloy-*.md` | 2026-10-17 to 2026-10-26 | Accepted decisions | See conflicts below. |
| Partner operation matrix | `C:\workspace\second-brain\20-Projects\TULOY\analysis\2026-10-18-partner-operation-matrix.md` | 2026-10-18, approved | Martin approved the 7 partner operations | Source of the scope names used here. |
| Reference implementation | `C:\Projects\4ps\tuloy\4ps-tuloy-api` | branch `feat/legacy-identity-jwt`, HEAD `7861e22` (2026-10-23) with uncommitted WIP | Reference only (not changed) | Already implements all six `/v1/api/tuloy` operations (`app/api/routers/tuloy.py:118-576`). |
| Repo working agreements | `C:\Projects\4ps\tuloy-delivery\AGENTS.md` | 2026-10-14 | Applicable practice | No `AGENTS.md` exists in this repository. |
| New ADR | `C:\workspace\second-brain\40-Decisions\2026-10-26-tuloy-api-main-partner-api-host.md` | 2026-10-26 | Martin's choice in chat | Makes this repo the Partner API host. |

**Conflicts recorded**

1. ADR 2026-10-19 rejected this repository as the Partner API (option C) and kept it as a restricted reference. On 2026-10-26 Martin chose to make it the Partner API host; the new ADR amends that decision.
2. The six operations already exist in `4ps-tuloy-api`. Two implementations of one contract now exist; a single authoritative enrollment writer must be named before partner cutover.
3. Both repositories target the same schema (`dswd_4ps_tuloy_dev`). `4ps-tuloy-api` plans Alembic there; this repo adds `scripts/migrate.py` with a `schema_migrations` ledger. Only one migration system should own a given table (see DB finding D-06).
4. Sex codes disagree across sources: the reference API uses `1 = Male, 2 = Female`; the partner request sample (`others/Request Params.png`) says CHED `0 = Male, 1 = Female`, TESDA `0 = Female, 1 = Male`; ODS `sex_id` meanings are unconfirmed (open task in the TULOY README).

**Capabilities used**

- Prompt Master: used to produce the execution prompt.
- Second Brain: the vault was read (rules, TULOY README, ADRs, partner matrix, ODS guide). `99-Restricted/` was not read.
- Council: the `/council` skill exists (Gemini/ChatGPT seats via external CLIs). It was **not** run for the code review because it sends a brief to external services and is a decision meeting, not a code review. Four in-repo reviewer agents (architecture, database, security, operations) reviewed the diff in parallel instead; results are in `PRODUCTION_READINESS.md` §4.

## 2. Architecture

Single FastAPI process, synchronous `mysql-connector-python`, optional SSH tunnel (`sshtunnel`/paramiko), `fastapi-cache2` (Redis or in-memory).

```
client ──HTTP──> uvicorn (127.0.0.1) ──> FastAPI
                                         ├─ /token, /signup                  api_users (app table)
                                         ├─ /VerifyMemberBene, /GetEligibleBene, /GetMatchedChedBene   ODS views (read-only)
                                         ├─ /PostEnrolleeMatchedCHED         CHED match tables (app tables, write)
                                         ├─ /v1/api/tuloy/* (6 ops, flag)    auth + scope, 501
                                         └─ /healthz, /readyz
             pools (ro / rw) ──> 127.0.0.1:<ephemeral> ──SSH (pinned host key)──> bastion ──> MySQL 8.0.43 (ODS host)
```

### Routes

| Method | Path | Auth | Reads / writes | Change in this work |
|---|---|---|---|---|
| POST | `/token` | none | reads `api_users` | Token now has `exp/iat/nbf/iss/aud/jti`; `expires_in` added; rate-limited; disabled users refused (403). |
| POST | `/signup` | none | writes `api_users` | Returns 404 unless `ALLOW_SIGNUP=true` (refused outside local/dev); 12-char password minimum. |
| POST | `/VerifyMemberBene` | Bearer | reads `vw_verify_member_bene` | Explicit columns, `LIMIT 2`, real-date validation (422), generic errors. Response shape unchanged. |
| GET | `/GetEligibleBene` | Bearer | reads `vw_verify_member_bene`, `enroll_matched_CHED_tbl` | Cache key per user; generic errors. No longer returns `m_maiden_name` or `account_no`; PhilSys and LRN show only the last 4 digits; still returns other personal data (see F-10). |
| POST | `/PostEnrolleeMatchedCHED` | Bearer | writes `matching_transaction_CHED_tbl`, `enroll_matched_CHED_tbl` | Same behavior; bounded fields; generic errors; audit event. |
| GET | `/GetMatchedChedBene` | Bearer | reads `vw_get_matched_CHED` | Explicit columns; `ORDER BY id` (stable pages, 16 ms measured). No longer returns `mm_first_name`, `mm_middle_name` or `mm_last_name` (data-owner rulings, ADR 2026-10-27); DSWD only. |
| 6 ops | `/v1/api/tuloy/...` | Bearer + scope | none | New, behind `PARTNER_API_ENABLED`; 501 until approved. |
| GET | `/healthz`, `/readyz` | none | `SELECT 1` (ready) | New. |

The README did not match the code in four places: it omitted `vw_get_matched_CHED` (the route reads the view, not the table), described tables "created automatically during startup" (the live FK differs from the startup DDL), listed `CREATE TABLE` permission as needed at runtime, and documented no token expiry while `ACCESS_TOKEN_EXPIRE_MINUTES` was never used.

### Configuration and credentials

*Before:* `load_dotenv(override=True)` ran in three places (`main.py:23,31`, `database.py:7`, baseline), so `.env` overrode real environment variables. No validation; `SECRET_KEY` fell back to a public default (`security.py:10`); `ACCESS_TOKEN_EXPIRE_MINUTES` was documented but ignored.

*After:* `config.py` reads the environment once, with `.env` as a fallback (real variables win; `TULOY_ENV_FILE=""` disables the file). It fails fast on missing or malformed values and refuses unsafe production settings. Secrets are excluded from `repr`. The SSH tunnel uses only the configured password or key (`allow_agent=False`, no `~/.ssh` scan, no `ssh_config`).

### Database objects (measured read-only on 2026-10-26)

| Object | Type | Used by | Access |
|---|---|---|---|
| `vw_verify_member_bene` | view (21 columns incl. `match_keys`) | verify, eligible list | read |
| `vw_get_matched_CHED` | view (27 columns) | matched list | read |
| `api_users` | table (~4 rows) | auth | read; write via `/signup` only |
| `matching_transaction_CHED_tbl` | table (~79k rows) | CHED post | write |
| `enroll_matched_CHED_tbl` | table (~81k rows); no index on `tuloy_id`; no unique key | CHED post, eligible filter | write, read |
| `education_enrollment` | table (~0 rows) | named in the old startup FK only | none |
| `users`, `roles`, `permissions`, `role_permissions` | tables (legacy identity used by `4ps-tuloy-api`) | not used here | none |

Runtime account grants (redacted): `SELECT ON *.*`, `ALL PRIVILEGES` on two schemas. This is a personal-looking account with DDL rights, not least privilege.

### Startup side effects (baseline)

`main.py:110-161` *(baseline)* ran on every start against the shared schema: three `CREATE TABLE IF NOT EXISTS`, `SET FOREIGN_KEY_CHECKS=0/1`, and `COMMIT`. Startup also probed Redis at `localhost:6379` by default and opened an SSH tunnel **without host-key verification** (sshtunnel logs `ssh_host_key: not checked`). A database or tunnel error aborted startup.

*After:* startup changes no schema (`tests/test_app.py::test_startup_performs_no_schema_changes`); DDL moved to `migrations/` and runs only through `scripts/migrate.py` with a separate account. A DB outage at startup is logged and reported by `/readyz` instead of crashing.

## 3. Findings

Status: **Fixed** (in this work, with test), **Open** (needs a person, DBA or a decision), **Mitigated** (reduced, not closed).

| ID | Sev | Finding | Source (baseline) | Status |
|---|---|---|---|---|
| F-01 | Critical | Startup DDL and `FOREIGN_KEY_CHECKS=0` on the shared staging schema at every start | `main.py:110-161` | Fixed: removed; `migrations/0001_baseline.sql` mirrors live DDL |
| F-02 | Critical | SSH tunnel did not verify the bastion host key; password sent to whatever answers | `main.py:87-98` | Fixed: pinned key from `known_hosts` (`tunnel.py`), staged check verifies |
| F-03 | Critical | Public default JWT secret; tokens never expire; no `iss/aud` | `security.py:10,38-41` | Fixed: required key outside local; `exp/iss/aud/jti` enforced |
| F-04 | High | Open self-registration: anyone could create an account and then read beneficiary data | `routers/auth.py:31` | Fixed: off by default, refused outside local/dev |
| F-05 | High | Raw database errors returned to clients (`detail=f"Database error: {e}"`) | `database.py:36`, `routers/beneficiaries.py:75,114,191,223` | Fixed: generic codes, request ID; DB message only as errno in logs |
| F-06 | High | Hard-coded `admin` / weak password and auto-signup in import scripts | `others/import_ched_csv.py:19-29`, `others/test_import_ched_csv.py:19-29` | Fixed in code. **Open:** if that account exists in `api_users`, disable or rotate it (owner action) |
| F-07 | High | Runtime account has DDL and wide SELECT (`ALL` on two schemas, `SELECT *.*`) | live grants | Open: DBA to create least-privilege runtime + separate migration account (`PRODUCTION_READINESS.md` §3) |
| F-08 | High | `others/CHED-Matching.csv` holds ~118k rows of real personal data inside the repository folder | file | Mitigated: git-ignored. **Open:** move to restricted storage; DPO decision |
| F-09 | Medium | Cache key ignored the caller; any user could be served another's cached page | `utils.py:16-19` | Fixed: user id in key (`tests/test_cache.py`) |
| F-10 | Medium | `/GetEligibleBene` returns names, birth dates and (masked) PhilSys and LRN to any authenticated user; no scope or region filter | `routers/beneficiaries.py:79-116` | Partly fixed: `m_maiden_name` and `account_no` removed and PhilSys/LRN masked to the last 4 digits (L08, decision 1(a)). Open: needs field and scope rulings (DPO/program owner) for the rest |
| F-11 | Medium | No ordering on paginated lists: pages can overlap or skip rows | `routers/beneficiaries.py:96,206` | Fixed for `/GetMatchedChedBene`. Open for `/GetEligibleBene` (ordered query > 30 s; needs DBA index/keyset, migration 0003 proposed) |
| F-12 | Medium | `/GetEligibleBene` first page ~3.5 s; `NOT IN` against an unindexed 81k-row table | measured | Mitigated: 15 s server-side limit. Open: DBA review of 0003 and the view plan |
| F-13 | Medium | No idempotency or uniqueness on CHED posts: retries create duplicate rows (proven with 8 concurrent posts locally) | `routers/beneficiaries.py:118-193` | Open: needs a request key in the contract (backlog P2) |
| F-14 | Medium | `SELECT *` on views; returns any column the DBA adds | `routers/beneficiaries.py:41,206` | Fixed: explicit columns |
| F-15 | Medium | Unbounded inputs; impossible dates silently "Not Found" | `schemas.py` | Fixed: lengths, real-date check, 422 without echo |
| F-16 | Medium | `print()` logging; no request IDs; no audit trail | all routes | Fixed: JSON logs, `X-Request-ID`, redaction, audit events (log-based) |
| F-17 | Medium | No health/readiness; DB outage crashed startup; no timeouts | `main.py` | Fixed: `/healthz`, `/readyz`, connect/read/statement timeouts, pool wait limit |
| F-18 | Low | Python normalization in `generate_md5_hash` used `r'\\s+'` and never collapsed spaces | `utils.py:27-28` | Fixed (only used by `test_fmt.py`) |
| F-19 | Low | `test_fmt.py` contained what looks like a real name and birth date | `test_fmt.py:2` | Fixed: synthetic values |
| F-20 | Low | Disabled user got 400; login worked for disabled users | `security.py:67` | Fixed: 403 `ACCOUNT_DISABLED` at login and on requests |
| F-21 | Info | Vault records the jump-host SSH password as exposed and pending rotation before any staging reconnect (ADR 2026-10-22, gate 2) | vault | Open: this work connected with the current credential at Martin's instruction; rotate or switch to a key |

## 4. ODS guide benchmark (gap assessment)

Guide sections cited as "ODS §n". "Met" means implemented and tested here; "Partial" means some rules apply; "Gap" means not addressed.

| ODS § | Rule | State | Evidence / remaining gap |
|---|---|---|---|
| 5 | Validate env once, fail fast, no insecure prod defaults | Met | `config.py`, `tests/test_config.py` |
| 5 | Secrets in a managed store | Gap | `.env` locally; production secret store not chosen |
| 6.1/6.5 | Short-lived service credentials, scoped abilities, rotation | Partial | 30-min JWT, scopes (`require_scope`), no refresh tokens, no rotation or inventory; HS256 shared key |
| 6.2 | Validate iss/aud/alg/exp; never log tokens | Met | `security.decode_access_token`, redaction tests |
| 6.6/6.7 | Deny-by-default RBAC; object-level scope in the query | Partial | Partner routes deny without scope; legacy routes have no object/region scope (F-10) |
| 7 | PII minimization in responses, logs, caches, fixtures | Partial | Logs redacted, fixtures synthetic; `/GetEligibleBene` still returns full PII; cache holds PII for TTL |
| 8.1 | Versioned API, one OpenAPI | Partial | `/v1/api/tuloy` versioned; legacy routes unversioned; OpenAPI checked by tests and smoke |
| 8.4 | Strict validation, reject unknown fields | Partial | Partner schemas `extra="forbid"`; legacy schemas still ignore unknown fields for compatibility |
| 8.5 | `{status,message,data,meta,error_code,request_id}` envelope | Gap (deliberate) | Errors add `error_code` and `request_id` but keep FastAPI's `detail` so existing clients keep working. Partner success envelope to be fixed in the contract |
| 8.6 | Error registry codes | Partial | Uses `UNAUTHENTICATED`, `FORBIDDEN`, `DB_UNAVAILABLE` instead of `AUTH_INVALID_TOKEN`, `RBAC_FORBIDDEN`, `DEPENDENCY_UNAVAILABLE`. Align when the partner contract is approved |
| 8.7 | Bounded, stable, cursor pagination | Partial | Limits capped; matched list stable; eligible list offset-based and unstable (F-11) |
| 8.8 | `X-Request-Id`, `Cache-Control`, `Retry-After`, `Idempotency-Key` | Met | Middleware; partner writes require `Idempotency-Key` |
| 9 | Parameterized SQL, explicit columns, timeouts, read-only transactions, central DB error mapping | Met | `routers/beneficiaries.py`, `database.py`, injection tests (unit + real MySQL) |
| 9 | Idempotent retryable mutations | Gap | F-13 |
| 9 | Rate limits per user/client before DB work | Partial | Per-IP global + per IP+username login limit, in-process only |
| 11.1 | Least-privilege runtime role; separate migration role | Gap (external) | Enforced in config outside local; DBA must create accounts (F-07) |
| 11.7 | Versioned migrations, separate release step | Met (locally) | `migrations/`, `scripts/migrate.py`; tested on local MySQL; never run remotely |
| 11.8 | Backup, PITR, restore test | Gap (external) | DBA |
| 12 | Security headers, CORS allowlist, trusted hosts, body limit | Met | `main.py`, `tests/test_app.py` |
| 13 | Structured logs, request IDs, redaction | Met | `observability.py` |
| 13 | Metrics, tracing, SLOs, alerts | Gap | Not implemented |
| 13 | Live/ready endpoints, graceful shutdown | Met | `/healthz`, `/readyz`, lifespan closes tunnel/pools; names differ from `/health/live`, `/health/ready` |
| 14 | Authz matrix, invalid/empty/dependency-failure tests | Partial | 96 tests incl. partner 401/403/501 matrix and outage; no positive partner data yet |
| 15 | CI gates (lint, types, audit, scan) | Gap | `pip-audit` run manually (clean); no CI pipeline in this repo |
| 16 | Container baseline | Gap | No Dockerfile yet |
| 19 P0 | Rotate exposed credentials; least-privilege DB; backups | Gap (external) | F-06, F-07, F-21 |
