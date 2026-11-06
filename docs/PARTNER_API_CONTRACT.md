# Partner API contract: /v1/api/tuloy (draft for approval)

Status: **draft, 2026-10-26.**

Local compatibility implementation: `POST /v1/api/tuloy/members/verify` now performs the same read-only lookup as `/VerifyMemberBene`, protected by `tuloy:member:verify`. It returns HTTP 200 with `{status, tuloy_id, member_id}` for `Matched`, `Not Found`, or `Multiple Match Found`; both IDs are strings (a numeric `member_id` from the view is sent as text) and are null for negative or ambiguous results. All mapped optional filters are applied; supplying `suffix` returns 422 because there is no confirmed source column. If the database is unreachable, the connection drops or the query times out, it returns 503 (`DB_UNAVAILABLE` or `DB_TIMEOUT`); an unexpected query or schema error returns 500 `DB_ERROR`. No verification receipts are stored. This implements the existing matching behavior only; the proposed eligibility, region rules and response contract below remain unresolved. The other five partner operations still return 501.

The original proposal follows (its all-501 description is superseded for verification): The six operations exist in this API behind `PARTNER_API_ENABLED`. Each one authenticates, checks its scope, validates the input it can validate today and answers **501 `NOT_IMPLEMENTED`**. Nothing is read or written until the open items for that operation are approved.

Rules for this draft:
- Nothing here invents enrollment rules, event types, matching rules, database mappings or permissions. Where a decision exists, it is cited. Where none exists, it is listed as **Open**.
- Code: `partner/router.py`. Tests: `tests/test_partner.py`, `tests/test_partner_submit.py`.
- Reference implementation of the same paths: `4ps-tuloy-api/app/api/routers/tuloy.py` (reference only). Approved operation list: vault `20-Projects/TULOY/analysis/2026-10-18-partner-operation-matrix.md`.

## 1. Rules for all six operations

| Topic | Contract today | Source / status |
|---|---|---|
| Base path | `/v1/api/tuloy`; authentication and operations endpoints live outside it (`/token`, `/healthz`, `/readyz`) | Task requirement; ADR 2026-10-18 |
| Authentication | `Authorization: Bearer <JWT>` from `POST /token`. HS256, 30-minute expiry, `iss`/`aud` checked, the account is re-checked on every request | Implemented. Partners never use human login tokens (ADR 2026-10-18). OAuth2 client credentials is the stated later target |
| Authorization | Deny by default. Each operation needs one active grant for its scope, read from `api_user_scopes` on every request (migration 0002). No table, no row, expired, revoked or unbound: 403. JWT claims grant nothing | Scope names from the approved matrix; grants managed with `scripts/grants.py` (`docs/RBAC.md`). **Open:** grant owner and review cadence |
| Partner binding | Each grant is a whole `(scope, partner_id, agency)` tuple (`principal.grants_for(scope)`); bindings never mix across scopes. Not yet used to filter data, because the operations return 501 | **Open:** each operation must enforce its tuple against record ownership when it's implemented; region dimension |
| Object ownership | A partner may only see records it created, inside its bound agency and regions. If a record is out of scope, return 404, not 403, so its existence is not disclosed (ODS §6.7, §8.6) | Rule from ODS. **Open:** the region dimension and its source |
| Error format | `{"detail": ..., "error_code": "...", "request_id": "..."}`. Validation errors list `loc`, `msg` and `type`, and never echo input | Implemented. **Open:** adopt the ODS §8.5 envelope (`status/message/data/meta/error_code/request_id`) for partner routes before first release. This is cheap now and breaking later |
| Status codes | 200/201 success; 401 auth; 403 scope; 404 not found or not owned; 409 idempotency or revision conflict; 413 body > 1 MB; 415 not JSON (R3); 422 validation; 429 rate limit (with `Retry-After`); 501 not yet available; 503 `DB_UNAVAILABLE`/`DB_TIMEOUT`; 500 `DB_ERROR` or `INTERNAL_ERROR` | Implemented except 404/409, which need the business logic. The OpenAPI document lists only the codes each operation can return today: placeholders list no 200, 404 or 409 |
| Request ID | `X-Request-ID` accepted when it matches `[A-Za-z0-9._-]{8,64}`; otherwise generated; always returned | Implemented |
| Idempotency | All writes require an `Idempotency-Key` header (UUID). Replaying the same key with the same body returns the first result. The same key with a different body returns 409 | Header enforced. Storage and replay **Open** (backlog P2) |
| Rate limits | 120 requests per minute per IP, in-process. Login: 10 attempts per 5 minutes per IP + username | Implemented. **Open:** per-client quotas from measured capacity (ADR 2026-10-18) |
| Personal data | Responses return opaque IDs and status codes, never names, unless the operation needs them. Logs and audit events carry IDs and counts only. Synthetic examples only | Rule. Field list **Open** (DPO, data owner) |
| Audit | One event per call: action, outcome, actor (`user:<id>`), request ID. No payloads | Implemented as JSON log events (`tuloy.audit`). **Open:** retention, and whether audit needs its own table |
| Caching | Partner responses are never cached (`Cache-Control: no-store`) | Implemented by middleware |

