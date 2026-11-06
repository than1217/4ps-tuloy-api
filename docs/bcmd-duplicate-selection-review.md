# BCMD duplicate-selection review

Status: implemented locally (branch `feat/bcmd-duplicate-selection-review-20261104`), not deployed.
Scope: DQA "For verification" findings with rule code `SELECTED_IN_OTHER_BATCH`.

## Problem

Criteria batches take the first N eligible records in source order, so batches created one
after another overlap. When the same member and academic period (the claim identity,
`bcmd_dqa_rules.claim_key` = member_ref, academic_year or '', semester or 0) is selected in
two or more batches that have no NAPA export yet, DQA marks the record REVIEW
(`SELECTED_IN_OTHER_BATCH`) in every one of them. Nothing in the system could settle it, so
the record stayed out of every DETAIL file.

This workflow lets an authorized reviewer record which batch keeps the claim. It does not
change any DQA result directly. The next DQA run of each affected batch reads the decision
and produces the outcome. Resolving never purges or deletes a batch, never overwrites a
stored result, and never bypasses any other rule.

## Data model (migration 0023)

| Table | Purpose |
|---|---|
| `bcmd_claim_conflict` | One row per claim identity that DQA found selected in more than one batch. Random `public_id`, `version` counter, `current_resolution_id` (the effective decision, NULL when none). |
| `bcmd_claim_resolution` | Append-only history. One row per RESOLVE or REVERSE, numbered by `version`. Holds the retained member (RESOLVE only), the reversed decision (REVERSE only), the reason, the actor, request ID and idempotency key. |
| `bcmd_claim_resolution_member` | The selections a RESOLVE covered at the time it was recorded, each RETAINED or EXCLUDED. |

Columns added to existing tables:
- `bcmd_batch.claim_epoch`;
- `bcmd_dqa_run.claim_epoch` and `bcmd_dqa_run.excluded_count`.

Widened CHECK lists:
- `bcmd_dqa_result.outcome` gains `EXCLUDED`;
- `bcmd_dqa_run` counts include `excluded_count`;
- `bcmd_audit_event.event_type` gains `CLAIM_RESOLVED`, `CLAIM_REVERSED` and `DQA_RUN_INVALIDATED`.

### Why at most one selection per claim identity can be effectively retained

The guarantee comes from the schema, not from application checks:

1. `uq_bcmd_conflict_claim (member_ref, academic_year, semester)` allows one conflict row per
   claim identity. Concurrent DQA workers that discover the same conflict upsert into the
   same row.
2. The effective decision is the single column `bcmd_claim_conflict.current_resolution_id`.
   The composite foreign key `(id, current_resolution_id) → bcmd_claim_resolution
   (conflict_id, id)` makes it point only at a decision of the same conflict.
3. A RESOLVE row carries exactly one `retained_member_id`. `ck_bcmd_resolution_action`
   requires it for RESOLVE and forbids it for REVERSE. The composite foreign key
   `(retained_batch_id, retained_member_id) → bcmd_batch_member (batch_id, id)` ties it to a
   real member of that batch.
4. In `bcmd_claim_resolution_member`, `uq_bcmd_resolution_member_retained (resolution_id,
   retained_flag)` allows at most one RETAINED row per decision.
5. `uq_bcmd_resolution_version (conflict_id, version)` makes history linear. Two concurrent
   writers that both read version n cannot both commit version n+1. The loser gets a
   duplicate-key error, which the API returns as 409.

Therefore each claim identity has at most one conflict row, that row has at most one
effective decision, and that decision retains at most one selection.

Rows reference members by ID. The purge script only clears personal columns and never
deletes member rows, so these foreign keys never block a purge. The conflict row keeps
`member_ref` (the internal member ID, no name, HHID or birth date). This lets later
batches match the claim. See policy question Q7.

## How DQA uses a decision (ruleset `essi-mvp-v3`)

For each member X of the batch being evaluated, with claim key k:

| Situation | Finding | Outcome effect |
|---|---|---|
| k is claimed by another batch's completed DETAIL export | `DUPLICATE_CLAIM` (FAIL) | unchanged from v2 |
| The effective decision for k EXCLUDED X | `SELECTION_EXCLUDED` (new, severity EXCLUDED) | EXCLUDED unless a FAIL or REVIEW finding applies |
| The effective decision RETAINED X, and it covers every other current selection of k | none | X can PASS |
| Other current selections exist and no decision covers them (no decision, or a selection made after it) | `SELECTED_IN_OTHER_BATCH` (REVIEW) | unchanged from v2 |

Outcome precedence: FAIL, then REVIEW, then EXCLUDED, then PASS. Unrelated FAIL or REVIEW
findings on the same member keep their effect.

