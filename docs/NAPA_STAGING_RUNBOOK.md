# NAPA staging release and rollback

Target: `dswd_4ps_tuloy_dev` on `ppisv2-v3Master`. It contains real data.
This runbook does not authorize a push, deployment, database account change or
real-data batch creation by itself. The local application work is ready for review.

## Current verification and account decision

Martin explicitly instructed reuse of his existing staging account. Do not create
another database user, change its grants or run the earlier DBA grant template.
The new-account requirement is withdrawn for this release.

Read-only verification on 2026-10-30 using the existing configured runtime credentials
passed identity, all thirteen migrations, all seven table definitions, nullable
expiry and the required SELECT/INSERT permissions. Evidence:
`schema/staging-napa-existing-account-20261030.json`. There were zero database writes
and zero new accounts. This was through the configured staging tunnel; still verify
the deployed API's actual process environment before enabling it.

## 1. Reuse the existing API database login

Keep the existing DB_USER/DB_PASSWORD (or DB_RW_USER/DB_RW_PASSWORD, if the API already
uses a separate writer). No new database user, password, grant or frontend secret is
needed. The explicit existing-account mode reuses those credentials when BCMD_DB_USER
and BCMD_DB_PASSWORD are unset. Leave both BCMD-specific credential fields unset to
avoid retaining an obsolete dedicated-account configuration.

The mode checks required privileges rather than demanding a least-privilege account.
Extra existing rights remain unchanged; this is Martin's explicit account-reuse decision.
Active roles and partial-revoke configurations remain unsupported and fail closed.
Database identity, current user, schema, ledger and per-request affiliation checks remain.

## 2. No new server configuration is required for the reviewed target

Martin authorized code defaults and push-to-main deployment without a sysadmin.
The reviewed non-local database dswd_4ps_tuloy_dev defaults to NAPA enabled, the
expected hostname ppisv2-v3Master, and reuse of existing DB_USER/DB_PASSWORD (or
DB_RW_USER/DB_RW_PASSWORD). No credentials are hardcoded in source.
BCMD_USE_EXISTING_ACCOUNT=true is not required; existing-account mode is the default.
Local mode and other databases do not auto-enable the feature. Per-request checks
still verify actual server/database identity, schema, migrations and required rights.
An existing explicit BCMD_ENABLED=false remains an operational rollback override.

Do not copy credentials into scripts, chat or frontend variables. No migrations,
accounts or grants are created or changed by deployment. BCMD retains its separate
pool even when it uses the same account, isolating connection failures.

## 3. Read-only preflight on the actual API host

With the candidate code and its intended staging environment loaded:

```bash
python -m scripts.napa_preflight
```

This checks using the existing configured account even while BCMD_ENABLED=false. PASS means
identity, current grants, ledger and table definitions matched. STOP means keep
the feature off and have the DBA inspect the mismatch. It changes no database rows,
privileges or environment flags and prints no secrets. Run again after any privilege,
schema, connection or deployment change. The API repeats these checks on checkout.

## 4. Paired release, after approval of exact commits

Use the API branch `feat/napa-detail-format` and web branch
`feat/agency-aware-titles`, both with explicit push destinations. Never use bare
`git push` from a branch whose upstream may be main. Main pushes trigger staging
pipelines. The delivery response supplies the exact local commit IDs for review;
neither main has been updated by this work.

Deploy the API first through its main pipeline and verify health plus the protected
NAPA route. The reviewed target profile activates without an env edit. Deploy the
frontend after the API pipeline passes, then run live acceptance. No nginx change is
required if the existing `/v1/api/tuloy/` prefix proxy is deployed.

## 5. Live acceptance

- Sign in with approved DSWD and TEST accounts. Any active recognized DSWD/DSWD
  or TEST/TEST grant is
  sufficient; a CHED-read scope is not required for NAPA/BCMD.
- Check `/v1/api/tuloy/staff/bcmd/status`: available=true, export_available=false.
- Confirm candidates are CHED, ENROLLED, first year, semester 1. Read-only checks
  come first; create a real-data batch only under the program's approved test procedure.
- When authorized, create a small named batch, inspect frozen members, validation
  and audit, then sign out/in and reopen it. Verify permitted null values remain null.
- Confirm CHED/TESDA, unknown affiliation and expired/revoked DSWD/TEST grants receive
  403 and disclose no batch data. A denial must not log the user out.
- CSV stays disabled/501. No payment status or payment execution should change.

The local browser evidence proves the synthetic flow, not these live results.

## Rollback

First set BCMD_ENABLED=false and restart the API; status becomes unavailable and
batch operations stop. This preserves existing snapshots and audit history. If
needed, roll back the web/API code with the normal approved deployment procedure.
Do not drop tables, rewrite the ledger, revoke unrelated grants or purge snapshots
as a release rollback. Do not disable the shared database login as a NAPA rollback: sign-in and other
modules use it too. No account or beneficiary-grant changes are required.

## Manual purge, only when explicitly requested

No scheduled retention period exists. Do not run purge merely to complete rollout.
`python -m scripts.purge_napa_batch --help` describes the separate operator tool.
It uses BCMD_PURGE_USER/BCMD_PURGE_PASSWORD from process environment, not application
.env files. Its operator needs limited snapshot UPDATE, batch status/purge-column
UPDATE, batch/export/member SELECT and audit INSERT; those rights must never be
added to the runtime login. Review/provision that operator separately before use.
Dry-run is default; applying requires the same batch UUID twice and exact target
identity. STARTED exports block purge. Source rows, retained identifiers and backups
are not erased by this snapshot-PII clearing operation.