Scopes (from the approved matrix): `tuloy:member:verify`, `tuloy:enrollment:submit`, `tuloy:enrollment:read`, `tuloy:enrollment:update`, `tuloy:history:read`.

## 2. Operations

### 2.1 POST /v1/api/tuloy/members/verify (`tuloy.members.verify`)

| | |
|---|---|
| Purpose | Confirm that a person is a uniquely matched, eligible 4Ps beneficiary. It does **not** establish educational enrollment (ADR 2026-10-17) |
| Scope | `tuloy:member:verify` |
| Request (candidate) | `first_name`, `last_name` (1-100 chars), `date_of_birth` (`YYYY-MM-DD`, must be a real date) are required. Optional: `middle_name`, `suffix` (not supported: any value returns 422), `sex` (≤50, code set **Open**), `member_id`, `household_id` (≤50), `lrn` (12 digits), `pcn` (12-19 digits or dashes). Unknown fields are rejected |
| Response (proposed) | Match: `{match_result: "MATCHED", member_id}`. `member_id` comes from ODS `tbl_person.person_id` (Martin, 2026-10-20). No names are echoed |
| Status | 200 matched. Definitive negative: 400 with Boolean false (ADR 2026-10-17). Ambiguous: distinct error code. 422 invalid input |
| Existing capability | `POST /VerifyMemberBene` matches `UNHEX(MD5(UPPER(TRIM(first))/UPPER(TRIM(last))/dob))` on `vw_verify_member_bene.match_keys`, narrows on optional columns, and returns `Matched` / `Not Found` / `Multiple Match Found` |
| Source mapping | `vw_verify_member_bene`: `match_keys`, `middle_name`, `sex_id`, `member_id`, `listahanan_id`, `lrn`, `philsys`, `bgy_psgc_code`, `client_status`, `roster_status`, `tuloy_id` |
| Idempotency | Read-only. The reference implementation stores a 30-day verification receipt per call. **Open:** whether this API stores receipts |
| Open | (1) Eligibility filter: CS1/CS3 and unarchived (ADR 2026-10-17). Does the view already apply it, or do `client_status`/`roster_status` need filters? DBA questions 19-25. (2) Case and space normalization: the view key uppercases and trims; the reference verifier is binary (24 Oct go/defer checkpoint). (3) The sex code set conflicts (see ASSESSMENT §1). (4) `pcn`: capabilities in the reference say `search_pcn=false`. (5) The final field list waits for the CHED/TESDA verify-fields replies. (6) Agency or region filtering before counting matches (ADR 2026-10-19 consequence) |

### 2.2 POST /v1/api/tuloy/enrollments (`tuloy.enrollments.submit`)

Design and contract per ADR 2026-10-27 (`40-Decisions/2026-10-27-tuloy-enrollment-submit-grant-bound-body.md`, ratified by Martin). **The route validates the request and then answers 501 `NOT_IMPLEMENTED`. Nothing is persisted. It stays off by default (`PARTNER_API_ENABLED`).**

