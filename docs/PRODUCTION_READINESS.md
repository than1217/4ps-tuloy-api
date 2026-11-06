# Production readiness: tuloy-api-main as the Partner API host

Date: 2026-10-26. Decision: vault ADR `40-Decisions/2026-10-26-tuloy-api-main-partner-api-host.md` (this repo hosts the Partner API; supersedes Option C of ADR 2026-10-19).

**Verdict: not production ready.** The service starts, connects read-only through the bastion, and passes its local and smoke checks. The six Partner operations authenticate and authorize, then return `501`: their business rules, mappings and storage are not approved yet. The blockers are in §4.

## 1. What was verified, and how

| Check | Result | Evidence |
|---|---|---|
| Unit tests (fake DB, no network) | 105 passed | `.\scripts\test.ps1` |
| Integration tests, throwaway local MySQL 8 on `127.0.0.1:3407` | 6 passed | READ ONLY session blocks writes (errno 1792); runtime account can't run DDL (1142); verify matching and injection resistance; DB down gives `503`; 8 concurrent CHED posts make 8 rows (confirms the non-idempotency finding) |
| Migration runner on the local DB | applied 0001–0003; refuses a remote target without `--allow-remote` (exit 3); refuses an edited applied file (exit 4) | `scripts/migrate.py` |
| Staged connection check against staging (read-only) | stages a–f pass | `.\scripts\check-connection.ps1`: TCP, pinned ed25519 host key, tunnel on `127.0.0.1`, MySQL 8.0.43 auth, `SELECT 1` in a READ ONLY session, required objects present |
| Live smoke through the tunnel | 5 pass, 1 skip | healthz, readyz `{database: up, tunnel: up}`, OpenAPI shows the six partner operations, unauthenticated `401`, DB-backed auth lookup `401` for a synthetic subject. **Skipped: authenticated read**, because no test account exists |
| Listeners | `127.0.0.1` only (API port and tunnel) | `Get-NetTCPConnection` during the run |
| Logs from the live run | no passwords, tokens, DB/bastion addresses or concrete paths | grep of `.run\api.log` |
| Dependencies | no known vulnerabilities | `pip-audit` on the hashed `requirements.lock` |

Remote writes, all approved by Martin as DBA and all on `dswd_4ps_tuloy_dev`:
- 2026-10-26: the `tuloy_admin` API account (id 5).
- 2026-10-26 08:34: migrations 0001 (no-op) and 0002. These created `api_user_scopes` and `schema_migrations`; the definitions of the existing tables were compared before and after and did not change.
- Six 7-day test grants for `tuloy_admin`.

Post-apply smoke test: 9 pass, 1 skip. Verify is allowed for the granted account; the bulk feeds are denied (403); the partner routes answer 501. No DB-level GRANT ran, and 0003 is still pending.

## 2. Done in this repo