The new EXCLUDED outcome is separate from both REVIEW and FAIL:
- It is counted in `excluded_count`, not in `review_count` or `failed`.
- It is left out of DETAIL, SUMMARY and the payroll preview (all of them read PASS only).
- It is left out of the FAILED file too. That file keeps exactly its approved columns and
  status values (`FAILED`, `FOR VERIFICATION`), and `file_count('FAILED')` is still
  `failed + review_count`. Policy question Q4 asks whether excluded records should appear
  in it.

A finding for `SELECTED_IN_OTHER_BATCH` or `SELECTION_EXCLUDED` carries the conflict's
`conflict_id` (a random UUID), so the web can open the review directly. The FAILED file
uses only `rule_code` and `remark`, so that field never reaches a file.

The ruleset hash changes, and `VERSION` is bumped from `essi-mvp-v2` to `essi-mvp-v3`. Every
run completed under v2 becomes outdated (`DQA_RULESET_OUTDATED`): each batch needs one new DQA
run before export readiness or the payroll preview returns. Historical runs, results, export
rows, hashes and downloads are not touched. Downloads rebuild files from their own run's stored
results, so the files stay byte-identical.

### Cycles, three-way overlaps, new selections

- **No batch-level precedence exists.** A decision applies to one claim identity, never to
  a pair of batches, so "batch A beats batch B" relations cannot form cycles. If A and B
  overlap on claims k1 and k2, a reviewer may keep k1 in A and k2 in B. Both batches lose
  one record each and both need fresh DQA.
- **Three or more batches.** One conflict lists every current selection. Choosing the
  retained one marks all the others EXCLUDED in the same decision.
- **A selection made after the decision.** For example, batch D is created later and
  includes the same claim. D's record is not covered, so it gets `SELECTED_IN_OTHER_BATCH`.
  The retained record also gets REVIEW again on its next run, because an uncovered
  competitor exists. The conflict shows `NEEDS_REVIEW`. The reviewer may confirm the same
  retained batch, which records a new decision covering D. Choosing a different batch
  needs a reversal first. Records excluded earlier stay EXCLUDED.
- **The retained batch is purged.** The conflict shows `RETAINED_UNAVAILABLE`. Excluded
  records stay EXCLUDED until a second reviewer reverses the decision. This is the most
  restrictive option; see Q5.
- **Reversal and resolving again.** A reversal records a REVERSE row, clears
  `current_resolution_id`, and returns every selection to `SELECTED_IN_OTHER_BATCH` on the
  next run. The history is never edited.

### Exported and paid claims are immutable

Resolve and reverse are both refused when any non-purged batch holding the claim:
- has an export: `export_seq > 0`, status `EXPORTED`, or a completed export row
  (409 `CLAIM_EXPORTED`); or
- has a `payment_transaction` row (409 `CLAIM_PAYMENT_RECORDED`).

The first rule covers more than "claimed": a batch that exported the record as FOR VERIFICATION
also blocks the decision (Q3).

If `payment_transaction` does not exist, no payment can be recorded and the check passes. If the
BCMD account cannot read it (MySQL 1142), the request fails closed with 503
`CLAIM_PAYMENT_CHECK_UNAVAILABLE`.

## Fresh DQA and concurrency

All writers take the batch row lock (`bcmd_batch ... FOR UPDATE`) before any run or conflict
row:

| Writer | Lock order |
|---|---|
| DQA start | batch, then insert run |
| DQA worker commit | batch, then run, then conflict upserts (sorted by claim key) |
| Export | batch, then export rows |
| Resolve / reverse | every affected batch in ascending ID order, then the conflict row, then runs |

A resolve or reverse transaction runs at READ COMMITTED (started explicitly, whatever the
session default), so every read after the locks sees the latest committed rows. With the
affected batches locked, it:
1. Re-reads the selections. If a batch that was not locked now holds the claim, it rolls
   back and retries, up to three times, then returns 409 `CLAIM_REVIEW_BUSY`.
2. Checks the idempotency key again (a concurrent request with the same key may have
   committed while this one waited; it then returns that decision), then the expected
   version (409 `CONFLICT_VERSION_STALE`) and the exported and paid rules.
3. Increments `claim_epoch` on every affected live batch.
4. Marks every QUEUED, RUNNING or RETRYING run of those batches FAILED with
   `last_error_code = CLAIM_DECISION_CHANGED`, clears the lease, and audits
   `DQA_RUN_INVALIDATED`.
5. Writes the decision, the covered selections, the new conflict version and one audit
   event per affected batch, then commits.

A run records the batch's `claim_epoch` when it starts. A worker chunk computed before the
decision then waits for the batch lock. After the lock it finds its run FAILED, or its
epoch different, and commits nothing.