| | |
|---|---|
| Purpose | A partner submits an agency-approved enrollment for a verified member |
| Scope | `tuloy:enrollment:submit` |
| Request | `Idempotency-Key` header (UUID) is required; it is format-checked today and used for replay only after activation. `Content-Type: application/json` is required (a `charset` parameter is allowed, case-insensitive). Body: one strict JSON object of at most 1,000,000 bytes, chosen by the account's grant (below) |
| Response today | 501 for every valid request, with audit `partner.enrollments.submit not_implemented` |
| Response (proposed) | 201 `{transaction_id, student_id, status, revision}`, using opaque IDs |
| Status | 403 grant missing, ambiguous or not approved; 413 body over 1,000,000 bytes; 415 `UNSUPPORTED_MEDIA_TYPE` the `Content-Type` is not `application/json` or is missing; 422 invalid input; 501 today. Proposed once active: 201 created; a replay of the same key after reauthorisation returns the original status code and body; 409 (marked "after activation" in the OpenAPI and the error table) key reused with a different body (both are activation-phase behaviour, not active today: today the same key always gets 501, never 409); 404 if the referenced verification is not found or not owned |
| Existing capability | Partial: `POST /PostEnrolleeMatchedCHED` writes a match transaction plus a CHED data row. It has no transaction ID, no owner, no idempotency and no link to a verification |
| Storage | New application-owned tables are needed (enrollment, idempotency record). They must not go in ODS. **Open:** placement in the shared schema vs a separate schema, decided by the DBA |
| Transaction boundary | One DB transaction: idempotency record + enrollment row + first event, committed together. Unique key on `(operation, account, agency, idempotency_key)` |

**Order of checks.** Authentication, then the scope (403), then the `Idempotency-Key` header (422), then the grant binding (403), then the `Content-Type` (415), then the body, read as a stream (413 over the limit, 422 invalid), then 501. A declared `Content-Length` over the limit gets 413 from the global middleware before everything else. A request with no usable grant gets 403 whatever its body holds.

**Grant binding.** The agency comes from the caller's stored grant, never from the body.
- The account must hold exactly one distinct active `(scope, partner_id, agency)` grant for `tuloy:enrollment:submit`, with `partner_id` equal to `agency` and `agency` in `APPROVED_AGENCIES` (CHED, TESDA, DSWD). Rows that repeat the same tuple count once.
- None, expired or revoked: 403 (`authz.scope denied`). More than one distinct tuple: 403, reason `ambiguous_binding`. `partner_id` different from `agency`, or an agency outside the catalogue: 403, reason `unapproved_binding`. The first grant is never picked.
- DSWD: 403, reason `payload_not_approved`, until a DSWD payload is approved.
- The grant is read on every request, so a revocation applies from the next one.

**Field matrix.** "Req" = required, "-" = rejected (even as null), "null" = may be omitted or null. Unknown fields are rejected at every level (`extra=forbid`), including `agency`, `partner_id`, `owner` and the like.

| Field | Type and rule | CHED | TESDA | DSWD |
|---|---|---|---|---|
| `tuloy_id` | strict non-empty string, kept as sent; format and length pending agency samples (size bounded by the 1 MB body limit) | Req | Req | denied |
| `student_id` | strict non-empty string, kept as sent; format and length pending agency samples (size bounded by the 1 MB body limit) | Req | - | denied |
| `uli` | strict non-empty string, kept as sent; format and length pending agency samples (size bounded by the 1 MB body limit) | - | Req | denied |
| `degree` | object (below) | Req | - | denied |
| `short_course` | object (below) | - | Req | denied |
| `mothers_maiden` | omitted or null only. Any object is rejected | null | null | denied |
| `enrollment_status` | strict integer, no range or code list yet (any integer reaches 501) | Req | Req | denied |
| `date_encoded` | RFC 3339 date-time, strictly `YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM)`, e.g. `2026-01-31T08:30:00+08:00`. No offset, space separator, missing seconds, `+0800`: rejected | Req | Req | denied |
| `date_updated` | same format, or null. Not earlier than `date_encoded` as an instant (equal instants in different offsets are accepted) | null | null | denied |

`degree` (CHED): `program_name` string 1 to 150; `date_enrolled` `YYYY-MM-DD` real date; `program_duration` integer 0 to 20; `academic_year` `YYYY-YYYY` and the second year is the first plus one (max 20 chars); `year_level` integer 0 to 10; `semester` integer 0 to 4. All required.

`short_course` (TESDA): `program_name` string 1 to 150; `start_date` `YYYY-MM-DD`; `end_date` `YYYY-MM-DD`, not before `start_date` (equal is accepted); `training_status` string 1 to 50 (**provisional**, no code list). All required.