- Config: typed, fails fast. `APP_ENV` required. Outside local/dev it refuses: missing or short `SECRET_KEY`, signup on, `CORS_ORIGINS=*`, `TRUSTED_HOSTS` empty or `*`, non-TLS `REDIS_URL`, migration user equal to runtime user. `DB_SSL_CA` is optional in every environment; leaving it empty uses the connector defaults without certificate verification.
- Database: separate read-only pool (READ ONLY sessions plus `max_execution_time`) and write pool (optional separate `DB_RW_USER`). Bounded pools; `503 DB_BUSY` / `DB_UNAVAILABLE` / `DB_TIMEOUT` with `Retry-After`. Stale connections are rebuilt once, with a fresh tunnel. After a failed connect, requests fail fast for 5 s instead of queueing behind another SSH or MySQL timeout. No DDL at startup.
- Tunnel: `127.0.0.1:0`, pinned host key mandatory, agent and stray keys disabled.
- Auth: JWT with iss/aud/iat/nbf/exp/jti; constant-time dummy hash; scope checks deny by default; signup off by default. The login throttle runs before a DB connection is taken.
- HTTP: request IDs, 1 MB body limit (Content-Length), per-IP limit, security headers, TrustedHost, CORS off by default. Unhandled errors return the standard body with the request ID.
- Logging: JSON, secret and identifier redaction, route templates instead of concrete paths, exception type and frames without messages. Audit events for logins (pseudonymised user, IP), throttling, scope denials, and data access.
- Partner API: six operations under `/v1/api/tuloy`, scopes from the approved matrix, `Idempotency-Key` (UUID) required on writes, contract in `docs/PARTNER_API_CONTRACT.md`.
- Migrations: ledger with checksums, named lock, `lock_wait_timeout`, prints the target server identity before the confirmation prompt.
- Scripts: staged connection check, smoke test (mints tokens only for loopback targets), start (fails if `/healthz` never answers), stop (checks that the PID is this project's Python), CSV import (credentials from env, retries only `429`, prints no identifiers).
- Repo hygiene: hard-coded import credentials removed; real-looking test values replaced with synthetic ones; `*.csv`, `.env*` and `.run/` ignored.

## 3. Proposed, not applied anywhere remote

| Item | File / place | Needs |
|---|---|---|
| `api_user_scopes` grant table (RBAC ADR 2026-10-26) | `migrations/0002_api_user_scopes.sql` | **Applied to dev 2026-10-26** after a local rehearsal (apply, grants, rollback, re-apply); see `docs/RBAC.md` |
| `api_user_scopes.expires_at` NULLable, CHECK `expires_at IS NULL OR expires_at > granted_at` (ADR amendment 2026-10-26) | `migrations/0006_api_user_scopes_nullable_expiry.sql`; rollback `migrations/rollback/0006_..._down.sql` | **Local rehearsal done (apply, rollback, re-apply).** Not on dev yet: needs Martin's go-ahead |
| Index on `enroll_matched_CHED_tbl.tuloy_id` (`ALGORITHM=INPLACE, LOCK=NONE`) | `migrations/0003_...sql` | DBA review; decide on UNIQUE after dedupe |
| Least-privilege accounts | below | DBA |

Recommended accounts (replace the current runtime grant: `SELECT ON *.*` plus `ALL` on two schemas):

```sql
-- runtime, read pool
GRANT SELECT ON <db>.vw_verify_member_bene, <db>.vw_get_matched_CHED, <db>.enroll_matched_CHED_tbl,
             <db>.api_users, <db>.api_user_scopes TO 'tuloy_api_ro'@'<app host>';
-- runtime, write pool (DB_RW_USER); SELECT on api_user_scopes for the CHED write's grant re-check
GRANT SELECT, INSERT ON <db>.api_users, <db>.matching_transaction_CHED_tbl, <db>.enroll_matched_CHED_tbl
      TO 'tuloy_api_rw'@'<app host>';
GRANT SELECT ON <db>.api_user_scopes TO 'tuloy_api_rw'@'<app host>';
-- grant administration (scripts/grants.py): never the runtime accounts
GRANT SELECT, INSERT, UPDATE (revoked_at, revoked_by) ON <db>.api_user_scopes TO 'tuloy_migrator'@'<admin host>';
GRANT SELECT ON <db>.api_users TO 'tuloy_migrator'@'<admin host>';
-- migrations only (MIGRATION_DB_USER); no DROP
GRANT CREATE, ALTER, INDEX, REFERENCES, SELECT, INSERT ON <db>.<each app table> TO 'tuloy_migrator'@'<admin host>';
GRANT CREATE, SELECT, INSERT ON <db>.schema_migrations TO 'tuloy_migrator'@'<admin host>';
ALTER USER 'tuloy_api_ro'@'<app host>', 'tuloy_api_rw'@'<app host>' REQUIRE SSL WITH MAX_USER_CONNECTIONS 20;
-- after cutover: REVOKE the old broad grants from the current shared account
```

## 4. Blockers before production

1. **Partner business rules not approved.** Enrollment rules, event types, matching logic, data mappings and where application-owned enrollment/event data lives (not in ODS without a decision). Each operation stays `501` until its contract section is approved. (`members/verify` is already wired to the existing beneficiary lookup in this tree. `PARTNER_API_ENABLED=true` is the dev setting from 2026-10-26: it mounts all six operations and lists them on `/tuloy/docs`; the other five answer `501`.)
2. **No test account** for the authenticated read smoke check. Owner: provision one with the minimum scope and give it to the operator out of band.
3. **Shared credentials.** The bastion password was exposed and is pending rotation. The runtime DB account is over-privileged (§3). Check `api_users` for an `admin` account with the old default password from the import scripts and disable it.
4. **Legacy-route data boundaries.** The routes are now gated by scope (fixed, see §5). The ODS view and the CHED tables have no partner or agency column, though, so a grant can't limit which rows a caller sees or writes. The policy covers this for now: the bulk feeds stay ungranted, and verify and CHED-write grants go only to named accounts. The DPO and the data owner must define permitted populations before any bulk grant.
5. **`PostEnrolleeMatchedCHED` is not idempotent**; `tuloy_id` can be NULL; `message` is free text. Needs a dedupe rule, then a UNIQUE key, and a message whitelist.
6. **Grant model**: partner ID, revocation and whole-tuple grants are done (§5). Grant expiry is optional since migration 0006 (NULL = non-expiring; ends only by revocation or by disabling the account); there is still no account expiry, and the login token stays 30 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES=30`). Non-expiring grants need a periodic review. Still open: enforcing the tuple against record ownership when each partner operation is implemented, revoking individual tokens (disabling the account is the cutoff today), and least-privilege DB accounts on dev (the shared account has ALL).
7. **Infrastructure outside this repo**: container image and CI with the lock file; Nginx/ALB health check on `/healthz` with an allowed Host header and a deregistration delay; secrets in a store, not `.env`; a direct private network path to MySQL with TLS instead of the bastion tunnel; Redis with TLS and auth; backups and a restore rehearsal; log shipping and alerts; a deploy runbook.

## 5. Review findings and dispositions

Four in-repo reviewers (code, security, database, DevOps) reviewed the hardened branch. The external council was not convened.

| Finding | Disposition |
|---|---|
| Requests pile up behind blocking connects during a DB/tunnel outage | Fixed: 5 s cooldown, pools closed and rebuilt, tunnel restarted |
| Write pool shares the read account | Fixed: optional `DB_RW_USER` |
| Certificate verification for a direct DB connection | Optional: set `DB_SSL_CA` to verify the server certificate; empty uses connector defaults without verification |
| `APP_ENV` defaulted to local | Fixed: required |
| Wildcard trusted hosts / plain Redis allowed in prod | Fixed: refused outside local/dev |
| 500 responses missing request ID | Fixed in middleware |
| Concrete paths (with IDs) in logs | Fixed: route templates; more identifier keys redacted |
| Login throttle after DB checkout; plain usernames in limiter memory; unbounded limiter | Fixed |
| No audit for failed logins and scope denials | Fixed |
| `tuloy_id` numeric in some rows | Fixed: `str` or `int` |
| Migration runner: no lock, unbounded metadata wait, partial status, no target identity | Fixed |
| Smoke test could mint tokens for a remote URL; start/stop scripts unsafe | Fixed |
| No log level setting | Fixed: `LOG_LEVEL` |
| Per-IP limit behind a proxy / breaks CSV import | Documented (`.env.example`); import retries `429` |
| Legacy routes lack scopes (High, F-10) | **Fixed** (RBAC ADR 2026-10-26): each route requires its `tuloy:legacy:*` scope and denies by default. Row-level boundaries remain open, blocker 4 |
| CHED post not idempotent | Open, blocker 5 |
| Cache not invalidated on write | Open: TTL bounds staleness (`CACHE_EXPIRE_SECONDS`) |
| Chunked bodies bypass the size limit | Open: enforce `client_max_body_size` at Nginx |
| Alembic (other repo) and this runner on the same schema | Open: split ownership per table before either runs remotely |
| Eligible list pagination unstable (no ORDER BY; ORDER BY times out) | Open: needs the 0003 index or a keyset on an indexed column |
| Grant model lacks partner ID, expiry, revocation | **Fixed in code and migration 0002**, then amended by 0006 (numbered 0004 until 2026-10-29) (expiry optional, Martin 2026-10-26): (scope, partner, agency) tuples, revocation from the next request, CHED write re-check, append-only history. Tuple-to-record enforcement is open, blocker 6 |
| Co-lead review (ChatGPT, 2026-10-26): grant uniqueness key could collide | **Fixed**: composite UNIQUE over the real columns |
| Sex codes differ across sources (reference 1=M/2=F, CHED 0=M/1=F, TESDA 0=F/1=M) | Open: needs a data-owner decision before any mapping |

## 6. Rollback

Remote (dev), in this order:
1. Revoke grants: `python -m scripts.grants revoke --allow-remote --grant-id <n> --by <who>` (keeps history).
2. Only if the table itself must go, export it, then run `migrations/rollback/0002_api_user_scopes_down.sql` with the admin account. Every scoped route then denies, because a missing table means no grants. Never restore token-only legacy access.
3. Migration 0006 (non-expiring grants): first revoke or date every grant with a NULL `expires_at` (`UPDATE api_user_scopes SET expires_at = <date> WHERE expires_at IS NULL`, an audited admin edit; export the table first), then run `migrations/rollback/0006_api_user_scopes_nullable_expiry_down.sql` statement by statement. Its first statement fails (error 3819) while any NULL remains, which protects those rows from being rewritten silently. Roll the API code back with it, or the old predicate will read the restored column as before.
4. Verification accounts: `UPDATE api_users SET disabled = 1 WHERE username IN ('tuloy-tesda01','tuloy-ched01','tuloy-dswd01')` (denies them on the next request, keeps history), or revoke their grants with `scripts.grants revoke`.
5. Partner stubs on dev: set `PARTNER_API_ENABLED=false` and restart. This unmounts all six `/v1/api/tuloy` operations and removes them from `/tuloy/docs`. `GET /v1/api/tuloy/staff/me` stays.
6. Remove the test account if it's no longer wanted: `UPDATE api_users SET disabled = 1 WHERE id = 5` (keeps grant history), or delete its grants and then the row.

Local:

- Code: `git reset --hard a4cb938` (baseline before this work), or restore from `..\_backup-tuloy-api-main-20261026` (includes the original `.env`; local only, never share it).
- Local throwaway MySQL: stop the process and delete its data directory.
- `.env`: the only change was appending `APP_ENV=local`.