`bcmd_export.run_reasons(run, batch)` adds `DQA_REFRESH_REQUIRED` when the latest run's epoch
differs from the batch's. Export readiness, export creation, `detail_ready` and the payroll
preview all use it, so they refuse until a new run completes.

An export racing a decision is serialized by the batch lock:
- If the export commits first, the decision sees `export_seq > 0` and is refused.
- If the decision commits first, the export sees `DQA_REFRESH_REQUIRED` and is refused.

## Authorization

- New scope `tuloy:bcmd:selection:review`. It is granted only with agency and partner DSWD
  (`scopes.SCOPE_AGENCIES`).
- Every conflict route requires BCMD access (an active DSWD/DSWD or TEST/TEST affiliation)
  **and** an active grant of that scope. Affiliation alone is refused with 403 FORBIDDEN,
  before any batch query. Reads are covered too, because conflict views link members
  across batches.
- No account receives the scope:
  - no migration or seed inserts it;
  - the staging full-access override (`STAFF_FULL_ACCESS_ENABLED`) does not confer it;
  - while that override is on, SSO sign-in reads no grant rows at all (an existing,
    tested property), so no SSO user can hold the scope until the override is off. Only a
    native account with an `api_user_scopes` grant can use the workflow meanwhile (Q9).
- **Reversal needs a second person:** the reviewer who recorded the decision being
  reversed may not reverse it (403 `SECOND_REVIEWER_REQUIRED`). This is the most restrictive
  reading. The repository does not say whether a second person is required (Q1).
- The web hides the action unless `/staff/me` lists the scope. The API checks it on every call.

### Provisioning (DBA or administrator)

Native (`api_users`) account:

```
python -m scripts.grants grant --user <username> --scope tuloy:bcmd:selection:review \
    --partner DSWD --agency DSWD --days 30 --by <approver>
```

Staff SSO account. There is no script; the DBA inserts one row after the user has signed in once:

```sql
INSERT INTO staff_user_scopes (staff_user_id, scope, partner_id, agency, granted_by, expires_at)
VALUES (<staff_users.id>, 'tuloy:bcmd:selection:review', 'DSWD', 'DSWD', '<approver>',
        CURRENT_TIMESTAMP + INTERVAL 30 DAY);
```

To revoke, set `revoked_at` and `revoked_by` on that row (`python -m scripts.grants revoke`
for native accounts). The change takes effect on the next request.

## API

All routes are under `/v1/api/tuloy/staff/bcmd` and appear in the authenticated internal
OpenAPI spec (`/tuloydswd/openapi.json`, tag "BCMD / NAPA").

| Method | Path | Purpose |
|---|---|---|
| GET | `/duplicate-selections?batch_id=&limit=&offset=` | Conflicts (optionally those touching one batch) with their competing batches |
| GET | `/duplicate-selections/{conflict_id}` | One conflict with its decision history |
| POST | `/duplicate-selections/{conflict_id}/resolution` | Record which batch keeps the claim |
| POST | `/duplicate-selections/{conflict_id}/reversal` | Reverse the effective decision |

Request rules:
- Mutations need:
  - a UUID `Idempotency-Key`;
  - a plain-text `reason` (10–500 characters, no control characters);
  - `expected_version`.
- Resolve also takes `retained_batch_id`, the batch's public UUID.
- Member and period are always read from stored rows. Clients never send personal
  identifiers.
- A same-key replay with the same body returns the stored decision. The same key with a
  different body returns 409 `IDEMPOTENCY_CONFLICT`.

What views return:
- Selections show batch ID, name, category, batch status, export state, line number,
  decision role and whether the batch needs fresh DQA.
- No names, HHIDs, member IDs or birth dates are returned. The line number locates the
  record in the batch's member list, which BCMD users can already read.

Audit:
- The decision row records actor, label, agency, time, reason, request ID and version. The
  previous state is the prior version.
- `bcmd_claim_resolution_member` records the affected selections.
- Each affected batch gets a `CLAIM_RESOLVED` or `CLAIM_REVERSED` audit event, with
  `reason_code` RETAINED, EXCLUDED or REVERSED.
- No payload, token or beneficiary value is logged.

## Payroll paths

- `routers/bcmd.py` payroll preview: PASS rows of the latest completed run on the current
  ruleset and epoch. EXCLUDED is not PASS, and a stale epoch returns 422 `NAPA_DQA_REQUIRED`.