Rules:
- Types are strict: a number where a string is expected (for example a numeric `student_id`) is rejected, as are strings for integers and booleans for integers.
- Bounds on `program_duration`, `year_level`, `semester`, `program_name` and `academic_year` reuse `schemas.py` (`Degree`). They are not agency-approved.
- Validation errors list `loc`, `msg` and `type` and never echo submitted values.
- The OpenAPI request body (`/tuloy/openapi.json`) shows both bodies as `oneOf`. Exactly one applies to an account.

**Body size.** The limit is the application's `MAX_BODY_BYTES` (1,000,000; `config.py`). A declared `Content-Length` above it is refused by the global middleware (413 `PAYLOAD_TOO_LARGE`, before authentication). R3 also reads its body as a stream and stops at the same limit, so a chunked body with no `Content-Length` also gets the same 413 body; a body of exactly 1,000,000 bytes is accepted. Other routes keep the declared-length check only.

**Audit.** On a denial (`authz.scope denied`) and on the 501 (`partner.enrollments.submit not_implemented`) every denial carries `actor` (`user:<id>`), `scope`, `operation` (`partner.enrollments.submit`), `outcome` (`denied`) and `reason`; the 501 carries `actor`, `scope`, `partner_id`, `agency`. The binding reference by reason:
- `unapproved_binding`, `payload_not_approved`: `partner_id` and `agency` as stored.
- `ambiguous_binding`: `bindings`, the sorted list of distinct `{partner_id, agency}` pairs, and no top-level `partner_id` or `agency`. No identifiers or body values.
- `no_grant` (none, expired or revoked): no binding exists, so none is logged. The denial comes from the shared scope check (`require_scope`), which R3 alone asks to add `operation` and `reason`; other routes' no-grant events still carry `actor` and `scope` only.

The request ID is added to every event. Never the identifiers, names, dates or any part of the body.

**Activation blockers** (nothing below is decided; the route stays 501 until each is ruled on):
1. ID formats and lengths for `tuloy_id`, `student_id` and `uli` (agency samples); until then only non-empty is checked.
2. `enrollment_status` code list, range and allowed transitions; `training_status` code list.
3. Term and course duplicate semantics: what makes two submissions the same enrollment (student, term, program, course dates).
4. DSWD payload: whether DSWD submits at all, and its fields.
5. `mothers_maiden`: DPO and data-owner ruling before any object is accepted, plus the field allowlists and retention for the other fields.
6. Idempotency and enrollment storage and recovery: placement (DBA), key namespace `(operation, account, agency)`, replay returns the original response after reauthorisation, a changed body returns 409, backup and restore plan.
7. Audit retention and access, and whether audit needs its own table.
8. Link to a prior verification and its validity window; catalog validation of programs and institutions; the single authoritative writer (also implemented in `4ps-tuloy-api`).

### 2.3 GET /v1/api/tuloy/enrollments (`tuloy.enrollments.list`)

| | |
|---|---|
| Purpose | A partner lists its own enrollments |
| Scope | `tuloy:enrollment:read` |
| Request | `limit` 1-100 (default 50); `after` is an opaque cursor (`[A-Za-z0-9_-]`, ≤128); optional `tuloy_id` (pattern as in 2.6) lists one person's enrollments. Other filters: candidates in the reference are `academic_year`, `term_code` and `enrollment_status`, **Open** |
| Response (proposed) | `{items: [...], next_after}`. Items carry IDs, statuses and period, without names unless approved |
| Ordering | Stable and unique, for example `(created_at DESC, id DESC)`. Keyset cursor, never offset (ODS §8.7) |
| Boundary | The query filters by the caller's binding in SQL, not after loading |
| Existing capability | Partial: `GET /GetMatchedChedBene` lists all CHED matches with no caller filter, offset paging and names |
| Open | Item fields; whether staff can call it; filters |

### 2.4 GET /v1/api/tuloy/enrollments/{transaction_id} (`tuloy.enrollments.get`)

| | |
|---|---|
| Purpose | Read one enrollment the caller owns |
| Scope | `tuloy:enrollment:read` |
| Request | `transaction_id`: opaque, `[A-Za-z0-9-]{1,64}`. The reference uses UUIDs; **Open** |
| Status | 200; 404 if not found **or** not owned (the two cannot be told apart); 422 malformed ID |
| Existing capability | None (no transaction ID exists today) |
| Open | Whether the full submitted baseline is read back (DPO question in the matrix §5.3) |

