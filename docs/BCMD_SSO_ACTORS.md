# BCMD / NAPA SSO creator support

This change lets authenticated DSWD/TEST SSO staff create batches and perform
approval-gated exports/downloads using their real `staff_users.id`. Native users
continue to use `api_users.id`. No username/email matching, shadow native user,
token changes, permission bypass or account creation is involved.

## Schema and application contract

New migrations follow the existing 0013 ledger. Applied 0001-0013 files and
checksums are unchanged. Each new version is one atomic MySQL ALTER:

| Version | Table | Change |
|---|---|---|
| 0014 | bcmd_batch | Nullable native creator, separate staff FK, exactly one creator, staff retry-key uniqueness |
| 0015 | bcmd_batch_export | Same identity split; export retry keys include batch and creator namespace |
| 0016 | bcmd_audit_event | Separate staff actor FK; native/staff cannot both be set; labelled system events remain valid |

Existing native rows retain their IDs and attribution. Native ID 11 and staff
ID 11 remain different actors, including when they reuse the same retry UUID.
Create, export and download audits use the matching actor FK. Both identities
still require current DSWD/DSWD or TEST/TEST affiliation. CHED/TESDA are denied.
Batch eligibility, DQA, export approvals, integrity checks and payroll separation
remain enforced. Batch creation does not export data or save payments.

`/status` advertises creation for either supported identity only after readiness
passes. The current web app already uses that response to enable Save; no frontend
permission workaround or nginx change is needed.

## Staging execution: 2026-10-31, Asia/Shanghai

At Martin's explicit request to apply using his existing staging connection,
0014, 0015 and 0016 were applied and each returned VERIFIED. The exact target was
`dswd_4ps_tuloy_dev` on `ppisv2-v3Master` (MySQL 8.0.43). Preflight matched every
0001-0013 name/checksum and all three prior table shapes. Postflight matched the
complete 0001-0016 ledger and all three new table shapes.

Prior data was compared column-for-column after migration: the existing one batch,
zero exports and two audit rows were unchanged. No beneficiary selection, batch
creation, export, approval or payment write was performed. Existing ledger rows
were preserved; only the three verified new versions were inserted.

Encrypted logical backups contain the affected tables' DDL/data and the prior
ledger. Windows current-user DPAPI encryption was verified by decrypting and
comparing the plaintext hash; this is integrity verification, not a full disaster
restore rehearsal. Keep the original Windows user/machine for backup recovery.
Private backup and sanitized execution evidence are in:
`.run/sso-staging-20261030T232234Z/` (UTC folder name; local date is October 31).

API code remains local and undeployed. The old API's exact-ledger readiness check
will keep BCMD/NAPA unavailable until the updated release and schema metadata are
deployed. SSO create/export acceptance remains pending that deployment and the
normal export approvals. GitLab fetch was unreachable during this operation;
refetch/reconcile main before any subsequent push or merge.

## Operator rollout and recovery reference

1. Fetch current main again and review migration numbers and checksums against
   the live ledger. These numbers assume main still ends at 0013. Stop on any
   competing migration, schema drift or target mismatch. Take a verified backup
   and arrange a BCMD maintenance window.
2. Keep BCMD unavailable during migration/deployment. Its readiness compares the
   complete ledger and exact schema; an older release will fail readiness once
   a new version is recorded. Other API modules do not require these BCMD shapes.
3. In DBGate, run the complete guarded wrappers in `docs/schema/dbgate-apply/`
   for **0014, then 0015, then 0016**, one file per clean autocommit session.
   Do not rerun old creation steps against upgraded tables. Each wrapper checks
   `dswd_4ps_tuloy_dev` / `ppisv2-v3Master`, exact prior ledger and before/after
   metadata, obtains the migration lock, and inserts only its own ledger row
   after verified postflight. Stop on STOP or any SQL error.
4. Deploy this API release with all three migration files and the updated
   `expected_schema.json`. Reuse the existing BCMD runtime account. No new
   database login or runtime grant expansion is required. Do not run startup DDL.
5. Verify signed-in SSO `/status`: available and creation_available must be true.
   Preview candidates, save an explicitly approved small batch, reload its frozen
   members, and verify its creator/audit staff FK while the native FK is NULL.
   Check a native account still works and denied affiliations still return 403.
6. Obtain genuine renewed COLUMN_MAPPING and DQA_RULES attestations: this change
   modifies their hashed source artifacts. Do not copy old hashes or record
   synthetic test approvals in staging. Export/download remain blocked until
   all current approval gates and CLEAN DQA pass. Only then test an approved CSV
   and verify its staff-attributed export/download audit. Payroll preview stays
   read-only; this release does not add a batch payment-save feature.

MySQL DDL autocommits; ROLLBACK cannot undo these ALTERs. A crash after an ALTER
but before its ledger insert can be recovered by rerunning that same guarded
wrapper: it accepts the exact post-shape and records the real checksum. Never
update/delete migration history or blindly rerun plain ALTER SQL.

For an application incident, disable BCMD and retain the migrated schema/audit
records while preparing a forward fix. Do not deploy the old 0013-only readiness
files and expect BCMD to remain active. Do not drop staff columns after SSO writes;
that would erase creator attribution. No automatic schema downgrade is provided.

## Local validation

Use the existing API test environment. `NAPA_TEST_CONFIG` must identify only the
dedicated loopback synthetic MySQL container on port 13498, never staging.

```powershell
python -m pytest tests/test_bcmd_sso_mysql.py
python -m pytest
```

Coverage includes SSO DSWD/TEST criteria save -> frozen members -> approval-gated
CSV -> payroll preview; typed audit attribution; native/staff numeric-ID and
idempotency collisions; missing staff FK refusal; actor XOR constraints; audit
failure rollback; CHED/TESDA denial; and preservation of historical native rows
through 0014-0016. Existing real-MySQL tests also rehearse guarded migration
wrappers, wrong-target/schema-drift rejection and exact readiness/grants.

These are local synthetic acceptance checks, not staging deployment evidence or
proof that any real beneficiary batch, approval, export or payment was created.

Validated locally on 2026-10-31 (Asia/Shanghai), based on origin/main
`41aa4fa642cf3965020fa3b49dc192a80b939ff7`: **1,106 passed, 17 skipped** with
the dedicated synthetic MySQL config. The 13 new identity/migration checks pass.
JUnit evidence is in the ignored `.run/bcmd-sso-full.xml` in this worktree.
Skipped tests remain unverified; no authenticated staging write acceptance was
performed and no code or schema was pushed/applied remotely.

## Release preparation: 2026-10-31

The pre-push full suite was repeated against current main `41aa4fa` with the same
result: **1,106 passed, 17 skipped**. Code commit `0e4422f` was pushed after
confirming no new main commits. Release MR:
https://git.dswd.gov.ph/mvinas/4ps-tuloy-api/-/merge_requests/24

This MR is the API deployment step for the actor migrations already verified
above. Merge remains gated by CI and the project's normal merge checks. The
existing frontend requires no corresponding change. Source and backup evidence
stay separate: credentials, decrypted rows and encrypted backups are not in Git.