- `routers/payments.py` (API MR !27) reads `bcmd_batch_member.dqa_status`, the save-time check,
  not DQA results. The minimal read-side guard removes members that an effective decision
  EXCLUDED from its three member queries. These queries feed beneficiaries, compute,
  generate, and the CSV, summary and PDF exports. Nothing else in that module changed. Q2
  covers the larger gap: unresolved REVIEW and FAIL members are still payable there. The
  payments database account needs SELECT on `bcmd_claim_conflict` and
  `bcmd_claim_resolution_member`; without it these routes fail closed (503).

## Deployment order

1. The DBA applies `docs/schema/dbgate-apply/0023_bcmd_claim_review.sql`, or runs
   `python -m scripts.migrate apply`.
   - Until it is applied, the new API returns 503 `BCMD_NOT_READY`. The ledger and the
     expected schema must match exactly.
   - The CHECK changes copy `bcmd_dqa_result` and `bcmd_audit_event`. Raise the Workbench
     read timeout (600 s) before running the file.
2. In least-privilege mode, grant the runtime account:
   - SELECT and INSERT on the three new tables;
   - UPDATE on `bcmd_claim_conflict`.

   `python -m scripts.napa_runtime_grants` prints these with the existing grants, and the
   readiness check requires exactly them. The committed
   `docs/schema/DBA_NAPA_RUNTIME_GRANTS_REVIEW.sql` is marked withdrawn (existing account
   reused) and already lacked the 0018 run tables; it was not regenerated. A least-privilege
   account cannot read `payment_transaction`, so resolve and reverse return 503
   `CLAIM_PAYMENT_CHECK_UNAVAILABLE` there (Q10). Also grant the payments account SELECT
   as above. The existing-account mode on staging needs nothing.
3. Deploy the API, then restart the DQA worker. The worker must run the same code: v2 runs
   left queued fail with "ruleset changed".
4. Provision the review scope for named reviewers only, as above.
5. Re-record the DQA_RULES approval for the new artifact hash (see below). No other gate changes.
6. Re-run DQA on each batch. Deploy the web.

### Rollback

Use `migrations/rollback/0023_bcmd_claim_review_down.sql`, after rolling back the API.
- It refuses while any EXCLUDED result, `excluded_count`, or new audit event type exists:
  the restored CHECK constraints fail.
- Decisions and their history are dropped with their tables, so export them first if
  they must be kept.

## Approval artifacts changed

- DQA_RULES: `bcmd_dqa_rules.py` (rule, version) and `bcmd_export.py` (readiness epoch).
  This needs a new DQA_RULES approval. Group hash `d5fa2256b47c82fd…` on main becomes
  `31c2d648bf27fe9c…` (print the full value with the operator command before recording).
- Unchanged: HEADER_FORMAT, COLUMN_MAPPING, DPO_DISCLOSURE, PAYROLL_SEPARATION.

## Open policy questions

| # | Question | Behavior kept until answered |
|---|---|---|
| Q1 | Must a reversal be made by a second person, and does it need its own permission? | Second person required; same review scope |
| Q2 | Should `payments.py` read DQA results (PASS of the latest current run) instead of the save-time `dqa_status`? Today, unresolved REVIEW and FAIL members are payable there. Owner: the payments module's developer. | Only EXCLUDED members are removed |
| Q3 | Should a batch that exported the record as FOR VERIFICATION (not in DETAIL) block a decision on the remaining unexported batches? | Blocks (409 `CLAIM_EXPORTED`) |
| Q4 | Should EXCLUDED records appear in the FAILED file? This needs a new status value, which is a format change that needs HEADER_FORMAT and DQA_RULES re-approval. | Not in any file |
| Q5 | When the retained batch is purged, should its excluded records be released automatically? | Stay EXCLUDED until reversed |
| Q6 | Which roles may hold `tuloy:bcmd:selection:review`, and for how long? | Granted to no one; DSWD only |
| Q7 | How long are conflict rows (with member_ref) and decision reasons retained? Should the purge script clear them? | Kept; the purge script is unchanged |
| Q8 | Is a "recorded payment claim" any `payment_transaction` row for the batch (as implemented), or only for this member? | Any row for an affected batch blocks |
| Q9 | While the staging full-access override is on, should explicit `staff_user_scopes` grants of the review scope be read (sign-in then queries grant rows again)? | Not read: no SSO user can review until the override is off |
| Q10 | Should the least-privilege BCMD account get SELECT on `payment_transaction` (the readiness grant check would need to allow it)? | Not granted: decisions fail closed with 503 |

Superseded on 2026-11-04 (`docs/bcmd-napa-workflow.md`, "Payment history"): payment history is now
required for candidates, save, DQA, handoff, new NAPA files and payroll, so the BCMD account, the DQA
worker and the payments readers need SELECT on `payment_transaction`; readiness requires it and
missing access fails closed. Q2 is answered by the same change: payroll reads PASS of the latest
usable DQA run, never the save-time `dqa_status`.