### 2.5 POST /v1/api/tuloy/enrollments/{transaction_id}/events (`tuloy.enrollments.record_event`)

| | |
|---|---|
| Purpose | Record a change to an enrollment (status change or correction) |
| Scope | `tuloy:enrollment:update` |
| Request | `Idempotency-Key` is required. Body: `event_type` plus payload. The **event types and transitions are Open** (program office). The reference uses an `expected_revision` optimistic lock |
| Status | 201; 200 replay; 404 not found or not owned; 409 stale revision or key conflict; 422 |
| Concurrency | Optimistic: `UPDATE ... WHERE id = ? AND revision = ?`. One transaction with the event insert |
| Existing capability | None |
| Open | Event catalogue, allowed transitions, who may correct what (program office: "authoritative writer and corrections owner", ADR 2026-10-22) |

### 2.6 GET /v1/api/tuloy/members/{tuloy_id}/enrollment-history (`tuloy.enrollment_history.list`)

| | |
|---|---|
| Purpose | Enrollment history of one person, identified by `tuloy_id`, as visible to the caller |
| Scope | `tuloy:history:read` |
| Request | `tuloy_id`: `[A-Za-z0-9-]{1,50}`, **provisional** until agency samples (source columns are `varchar(50)`/`varchar(100)`). `limit`, `after` as in 2.3. The former `/students/{student_id}/enrollment-history` path was removed with no alias (2026-10-27): it was a 501 placeholder, and `student_id` is an agency number, not a person identity |
| Boundary | **Open:** only the caller's own records, or every agency's history for a student the caller has enrolled? This decides data sharing between CHED and TESDA |
| Existing capability | None. `enroll_matched_CHED_tbl.student_id` is an agency student number, not a stable student identity |
| Open | Final `tuloy_id` format (agency samples); boundary; fields |

## 3. Mapping and compatibility

| Existing route | Consumer | Maps to | Plan |
|---|---|---|---|
| `POST /VerifyMemberBene` | none found in the vault inventory | 2.1 (method and source) | Keep. Once 2.1 is live, mark it deprecated with a sunset date (ODS §8.1) |
| `GET /GetEligibleBene` | tuloy-web-app backend proxy | none (staff list, not a partner operation) | Keep. No longer returns `m_maiden_name` or `account_no`, and shows `lrn` and `philsys` only by their last 4 digits, other digits as `*` (DB design decision 1(a), L08); other fields unchanged. It still needs its own field and scope ruling (F-10) |
| `POST /PostEnrolleeMatchedCHED` | `others/import_ched_csv.py` | 2.2 (partial) | Keep for the import. Do not reuse its tables for partner enrollments without a design decision |
| `GET /GetMatchedChedBene` | none found | 2.3 (partial) | Keep |
| `POST /token`, `POST /signup` | web-app proxy, import scripts | auth | Keep `/token`. `/signup` is now off unless enabled; the web-app proxy's automatic signup fallback no longer works and must use a provisioned account |

No existing route was renamed or removed.

## 4. Phased backlog

| Phase | Work | Needs before start | Done when |
|---|---|---|---|
| P0 (done) | Contract skeleton: six routes, auth, scopes, validation, 501, OpenAPI test | none | `tests/test_partner.py` passes |
| P1 decisions | Error envelope (ODS §8.5 or current); verify response codes; scope grant process; partner binding model; storage placement and DB accounts; single authoritative writer | Martin, DBA, program office | ADR recorded |
| P1 verify | Implement 2.1 on `vw_verify_member_bene` with the approved fields, eligibility filter and normalization. Optional receipt table | DBA answers 19-25; field replies; 24 Oct normalization ruling; migration 0002 applied (approved) | Positive, negative, ambiguous and invalid tests on synthetic local data; staging test with owner-approved records |
| P2 writes | 2.2 and 2.5: enrollment and event tables, idempotency store, revision lock, audit. Concurrency tests | Body schemas, event catalogue, transitions, writer decision, DPO field review | Duplicate and concurrent-write tests pass on local MySQL |
| P2 reads | 2.3, 2.4, 2.6 with keyset pagination and binding filters in SQL | P2 writes; history boundary ruling | Cross-partner isolation tests (owner, other partner, missing, malformed) pass |
| P3 hardening | Per-client quotas, shared rate limiter, OAuth2 client credentials, retention jobs, metrics | Infra choice | Load test and ODS §19 P1 items closed |
