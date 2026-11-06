# Access control (RBAC)

Decision: vault ADR `40-Decisions/2026-10-26-tuloy-api-main-rbac-direct-scope-grants.md`, amended by `40-Decisions/2026-10-26-tuloy-api-main-rbac-non-expiring-grants.md` (grant expiry is now optional).

## Three kinds of expiry (keep them apart)

| Kind | Where | Value |
|---|---|---|
| Login token lifetime | `ACCESS_TOKEN_EXPIRE_MINUTES` | **30 minutes** (the default; `POST /token` returns `expires_in: 1800`). Not changed by non-expiring grants |
| Account expiry | none | **Does not exist.** `api_users` has only `disabled`. Disabling the account is the cutoff for an account |
| Grant expiry | `api_user_scopes.expires_at` | Dated, or **NULL = non-expiring** (migration 0006). A non-expiring grant ends only by revocation |

## How a request is authorised

### Payment API exception (DSWD and TEST)

Every `/api/payments/*` operation, including rates, computation, generation and exports,
requires a valid Bearer token for an enabled `api_users` account. The account must have
at least one active, recognized scope grant bound to **DSWD/DSWD** or **TEST/TEST**
(`partner_id` and `agency` must both match). No additional payment scope is required.
This policy uses existing grant bindings; usernames and token agency claims do not establish
eligibility. No grants are created or changed by this check.

Missing/invalid/expired credentials return 401. Disabled accounts, no eligible grants,
CHED/TESDA-only accounts, mismatched bindings, and expired/revoked grants are denied with
403. Account and grant state are reloaded on every request; a grant-store failure fails
closed with 503. Existing policies for non-payment APIs are unchanged, including DSWD-only
legacy feeds. Access decisions emit `authz.payments` events without beneficiary data.

TEST access is explicitly allowed by this payment policy and is not restricted to synthetic
data by the authorization check. Do not treat a TEST-bound account as a sandbox account.

### Scope-based routes

1. `POST /token` with a username and password (OAuth2 form) returns a Bearer JWT, valid for `ACCESS_TOKEN_EXPIRE_MINUTES`.
2. On every request the API reloads the account (`api_users`) and its **active grants** (`api_user_scopes`). A grant is active when `revoked_at` is NULL and (`expires_at` is NULL or `expires_at` is in the future), judged by the database clock. An account with `disabled = 1` is denied whatever its grants are.
3. Each protected route needs one scope. With no active grant for that scope, the API returns `403 FORBIDDEN` and writes an `authz.scope denied` audit event.

Scopes in the JWT give no access. A grant whose scope the API doesn't know, or which lacks a partner or agency, is ignored. If the grant table is missing, nobody holds any scope. If the grant store fails, the API returns `503`, never a pass.

## Scopes per route

| Route | Scope | Granted today |
|---|---|---|
| `POST /VerifyMemberBene` | `tuloy:legacy:member:verify` | named **DSWD** accounts only (data-owner rulings) |
| `GET /GetEligibleBene` | `tuloy:legacy:beneficiary:read` | **nobody**. Bulk feed; DSWD staff only when granted |
| `POST /PostEnrolleeMatchedCHED` | `tuloy:legacy:ched:write` | nobody until the importer account is named; **CHED or DSWD** only |
| `GET /GetMatchedChedBene` | `tuloy:legacy:ched:read` | **nobody**. Bulk feed; DSWD staff only when granted |
| `POST /v1/api/tuloy/members/verify` | `tuloy:member:verify` | named accounts only; legacy-compatible lookup. Mounted when `PARTNER_API_ENABLED=true` (on for dev) |
| `POST /v1/api/tuloy/enrollments` | `tuloy:enrollment:submit` | 〃 |
| `GET /v1/api/tuloy/enrollments`, `.../{id}` | `tuloy:enrollment:read` | 〃 |
| `POST /v1/api/tuloy/enrollments/{id}/events` | `tuloy:enrollment:update` | 〃 |
| `GET /v1/api/tuloy/members/{tuloy_id}/enrollment-history` | `tuloy:history:read` | 〃 |
| `GET /tuloydswd/openapi.json` (internal docs at `/tuloydswd/docs`) | `tuloy:docs:internal` | DSWD staff who need the internal reference; opens no API route. The partner docs at `/tuloy/docs` are public |
| *(no route)* staff web app: every module visible | `tuloy:staff:admin` | **DSWD** only; `tuloy_admin` on staging. Opens no API route: each module's own call still needs its scope (member check needs `tuloy:legacy:member:verify`) |

`/healthz`, `/readyz` and `/token` need no token.

**Agency per scope (data-owner rulings, ADR 2026-10-27-tuloy-data-owner-access-rulings).** DSWD administers the API and the data; CHED and TESDA use only the partner API. `SCOPE_AGENCIES` in `scopes.py` narrows four scopes: `tuloy:legacy:member:verify`, `tuloy:legacy:beneficiary:read` and `tuloy:legacy:ched:read` are DSWD only, and `tuloy:legacy:ched:write` is CHED or DSWD. `scripts.grants` refuses any other agency for them. At runtime, a grant bound to another agency authorises nothing: the route returns `403 FORBIDDEN` and the `authz.scope` denial event carries `reason=agency_not_permitted`. `GET /v1/api/tuloy/staff/me` lists only grants that authorise, as `scope`, `agency` and `expires_at`, with the username; it shows no internal user ID and no `partner_id`. Grants already in the database are not rewritten: list the non-DSWD ones for these scopes and revoke or re-bind them.

