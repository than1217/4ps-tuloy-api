> Latest access ruling: NAPA/BCMD is available to all signed-in users with any active recognized DSWD/DSWD or TEST/TEST grant; no module scope is required. See NAPA_RELEASE_READINESS.md for current delivery status.

# NAPA mapping resolution and remaining delivery work

Updated 2026-10-30 after Martin's answers. Supersedes the earlier open-question list.

## Confirmed rules

| Item | Decision / implementation |
|---|---|
| HHID | tbl_household.household_id, serialized as text |
| FamilyID | Same household_id when the active family association is present; NULL when missing |
| Five missing sample associations | Allowed NULL; member remains included |
| IP 0 | NO |
| IP NULL | Preserve NULL; empty CSV cell, never the literal NULL and never automatic NO |
| Recognized nonzero IP group | YES via the ODS reference; member-level source, no household fallback |
| Geography | Stored 10-digit PSGC codes, not internal geographic IDs |
| Sex | ODS V3 library: 1=MALE, 2=FEMALE; NONE cannot be exported as FEMALE/MALE |
| Suffix | ODS lib_ext_name_v3; unknown references remain validation errors |

The association's CBMS family_id is used to validate the source relationship, not
as the exported FamilyID. Multiple matching associations remain a validation issue.
No source records are edited to make an export pass.

The same-schema geographic libraries are lib_psgc_barangay and lib_psgc_denorm.
The source probe checks tbl_psgc_brgy and other tbl_psgc_* candidates too; none
were returned in this schema. All 200 sampled stored codes are ten-digit values
and both barangay sources agree. This sample does not establish whole-population
quality or the administrative release date of the code library.

## Delivery status

1. Local persisted batches, frozen snapshots, validation issues and durable create/DQA
   audit are implemented. Approval recording and final migration numbering remain.
2. Authenticated create/list/detail/member/issue/audit APIs and transactional
   idempotency are implemented. Export intentionally remains 501.
3. NAPA is connected locally for list/create/review; synthetic tests cover
   persistence, errors and retry behavior. Browser acceptance is still pending.
4. Complete snapshot field handling, category/source eligibility, synthetic database
   acceptance and export approval checks before enabling a real-data download.

Local mapping + formatter verification: 72 tests passed. No pushes, migrations,
real-data exports or database writes have been performed for this work.

See BCMD_LOCAL_CANDIDATE.md for routes, activation guards, test results and limits.


## Current policy update ? 2026-10-30

Confirmed by Martin: first-release TK candidates must be CHED, ENROLLED, first
year, semester 1. Middle names and suffixes may be blank. No automatic retention
period applies; snapshots remain until an explicit manual purge. This decision
is stored in each new batch policy, not retroactively substituted in old batches.
The operator-only purge implementation and remaining staging activation checks
are documented in NAPA_RELEASE_READINESS.md. Migration numbering is now 0007?0013;
Martin supplied matching staging ledger entries for all thirteen migrations.
Earlier delivery notes above describe the earlier local-only state.
