# Unified matched enrollment API

`GET /GetUnifiedMatchedBene?limit=100&offset=0` is documented in the protected
internal Beneficiary API group. It uses the existing matched-feed scope
`tuloy:legacy:ched:read` and its DSWD binding; it also supports the existing
verified SSO principal path. A docs grant alone does not authorize this feed.
Anonymous requests return 401; unauthorized agencies return 403 before reading
the view. This does not change partner API authorization.

The query selects six named columns from `vw_unified_matched_enrollment`:
`agency_name`, `tuloy_id`, `member_id`, `enrollment_details`, `created_at`,
and `updated_at`. The existing page envelope supplies `items`, `limit`, `offset`,
`has_more` and `next_offset`. Limit is 1–1000, offset is 0–10,000,000, and one
extra row determines whether another page exists. No SELECT * or write occurs.

The web app calls the equivalent staff alias:
`GET /v1/api/tuloy/staff/unified-matched-beneficiaries`.
Both routes accept optional `agency=CHED|TESDA`,
`course_type=degree|diploma|short_course`, and `search` (up to 100 characters).
These filters run before pagination across the whole feed. Search matches TULOY ID,
member ID, program name and student/ULI IDs; SQL wildcard characters are treated
literally. All query values are bound parameters, not SQL identifiers.

## View semantics and field contract

Staging SHOW CREATE VIEW was inspected read-only on 2026-10-30. The view returns
CHED degree rows and TESDA diploma/short-course rows **only when that tuloy_id
has no CHED match**. The API preserves that behavior rather than changing the
database view. A TESDA member can have both diploma and short-course rows.

The response permits only these nested course fields. `degree` and `diploma`
are course objects; `short_course` is an array of course objects as returned by
the view:

| Course type | Fields |
|---|---|
| All three | course_type, program_name, enrollment_status, date_encoded, date_updated |
| degree | student_id, date_enrolled, program_duration, academic_year, year_level, semester |
| diploma | uli_id, date_enrolled, program_duration, academic_year, year_level, semester |
| short_course | uli_id, start_date, end_date |

Unknown top-level JSON objects and unknown course fields are removed. The API
filters each short-course array item independently. Nested objects/arrays in a
permitted scalar course field are rejected. Mother's maiden
names, LRN and PhilSys are not returned. A malformed JSON document or unsupported
shape returns sanitized 503 `UNIFIED_MATCHED_SOURCE_INVALID`; no source values are
included in the error. The underlying view may reference private columns in its
CTE, but its output projection and the API's allowlist do not expose them.

Ordering uses agency, TULOY ID, member, timestamps and the binary representation
of course details. The last tie-breaker separates diploma/short-course records
with identical member IDs and timestamps. Offset pagination remains a live read,
not a cross-request snapshot; concurrent source changes can move rows.

## Delivery scope

The combined Matched Beneficiaries screen uses the staff alias and server-side
agency/course/search filters. It loads one 50-record page at a time, keeps separate
course rows for the same member, and shows agency, identifiers and course details
without inventing names or locations absent from the feed. Cards count only the
current page, not nationwide totals. The CHED-only BCMD workflow retains
the existing CHED source/cache. No nginx, table, view, migration-ledger, grant or
payment changes are part of this release.


## Unified Overview

`GET /v1/api/tuloy/staff/overview/unified` uses the same DSWD/verified-SSO guard.
It returns overall `counts` (`total_records`, `ched_records`, `tesda_records`,
`distinct_tuloy_ids`, `ched_beneficiaries`, `tesda_beneficiaries`) from the
unified view and a bounded `recent_matched` page.
Agency/course/search filters have the same contract as the unified list. They
apply only to the table; overall totals remain independent of those filters.
`offset` is 0..10,000,000, page size is fixed at 10, and `recent_has_more`
uses one extra probe row which is never returned. No full-feed browser scans.
Recent ordering is created_at descending, updated_at descending, then the stable
unified list tie-breakers. Pagination is a live read; inserts can move rows.

`total_records`, `ched_records` and `tesda_records` are course-record counts.
`distinct_tuloy_ids` (all matched beneficiaries), `ched_beneficiaries` and
`tesda_beneficiaries` count distinct TULOY IDs. None of them are matching
transactions, COR approvals or payment totals. The distinct counts exclude NULL
identifiers and count a diploma/short-course pair for the same ID once. View semantics still
exclude TESDA when a CHED match exists for the same TULOY ID. Course details
retain the existing privacy allowlist. The previous `/staff/overview` contract
is retained for compatibility but is no longer used by the web Overview.

Read-only staging query rehearsal: aggregate 0.672s; newest unfiltered page 0.953s;
TESDA page 0.016s; CHED degree search 2.640s. These are single-run timings, not a
load test. No schema, ledger, grant or payment changes.