## Revocation

- **Revoking a grant** takes effect from the next request. The CHED write re-reads its grant as the first statement of its transaction. A revocation committed before that read blocks the write; one committed later doesn't cancel the write already in progress.
- **Revoking one token** isn't possible. Disabling the account (`api_users.disabled = 1`) blocks all its tokens on the next request.

## Managing grants

Use `scripts/grants.py` with the admin account (`MIGRATION_DB_USER`). The runtime accounts can only read the table.

```powershell
.\.venv\Scripts\python.exe -m scripts.grants list   --user <username>
.\.venv\Scripts\python.exe -m scripts.grants grant  --user <username> --scope tuloy:legacy:member:verify `
    --partner <agency> --agency <agency> --days 30 --by <who approved>
.\.venv\Scripts\python.exe -m scripts.grants revoke --grant-id <n> --by <who>
```

- Expiry is explicit: pass `--days` (1 to 365) or `--no-expiry`. The two are mutually exclusive and neither is a default, so a non-expiring grant is always a deliberate choice. `GET /v1/api/tuloy/staff/me` reports `expires_at: null` for such a grant.
- Revoke a non-expiring grant by id when the person leaves or the purpose ends: it is the only way it stops. Review them periodically with `list`.
- Each (user, scope, partner, agency) can have only one active grant. A regrant adds a new row, and the old rows stay as history.
- A remote target needs `--allow-remote` and typing the database name.

## Impact notice: legacy route callers

**From the release of the RBAC change, any valid token is no longer enough for the four legacy routes.** Callers without a grant get `403 FORBIDDEN`.

| Caller | Route | What it needs |
|---|---|---|
| CHED CSV importer (`others/import_ched_csv.py`) | `POST /PostEnrolleeMatchedCHED` | Name the account it uses; grant `tuloy:legacy:ched:write` to that account only |
| tuloy-web-app | `GET /GetEligibleBene` | Stays denied until the DPO / data owner approve the consumer, purpose, population and fields of the bulk feed |
| Any other caller | any legacy route | Ask for a grant with a stated purpose; none is granted by default |

The importer doesn't retry 403. A run without a grant fails on every row and writes nothing.


## National verification accounts (user direction, 2026-10-26)

Multiple named staff accounts may belong to each of CHED, TESDA and DSWD. Provision each account separately with its own user_id, partner binding, expiry and verification grant. Agency identifies the caller's organisation; it does not select a beneficiary population. Verification searches all regions nationwide. Do not add a mandatory region filter or infer a region from agency. This policy applies only to member verification, not enrollment ownership or bulk feeds.

For every scope, the grant command accepts only CHED, TESDA or DSWD as agency, with partner equal to agency (Martin, 2026-10-27), normalizing case and surrounding whitespace; any other binding is rejected before the database is touched. The list is `APPROVED_AGENCIES` in `scopes.py`. Existing grants are not automatically rewritten or revoked. No schema migration is needed: agency is already stored on api_user_scopes. The profile displays verification agencies and national coverage from active verification grants.

Example for one EXISTING named staff account (repeat for each account, replacing placeholders):

```powershell
.\.venv\Scripts\python.exe -m scripts.grants grant --user <username> --scope tuloy:legacy:member:verify --partner DSWD --agency DSWD --days <duration> --by <operator>
```

Use `tuloy:member:verify` instead for the partner verification endpoint; grant both separately only if that account needs both routes. Neither verification grant permits enrollment reads or writes. Keep the existing remote-change safeguards. No grants were applied by this implementation; usernames, partner bindings and expiry must be supplied for actual account changes. Signup alone does not grant access.

## Provisioned verification accounts (2026-10-26)

`scripts/provision_verification.py` creates `tuloy-tesda01` (TESDA), `tuloy-ched01` (CHED) and `tuloy-dswd01` (DSWD) with the admin account, the same way as `tuloy_admin`. Each gets `tuloy:member:verify`, and `tuloy-dswd01` also `tuloy:legacy:member:verify` (DSWD only since the data-owner rulings), with `partner_id` and `agency` both set to the agency code. Nationwide means no regional restriction: the model has none, and a wildcard partner or agency is never created. An existing username is skipped, never overwritten or granted anything. Each new account gets its own random password, written only to the git-ignored `.run/` directory and never printed or logged; hand it over out of band and delete the file.

```powershell
.\.venv\Scripts\python.exe -m scripts.provision_verification                                  # preview
.\.venv\Scripts\python.exe -m scripts.provision_verification --apply --no-expiry --by Martin   # non-expiring grants
.\.venv\Scripts\python.exe -m scripts.provision_verification --apply --days 30 --by Martin     # dated grants
```
