# NAPA release readiness ? 2026-10-30

## Current outcome

The API and web release candidates are implemented locally. Nothing is pushed or
deployed. The database milestone is operator-reported complete: Martin supplied
all 13 migration ledger entries for dswd_4ps_tuloy_dev on ppisv2-v3Master; canonical
text checksums match. No migrations or ledger entries are changed by this release.

## Access and policy

- Martin's latest ruling: all signed-in DSWD and TEST users can use NAPA/BCMD, without a
  particular module scope. Identity is established by any recognized, active grant
  bound to matching agency/partner DSWD/DSWD or TEST/TEST. Username and sidebar visibility are
  not affiliation evidence. Unknown affiliation, no active grants, CHED and TESDA are denied. Revocation and expiry are checked on every request.
- This changes NAPA/BCMD access only; it does not disable authentication or change
  the payment, beneficiary, member-verification or other API guards.
- TK is CHED, ENROLLED, first year, semester 1. The same SQL predicate runs for
  candidate discovery and write-time eligibility. Middle name and suffix may be blank.
- FamilyID equals household_id when an association exists, otherwise NULL. IP 0
  is NO; NULL stays NULL. No household fallback or fabricated identifiers.
- No automatic retention deadline or scheduled purge. Each batch freezes the
  policy and its hash. Explicit manual purge is available separately; this is
  Martin's policy decision, not a claim of independent DPO approval.
- Deduplication is within one batch, not proof of annual or cross-batch payment eligibility.

## Implemented safeguards

The reviewed non-local staging database defaults to NAPA enabled, uses the pinned
server/database identity, and reuses existing runtime/write credentials without
new env flags. Local and other database profiles remain off by default. Explicit
BCMD_ENABLED=false is preserved for operational rollback.
BCMD has a lazy separate pool, even when the underlying account is shared.
Each checkout verifies actual identity, required rights, the complete migration
ledger/checksums, seven table definitions and nullable grant expiry. Existing-account
mode accepts the account's existing extra privileges without adding or revoking any.
Strict dedicated-account mode remains available but is not required for this rollout. No success cache hides privilege revocation or schema drift.
The application performs no startup DDL or grant changes.

The operator purge tool defaults to dry-run and requires explicit matching batch
confirmation to apply. It loads no application .env or runtime credentials.
It locks the batch, refuses STARTED exports, clears all 18 private snapshot columns,
marks PURGED and appends its audit atomically. Readers take a shared batch lock;
post-purge personal-data reads return 410. This does not delete source records,
retained linked identifiers, existing audit history or backups. There are no generated
CSV artifacts in this release. Any future export implementation must purge its own artifacts.

## Verification

- Real local MySQL: transactional creation, snapshot reload, null mappings,
  idempotency, concurrent retries, audit rollback, purge/retry and interrupted-migration
  recovery are covered. A genuine least-privilege account passed production-mode
  preflight; wrong target, extra approval privilege and altered ledger were denied.
- Browser acceptance uses the project's Chromium runner against the actual local
  API and MySQL. Password verification, JWT, /staff/me and BCMD requests are real.
  Only unrelated dashboard preload reads are stubbed. DSWD and TEST are admitted without a CHED-read grant and can create/review batches.
  DSWD reopens its saved batch after re-login; TEST audit attribution remains TEST.
  CHED is denied with 403 while remaining signed in. CSV stays disabled.
- Production build and lint pass; frontend suite: 181 tests. The existing long BCMD
  interaction test has a scoped 15-second budget; its assertions are unchanged.
- Final API regression: 941 passed, 17 skipped (the optional browser harness and
  16 unrelated integration cases). The browser harness was run separately: all
  three Chromium journeys passed. Commit IDs are reported with delivery.
  Existing image-reference build warnings and the 401-session React warning remain.

## Staging account requirement resolved

Martin instructed reuse of his existing login. No new account or grant changes
are needed. The existing-account read-only preflight passed on staging; evidence:
`schema/staging-napa-existing-account-20261030.json`. The updated runbook uses reviewed code defaults, so no new server flags or sysadmin
change is needed for this target. Verify the deployed process after the pipeline.
Earlier dedicated-account instructions and the earlier blocker below are historical,
superseded by this explicit decision. No real batches were created or purged here.

## CSV release remains separate

Export returns 501 and the browser download control is disabled. The five approval
gates, export sequencing/two stored hashes, audited download admission/result,
recovery/reaper and artifact lifecycle must be completed before enabling CSV.
DQA=CLEAN alone is not export approval. No NAPA operation releases a payment.


## Independent staging verification ? 2026-10-30 08:44 Asia/Shanghai

The authorized configured staging connection is reachable. A READ ONLY transaction
independently confirmed dswd_4ps_tuloy_dev on ppisv2-v3Master, all thirteen ledger
names/checksums, all seven expected table definitions and nullable grant expiry.
The transaction was rolled back; no account, privilege, migration, grant or batch
was changed. Evidence: schema/staging-napa-readonly-20261030.json.

The configured administrative/migration login has neither CREATE USER nor GRANT
OPTION. BCMD credentials are not configured. Account provisioning therefore remains
blocked on a DBA login with those privileges. No attempt to escalate or modify that
login was made. The DBA must confirm the API's actual MySQL source-host binding;
the developer tunnel's observed host is not proof of the deployed API's route.
The dedicated-account preflight and application activation remain NOT PASSED.


## Existing-account override ? 2026-10-30 08:50 Asia/Shanghai

Martin: do not create another staging database user; reuse his configured account.
This mode is implemented and its live read-only preflight passed: target identity,
all migrations, table definitions and required permissions. Existing privileges were
not changed. The previous CREATE USER/GRANT OPTION blocker no longer applies.
No staging rows or users were changed, and no credentials were printed or committed.


## Code-default deployment authorization ? 2026-10-30

Martin authorized removing the need to set BCMD_USE_EXISTING_ACCOUNT and pushing
both repos to main. Existing-account reuse is now the default. Only the reviewed
non-local target gets automatic activation and its pinned identity defaults.
Credentials are still read from existing configuration; none are hardcoded.
