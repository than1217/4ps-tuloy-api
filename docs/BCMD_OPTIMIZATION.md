# BCMD bounded criteria workflow

The bounded-preview optimization uses API and web code with existing batch
tables 0008-0013. Subsequent SSO creator support requires additive migrations
0014-0016; see BCMD_SSO_ACTORS.md. Existing API credentials and readiness checks
remain in use. No nginx edit, dependency or new database account is required.

## UI and API contract

BCMD now reads the persisted batch register. The contributed browser-only draft
screen is retained as LegacyBCMDCriteriaBatching for reference/tests but is not
the live route. Opening BCMD does not read source beneficiaries. Opening Create
batch reads one 25-record /bcmd/candidates page. Next/Previous fetch only the
requested page; there is no continuous enrollment hook, full-feed scan or last-
page loop. Supported live category remains TK, CHED, ENROLLED, year 1, semester 1.
TA/TT and TESDA batching are not implicitly enabled.

GET /v1/api/tuloy/staff/bcmd/candidates accepts exact academic_year (max 20),
program_name (max 255), exact region_name (max 100 via existing household/geography tables), literal search (max 100; TULOY ID/member ID/program),
limit 1-200 and offset 0-10000. All values are bound parameters. Count and preview
share a repeatable-read read-only transaction. total_count counts source records,
not distinct people or export-ready rows. Both queries use a 5000ms MySQL SELECT
execution hint. This bounds database work; it is not a staging load-test result.

POST /bcmd/batches still supports record_ids for the existing NAPA workflow.
Alternatively send name, category, criteria and a record_count of 1 or more (no
fixed batch size). Exactly
one selection mode is allowed. The server checks the idempotency key first,
selects stable IDs inside its consistent save snapshot, rechecks source and TK
eligibility, maps/freeze members, performs DQA and writes the existing audit in
one transaction. If fewer eligible records match than record_count,
RECORD_COUNT_EXCEEDS_ELIGIBLE returns 422 with no writes. The recorded criteria include the resolved IDs. A replay returns
the original frozen batch even if live source data has changed. The browser
retains the exact payload/key after uncertain errors and disables edits until a
retry settles it. Changed, unapplied filters cannot be saved accidentally.

DSWD/TEST affiliation rules remain. After migrations 0014-0016, native and SSO
creators/audit actors use separate api_users/staff_users foreign keys. Status
enables creation only after schema readiness; the UI follows that capability.
No username matching or fake actor ID is added. Exports still require approvals.

## NAPA and payroll

Open in NAPA passes the actual saved public UUID into the existing workspace.
Export still requires all existing approvals and CLEAN DQA. Creating a batch does
not approve export, download CSV, record or release a payment.

GET /bcmd/batches/{public_id}/payroll-preview?months=1..12 reads frozen CLEAN TK
CHED batch members under the same batch shared lock/purge checks. It reuses the
existing CHED monthly rate and household cap (3), aggregates only this batch and
returns payable/capped counts plus an estimate. It does not touch payment tables,
write an audit event, export data or call payment generation. Other batches and
existing payments are excluded; this is not release authorization.

## Validation

- Local disposable MySQL: filtered counts/pages, literal wildcard/injection tests,
  atomic criteria save, count limits, invalid mixed modes, retry after live-source
  changes, idempotency conflicts, source revalidation, household cap and DQA block.
- Synthetic BCMD -> NAPA approval/export/download -> frozen-batch payroll preview
  works with no payment_transaction table. Approvals are synthetic test fixtures
  only and are never applied to staging.
- UI tests: no source reads before opening modal, bounded paging, global filters,
  criteria-only save, uncertain-save retry key, SSO block and read-only preview.
- Browser journey: preview paging, applied criteria save, selected-batch navigation into NAPA and read-only payroll computation. No matched-CHED scan or payment-generation requests.
- Existing full suites retained, including contributed legacy-screen tests.

Live staging counts/performance and authenticated SSO/native acceptance remain
operator checks. No staging beneficiaries, batches, approvals or payments are
created during this verification.
