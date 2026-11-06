> Current status: see NAPA_RELEASE_READINESS.md and NAPA_STAGING_RUNBOOK.md.
> Earlier rehearsal/guard statements below are historical; staging migrations are
> operator-reported complete, production preflight and browser acceptance are now implemented.

# Persistent BCMD / NAPA local candidate

## Implemented

- Transactional batch creation from one or more explicitly selected source record
  IDs, or from criteria and a record count. There is no fixed batch size.
- Frozen member snapshots, per-member validation issues and two durable create/DQA
  audit events committed together. If the audit fails, everything rolls back.
- UUID idempotency keys scoped to the authenticated API user. Identical retries
  return the persisted result; conflicting payloads return 409. Concurrent saves
  with the same key commit one batch, with a safe retry on a losing request.
- Bounded candidate, batch, member, issue and audit pages; batch detail; shared
  batch read locks and rejection of PURGED personal-data reads.
- Any recognized active DSWD/DSWD or TEST/TEST grant identifies users for NAPA/BCMD; no particular module scope is required (Martin's latest ruling).
  Admin sidebar visibility alone does not grant access. CHED/TESDA/TEST bindings
  cannot use these routes. Authentication and grant lookup remain the existing API's.
- Frontend NAPA list/create/detail/validation/audit integration, request cancellation,
  safe errors, retry-key reuse and no browser persistence of member data.

The initial local source/category is CHED/TK only. Current eligibility is checked
against vw_verify_member_bene at create time. Period and Tuloy ID are required;
multiple source/roster projections are rejected rather than arbitrarily selected.
The candidate freezes a versioned local mapping configuration; this is not a claim
that program eligibility or disclosure policy has completed production approval.

## Routes

All are under `/v1/api/tuloy/staff/bcmd` and hidden from the partner OpenAPI schema.

| Method | Path | Behavior |
|---|---|---|
| GET | /status | Feature availability; categories and row limit; export unavailable |
| GET | /candidates | Eligible CHED selection metadata, 50 by default / 200 maximum |
| POST | /batches | Create snapshot; Idempotency-Key UUID header required |
| GET | /batches | Saved batches with validation counts |
| GET | /batches/{uuid} | Batch metadata |
| GET | /batches/{uuid}/members | Frozen member page |
| GET | /batches/{uuid}/issues | Rule codes and affected line/column, without field values |
| GET | /batches/{uuid}/audit | Durable create/DQA event page |
| GET | /batches/{uuid}/export | Always 501; never releases a file |

Read page parameters are limit 1-200 and offset 0-10000. A failed page is not
represented as a successful empty list. Source selection is revalidated at save.

Example create body (synthetic IDs only):

```json
{"name":"Synthetic batch","category":"TK","record_ids":[1,2]}
```

## Mapping decisions

HHID and non-null FamilyID use household_id, with NULL FamilyID for a missing active
association. IP 0=NO; NULL stays NULL; recognized nonzero member IP membership=YES.
No fallback to household IP is made. Sex and suffix use the ODS V3 libraries,
not legacy sex_tbl. Geography uses stored ten-digit PSGC codes. NULL snapshot
values remain NULL; the standalone formatter represents approved NULLs as blank
CSV cells without excluding members. Invalid references remain DQA issues.

Only family_id and ip_indicator have approved default CSV nullability. A missing
suffix/middle name currently produces a validation issue unless another explicit
nullable policy is approved; the local candidate does not silently change it.

## Schema and activation

`docs/schema/bcmd_candidate.sql` has six tables derived from spec 02 Revision 4:
batch, members, issues, export ledger, audit ledger and approvals. It adds nullable
ip_indicator to the member snapshot. There are now **18** personal snapshot fields
listed in bcmd_store.PERSONAL_COLUMNS; future purge/grant logic must include IP.

The source candidate remains under docs/schema; proposed numbered files 0007-0013
are now in migrations/ after Martin supplied the four applied ledger checksums.
All four match. No startup DDL or staging ledger changes were made. Other worktrees, including _bcmd and Keycloak work,
were not modified. See `docs/schema/DBGATE_RELEASE_NOTES.md` before staging work.

BCMD_ENABLED defaults false. Enabling it currently requires APP_ENV=local, a
loopback DB host, no SSH tunnel, a `tuloy_napa_*` schema and distinct source/write
account configuration. The guard intentionally rejects staging activation. This
implementation is a local candidate, not a production-ready feature toggle.

## Validation

Full API suite before the final concurrency case: 894 passed, 16 unrelated existing
integration skips. After final changes: 126 focused tests passed, including eight
real MySQL tests. The real-MySQL tests run only against 127.0.0.1:13498, create a
uniquely named tuloy_napa_test_* database and delete only that database afterward.
All source rows and reference data in this test container are synthetic.

Set NAPA_TEST_CONFIG to a local ignored JSON file with host=127.0.0.1, port=13498,
user/password for the dedicated test container. Then run:

```powershell
$env:NAPA_TEST_CONFIG='.run/napa-test.json'
..\tuloy-api-main\.venv\Scripts\python.exe -m pytest -q
```

The isolated MySQL 8.0 container used in this session is tuloy-napa-candidate;
synthetic credentials are in ignored .run/napa-test.json. The tests use an admin
connection for their fixtures and override authentication with a synthetic DSWD
principal. Separate non-database tests exercise real token/grant authentication
through the existing test fixtures. Production column privileges and real browser
acceptance are not claimed by these tests.

Frontend: full lint, 181 tests and production build passed; the final issue-line
display change also passed the focused NAPA/service tests.

## Still required before real export / deployment

- Staging identity confirmation and reviewed per-file schema verification. Numbered
  migrations 0007-0013 now exist; staging still needs explicit backfill 0003 and 0006
  before those files. See migration_manifest.json for checksums and local order.
- Production account/privilege checks replacing the local-only activation guard.
- Category/eligibility, entitlement and remaining nullable-field contract approval.
- Approval recording, export version/hash validation, download auditing and the
  reviewed purge/retention/recovery flows. Their schema exists, but this candidate
  does not implement them or claim those export gates have passed.
- Synthetic release rehearsal, browser acceptance and explicit push/deployment
  approval. No staging DDL, grants, source updates or real export occurred here.

Migration rehearsal follow-up: nine MySQL tests passed, including reproducing the
reported 0001/0002/0004/0005 ledger gap, applying 0003 via explicit backfill, then
0006-0013 one file at a time, preserving the settings row, schema, counter and
pre-existing member index. A fresh settings table also matches the supplied DDL.


## Operator-reported staging completion (2026-10-30)

Martin supplied the final staging ledger for dswd_4ps_tuloy_dev on
ppisv2-v3Master. All 13 recorded checksums, 0001-0013, match the current local
migration files. Evidence is docs/schema/staging-ledger-verified-20261030.json
(relative to the API repository). This confirms the supplied migration history;
it does not independently verify current table contents or deploy/enable code.
No additional schema apply is requested. Historical pending-migration statements
above describe the pre-apply state. Application release limitations remain.
