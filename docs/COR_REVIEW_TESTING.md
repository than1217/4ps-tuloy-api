# COR review and synthetic CSV testing

Implementation and staging deployment preparation, 2026-10-31.

Martin confirmed that COR review decisions must not change partner-submitted
enrollment records or payroll eligibility. DSWD and TEST staff with active matching
affiliations may review; CHED/TESDA partner accounts may not. Both native and SSO
identities retain their correct foreign-key namespaces.

## Contracts and persistence

- `GET /v1/api/tuloy/staff/cor/enrollments`: agency, course_kind, literal search by TULOY/member ID, limit (1–100),
  offset (0–1,000,000). Default page size 25. No full-dataset download.
- `GET /v1/api/tuloy/staff/cor/enrollments/{reference}`: current snapshot and at most
  100 most recent decision events. Reference is `CHED:<source id>:degree` or
  `TESDA:<source id>:diploma|short_course`; not a person or household ID.
- `POST .../{reference}/decisions`: APPROVED/DENIED, reason (3–1000 characters),
  expected_revision, source_sha256, request_key (UUID). Only COR review is saved.

The source is `enroll_matched_CHED_tbl` or `enroll_matched_TESDA_tbl`. TESDA rows
follow the unified view's existing CHED-precedence rule. Program and enrollment
fields are explicitly selected; arbitrary nested JSON fields are not exposed.
This records the staff decision; it does not introduce a document upload store.

Draft migration `0017_cor_review.sql` adds `cor_review` (current revision) and
`cor_review_event` (append-only application audit). Review and event commit in one
transaction. A shared source-row lock plus the exclusive review lock protects the
decision snapshot. A changed source/revision returns 409; repeated requests with
the same UUID and payload return the prior result. Changes to source data mark
the old review stale. No enrollment status or payment rows are written.

Before staging deployment, fetch the current main and compare target migration
ledger/DDL and source columns. Confirm 0017 remains free; renumber an unapplied
draft if needed. Do not modify historical migration files or ledger entries.
Use the normal reviewed migration runner and existing authorized account. Deploy
the API/migration before the frontend; an absent table returns COR_NOT_READY.
Rollback the application first and retain review/audit data; do not automatically
drop populated review tables.

## Recording

Martin authorized staging deployment so the recording can stay on one site.
Verify the deployed API and UI before recording; local test results alone do not
prove staging acceptance. Visible guides and downloads say "test data". The fixed
CSV remains separate from a real batch release. User management remains unchanged.
Start recording only after the explicit Ready cue and confirmation gate.
