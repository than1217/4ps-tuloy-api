# Durable BCMD DQA runs

Sources: supplied MySQL worker runbook sections 4–5, Implementation Blueprint
section J, and proposed ESSI parameters II/IV/VI with age/member-status comments.
These are draft references, not approved payment policy. No starter CREATE script
was executed against an existing schema; migrations adapt the existing tables.

## API and worker

Under `/v1/api/tuloy/staff/bcmd`, using the existing authenticated DSWD/TEST guard:

- POST `/batches/{id}/dqa-runs`: UUID Idempotency-Key, no client rule JSON, returns
  202 and a durable numbered run. Same-key replay returns the original run.
- GET `/batches/{id}/dqa-runs`: bounded newest-first run history.
- GET `/batches/{id}/dqa-runs/{run}`: persisted status, rules version/hash and counts.
- GET `/batches/{id}/dqa-runs/{run}/results`: bounded member outcomes/findings.

Run `python -m scripts.bcmd_dqa_worker` from the API root using the normal BCMD
environment, or add `--once` to drain one job. Worker startup validates configuration,
identity, grants, migration ledger and expected schema; it performs no DDL/grants.
Supervise the worker separately from the API. Polling never drives execution.

The MySQL queue claims with FOR UPDATE SKIP LOCKED, a 60-second lease/fencing token
and three-attempt limit. Use row-based replication. Computation is bounded to 50
frozen members outside locks; findings, cursor, counts and completion commit
atomically after checking the current unexpired lease. Expired owners cannot
commit. Recovery resumes the committed member-ID cursor, with finite backoff and
jitter. Unique run/member results and checkpoint checks prevent double counting.
The existing 1–200-member batch limit remains; this is not million-record support.

Batch row locking prevents overlapping active runs and serializes run numbers.
Existing runs/results remain immutable. Completed and terminal failed events are
audited. Exported/purged batches cannot start new runs. A failed job remains in
history and the operator may start another numbered run after resolving its cause.
Raw DB exceptions and member values are never stored in job error messages.

## Worker diagnostics

The standalone worker initializes the shared JSON logger. Processing exceptions
emit `dqa_processing_failed` with the run UUID, attempt, processed count, original
`dqa_request_id`, exception type, source frames and numeric database errno when
available. Connection/readiness failures emit `bcmd_dqa_worker_unavailable`.
Exception messages, SQL, traceback locals and beneficiary values are not logged.
Retry limits and saved public error codes are unchanged.

For the supplied systemd service, inspect stdout/stderr in the journal:

```bash
sudo journalctl -u tuloy-bcmd-dqa-worker -n 200 --no-pager
```

Restart the worker after deploying code so it loads the updated logger. A service
restart does not requeue a terminal FAILED run; start a new run through the API
after resolving its cause. Logging cannot recover exceptions from older runs.

## Rule behavior (`essi-mvp-v3`)

The ruleset pins canonical config/hash. Each run pins its own Philippine (Manila)
evaluation date when the run is created (the batch's save-time date is used only by
the legacy save-time check). Each member gets one outcome; all findings are kept.

- **FAIL**: missing member ID, HHID or name; duplicate member in the batch;
  missing or invalid birth date; age outside 18–30 inclusive on the evaluation
  date (`AGE_OUTSIDE_RANGE`); incomplete or invalid academic period for TK/TA
  (TT graduates need no semester); invalid NAPA field; save-time source mapping
  failure; the same member, academic year and semester already sent in another
  batch's completed DETAIL export (`DUPLICATE_CLAIM`).
- **REVIEW** (For verification): the same member and period selected in another
  batch that has no NAPA export yet (`SELECTED_IN_OTHER_BATCH`), unless a
  duplicate-selection review decided it.
- **EXCLUDED**: a duplicate-selection review kept the same member and period in
  another batch (`SELECTION_EXCLUDED`). Counted in `excluded_count`, in no NAPA
  file and not in the payroll preview. See `docs/bcmd-duplicate-selection-review.md`.
- **PASS**: no findings. Nullable FamilyID is not a finding.

FAIL takes precedence over REVIEW, REVIEW over EXCLUDED. A decision recorded after
a run started makes that run outdated (`DQA_REFRESH_REQUIRED`): run DQA again. Client status, partner validation, enrolment
and graduation are applied by the source view at selection and are listed in
the run's `checks`. Parameters TULOY cannot evaluate (SHS status, pathway, TVET
program, Tuloy Gabay, external disbursement ledger, payment account) are listed
in `not_evaluated` and shown on screen. `payment_eligible` stays false: passing
DQA is not payroll or disbursement authorization.

Runs on an older ruleset (`essi-draft-frozen-review-v1`) stay readable but are
`ruleset_current=false`; exports and the payroll preview ask for a new run.
The cross-batch checks ignore purged batches, whose member references are
cleared.

Legacy inline mapping results remain compatible and separate. Public batch
metadata reports `dqa_run_status` (NOT_RUN until a run exists) and a compact
`latest_dqa`. The frontend uses the run workbench when status advertises
`dqa_runs_available`.

## Deployment and verification

Migrations 0018/0019 add run/result tables and extend audit vocabulary. The expected
schema manifest and runtime grant generator include them. Run tables require
SELECT/INSERT/UPDATE; result tables require SELECT/INSERT. Review/apply migrations
and generated grants through the existing release process, then supervise a worker
before advertising the capability. No remote migration/deployment was performed.

Real local MySQL tests exercise replay, active-run conflicts, count reconciliation,
paging, prior history, expired leases/fencing, checkpoint replay, retry exhaustion,
nullable FamilyID, multiple findings, PASS results, cross-batch REVIEW and
duplicate-claim FAIL, outdated-ruleset blocking and strict runtime readiness.
Browser acceptance drops a committed start response, replays its key, waits on the
real worker and confirms findings survive a fresh sign-in. Existing web regression
tests also cover legacy fallback and new run UI behavior.

Migration 0022 adds `ix_bcmd_member_claim` for the cross-batch checks. The three
NAPA files built from a completed run are described in `bcmd-napa-workflow.md`.
