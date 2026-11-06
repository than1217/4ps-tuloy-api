> Current workflow (2026-11-04): owner-approval gates and manual NAPA handoff are no longer required. A batch appears in NAPA automatically after its latest BCMD DQA run completes, including completed runs with only failed records. Detailed/Summary require passed members; Failed requires failed/review members. Existing approval/handoff records remain historical and are not rewritten. Access control, current ruleset, review invalidation, payment-history checks and immutable-file integrity checks remain enforced. Payroll lists completed latest-DQA batches and the web can search them by public batch ID or batch name. The approval-recording and mandatory-handoff instructions below describe the earlier workflow and are retained as historical reference only. No migration or fabricated approval is needed for this change. Restart the API and local web to load the updated code.

# NAPA batch exports (DETAIL, SUMMARY, FAILED)

The end-to-end contract (categories, DQA, three files, payroll preview) is in
`bcmd-napa-workflow.md`. This page covers the export mechanics and approvals.

Deployment and real owner attestations are separate actions.

After migrations 0014-0016 and this API release, both native and SSO DSWD/TEST
accounts can create batches and perform approval-gated exports/downloads.
Native creators reference api_users; SSO creators reference staff_users through
separate foreign keys and retry-key namespaces. Never substitute a numerically
equal ID across those tables. See BCMD_SSO_ACTORS.md for the coordinated rollout.
Uses tables 0008–0013, additive actor migrations and the existing BCMD account.
No startup DDL, new login, nginx change, file-storage service or payment write.

## Client flow

1. Run DQA on the batch (`POST .../dqa-runs`) and wait for COMPLETED. Exports
   use the passed (or, for FAILED, the failed and for-verification) records of
   the latest completed run on the current ruleset.
2. `GET /v1/api/tuloy/staff/bcmd/batches/{batch_uuid}/export-readiness`
   returns per-file `files.{DETAIL,SUMMARY,FAILED}.ready` and
   `blocking_reasons` (top-level `ready` mirrors DETAIL for older clients).
3. `POST /v1/api/tuloy/staff/bcmd/batches/{batch_uuid}/exports` with
   `Idempotency-Key: <UUID>` and `{"file_type":"DETAIL"|"SUMMARY"|"FAILED"}`
   returns `export_no`, SHA-256, row/byte counts, `filename` and
   `download_url`. Retrying the same key for the same account and batch returns
   the same version; reusing it for another file type is 409
   IDEMPOTENCY_CONFLICT. Each DQA run has at most one file per type: asking
   again returns the existing export instead of a new version.
4. `GET .../batches/{batch_uuid}/exports/{export_no}/download` returns UTF-8
   CSV named `NAPA-DETAILED-<batch>.csv`, `NAPA-V2-SUMMARY-<batch>.csv` or
   `NAPA-FAILED-<batch>.csv`, with no-store and nosniff headers.
   Both operations recheck active DSWD/TEST affiliation and current approvals.
   `GET .../export` downloads the latest completed version, creating nothing.

Frozen batch members and the stored DQA results supply all CSV fields. DETAIL
keeps all 20 approved headers, renumbers the passed rows 1..n in frozen line
order, and keeps leading zero PSGC codes, blank approved NULLs and DD-MM-YYYY
dates. SUMMARY and FAILED use the header files in `docs/templates/`. All three
reject formula-leading values and invalid mappings. Only supported-policy batches
with at least one member and a completed current DQA run export (there is no
fixed batch size); an export that would
be empty is refused (NAPA_NO_RECORDS). Once a batch has any export, new DQA runs
are refused so the files keep matching their run. Exports made before DQA runs
existed (no `dqa_run_id`) still download with their original serializer. No live
source join, saved artifact, automatic purge or payment release occurs.

## Approval recording

Print current content hashes without connecting to a database:

```powershell
python -m scripts.napa_export_approvals
```

Obtain real attestations for HEADER_FORMAT (BCMD_OWNER), COLUMN_MAPPING
(DBA/DATA_OWNER), DQA_RULES (BCMD_OWNER), DPO_DISCLOSURE (DPO), and
PAYROLL_SEPARATION (MARTIN). Approvals outside the database must be recorded;
do not manufacture a signature or treat code deployment as approval.

Prepare a JSON array. Each record needs `gate_code`, `decision` (APPROVED or
REVOKED), `approver_role`, actual `approver_label`, evidence `reference`, ISO
`approved_on`, and the printed `artifact_sha256` (null for payroll separation).
Do not put beneficiary rows or secrets in the manifest. The latest inserted ID
for each gate wins; revoked and hash-mismatched approvals fail closed.

```powershell
python -m scripts.napa_export_approvals --manifest <attestations.json>
# Dry-run above; only after checking the exact target and attestations:
python -m scripts.napa_export_approvals --manifest <attestations.json> --apply --expect-db dswd_4ps_tuloy_dev --expect-host ppisv2-v3Master
```

Use the existing configured connection. No credentials appear in commands.
Recording requires INSERT on bcmd_approval; runtime requests never insert approvals.
Changed review artifacts invalidate their previous approvals. Preserve deployed
serializer versions when adding future versions; unsupported snapshots fail closed.

## Transaction and disclosure semantics

Generation is synchronous and covers every saved member, with no fixed batch
size, so very large batches take longer to generate. It locks the batch
exclusively before allocating an export number, generating bytes and atomically
committing STARTED/COMPLETED events, two stored hashes and EXPORTED status.
A crash or audit failure rolls back the whole request, including the sequence.
There is no committed in-progress job requiring a worker or reaper. A lost HTTP
response after commit is recovered with the same idempotency key.

Download holds a shared batch lock while materializing and comparing the regenerated
bytes with both the export row and its completion audit hash. Counts, header hash
and supported serializer versions must match as well. Manual purge takes the
exclusive batch lock. Data cannot be read after purge commits; already admitted
bytes cannot be recalled. Database lock waits are bounded to five seconds.

Before returning any bytes, admission and a conservative DOWNLOAD_UNCERTAIN result
are committed together. This version deliberately does not claim client receipt or
server-send completion; UNCERTAIN remains the result even after an ordinary download.
This limitation is explicit in the DPO disclosure artifact. Audit insert failure
returns JSON and no CSV. Hash mismatch records TAMPER_DETECTED and returns no CSV.

## Release checks

Apply migrations 0020–0022 (DBA review first) and run the DQA worker. Run the
complete API suite with the opt-in disposable local MySQL config, plus
frontend tests/lint/build. Never point the synthetic fixture at staging.
Deploy API and web together after review. Record genuine approvals, check batch
readiness, then verify one approved batch's CSV and audit trail. Current staging
approvals are not inserted by this implementation. Preserve all applied migration
files and existing ledger rows; record only the new verified versions. BCMD_ENABLED=false remains the
emergency whole-workspace disable switch.
