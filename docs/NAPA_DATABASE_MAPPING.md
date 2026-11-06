# NAPA database mapping and implementation plan

Date: 2026-10-30. Status: household/family, IP null handling and PSGC code policy confirmed by Martin on 2026-10-30; live export not yet implemented.

## Evidence and limits

Read-only connection succeeded using the existing staging runtime configuration. Configured target: `10.10.10.133:3306 / dswd_4ps_tuloy_dev`, through the configured SSH tunnel. This differs from earlier notes naming 10.255.9.165 / ppisv2-v3Master. The follow-up probe confirms server hostname ppisv2-v3Master; deployed-process configuration still requires release verification.

Evidence: `napa-db-metadata.json` (87 column definitions and two complete view definitions); `napa-db-metadata-detail.json` (source columns, declared foreign keys and sex reference values); `napa-db-lookups.json` (extension/IP candidates). Queries used information_schema plus the non-personal sex reference table in READ ONLY transactions, rolled back. No member records were fetched. Existence and datatype are verified; completeness, join cardinality and business meaning remain untested.

Template: Second Brain `00-Inbox/NAPA Template - NAPA_V2_RAW_DETAILED.csv`, SHA256 `6e3b5e10ca3ff6f8e2f16d5efd4b59f28d4d44d23344802293d9acd968285902`. Header-only copy: `templates/napa-detail-headers.csv`. The ZIP reference implementation is a proposal, not proof of current schema or approval.

## All 20 columns

Aliases: m=enroll_matched_CHED_tbl, v=vw_verify_member_bene, p=tbl_person, r=tbl_hh_roster, h=tbl_household, bgy=lib_psgc_barangay, g=lib_psgc_denorm, fa=tbl_family_assoc_ops, sx=sex_tbl.

| # | Template field | Actual source / proposed mapping | Readiness |
|---|---|---|---|
| 1 | REGION | v.region_name, originating g.region_name | Source verified; nullable, DQA required |
| 2 | REGION(PSGC) | g.region_psa_code, char(10) | Use stored 10-digit PSGC code, not region_id |
| 3 | PROVINCE | v.province_name / g.province_name | Source verified; nullable |
| 4 | PROVINCE(SURROGATE KEY PSGC)) | g.province_psa_code, char(10) | Martin selected PSGC code, not province_id |
| 5 | MUNICIPALITY | v.city_name / g.city_name | Source verified; nullable |
| 6 | MUNICIPALITY(SURROGATE KEY PSGC)) | g.city_psa_name, char(10) | Stored code column; 200/200 sampled values are 10-digit codes |
| 7 | BARANGAY | v.barangay_name / bgy.name | Source verified; nullable |
| 8 | BRGY(SURROGATE KEY PSGC)) | bgy.psa_code, char(10); g.brgy_psa_code cross-check | Use PSGC, not barangay_id; 200/200 sample codes agree |
| 9 | HHID | h.household_id converted explicitly to text | Confirmed by Martin; do not use listahanan_id |
| 10 | FamilyID | h.household_id converted to text when a matching active family association exists; otherwise NULL | Martin confirmed both ID columns use household_id and missing associations may remain NULL; never output CBMS family_id |
| 11 | MemberID | m.member_id -> p.person_id; same mapping in v.member_id | Structural mapping verified; confirm receiver expects person_id rather than p.newmember_id |
| 12 | MEMBER Last name | p.last_name / v.last_name | Verified source; nullable, not m.mm_last_name |
| 13 | MEMBER First name | p.first_name / v.first_name | Verified source; not mother's name |
| 14 | MEMBER Middle name | p.middle_name / v.middle_name | Verified source; approve blank handling |
| 15 | Ext. name | p.ext_name_id -> lib_ext_name.ext_name_id -> name (copied from consultant_ods.lib_ext_name_v3) | Lookup found; all populated sample IDs resolve. NULL remains absent, unknown IDs block. Confirm source ownership before deployment |
| 16 | Sex FEMALE / MALE | p.sex_id -> lib_sex.sex_id -> name (copied from consultant_ods.lib_sex_v3) | Correct ODS candidate: 1=MALE, 2=FEMALE, 3=NONE (not exportable). All 200 sample IDs resolve. Do NOT use local sex_tbl's incompatible 0/1 meanings |
| 17 | IP YES / NO | Member-level p.ip_group_id with lib_ip_group (copied from the ODS V3 lookup) | Martin: 0=NO, NULL stays NULL, recognized nonzero group=YES. No silent household fallback |
| 18 | Date of Birth | p.date_of_birth / v.date_of_birth, DATE | Source/type verified; serialize DD-MM-YYYY; reject invalid or ineligible values |
| 19 | VALIDATED SOURCE OF ORIGIN | m.agency_id -> lib_partner_agency.agency_id, agency_alias; current flow is CHED | Candidate provenance exists; validate populated values; do not label every row CHED merely from table name or infer TESDA |
| 20 | CATEGORY TK / TA / TT | Validated batch category, frozen when created | Application-owned decision; approve category/eligibility rules; never infer from agency alone |

## Join strategy and important trap

The existing matched view joins m to v on member_id. v joins p -> r on person_id -> h on household_id -> bgy on barangay_id -> g on barangay_id. These relationships are established by the live view definition, but not all have declared foreign keys.

**Do not use v as an unconditional historical source.** Its WHERE clause filters client status, grade level and age using CURDATE (18 to under 31). A previously matched member can disappear from v as time or status changes. Its roster/beneficiary joins may also multiply a person. Decide whether batch eligibility intentionally uses that current filter. Prefer a new explicitly scoped source projection over changing legacy views; retain matched-row identity and document the selected household/roster episode.

Family candidate: fa.person_id plus fa.hh_roster_id links to selected p/r; retain only the owner-approved association, with explicit archived handling. tbl_family_selected is a separate candidate, not an interchangeable fallback. Never resolve multiple candidates using arbitrary MIN, MAX, DISTINCT, or the first row.

Keep m.id, enrollment_id, member_id, household/roster identifiers, provenance and source timestamp in the snapshot metadata. Resolve one permitted snapshot row per approved batch identity; policy must decide whether repeat enrollments for a person are allowed. Cast integer identifiers to strings only after selecting the correct source; never regenerate leading zeros lost in numeric storage.

## Recommended delivery order

1. **Finish the source contract.** Apply Martin's confirmed household/family, IP null and PSGC rules below. Verify the deployed API target and required lookup privileges; finish category, repeat-enrollment and eligibility rules plus other nullable fields. Record the mapping version and remaining disclosure approvals.
2. **Profile using counts only.** After target confirmation, run bounded read-only aggregate checks: source coverage; duplicate m.id/person/roster associations; unmapped sex/agency; missing names/DOB/IDs/codes; code lengths and mismatches; FamilyID multiplicity. Capture query plans before heavy scans and choose DBA-approved windows. Do not retrieve member samples in logs. Acceptance: cardinality rules and thresholds explicitly met or exclusions reported.
3. **Build and test the source adapter.** Add an explicit column projection and parameterized filters in the API. Preserve existing views. Reject ambiguous mappings with structured field errors without PII. Synthetic fixtures cover absent lookups, multiple families/rosters, historical age changes, nullable names, agency provenance and 9/10-digit code confusion. Do not connect NAPA to the payroll CSV: its contract is different.
4. **Persist BCMD batches and frozen member snapshots.** Follow spec 02 Revision 4's six-table design and durable audit/approval records; creation must be transactional and idempotent. Reconcile actual migration files and staging ledger before assigning numbers: the spec proposes 0007 app_settings and 0008-0013 BCMD, while another local _bcmd worktree contains an untracked 0007_bcmd_batches.sql. These are conflicting proposals, not authority to apply either. No ledger rewrite or changes to Wilson's Keycloak branches.
5. **Expose batch APIs, then wire the UI.** Proposed contracts (not existing endpoints): POST /bcmd/batches (Idempotency-Key), GET /bcmd/batches, GET /bcmd/batches/{id}, GET /bcmd/batches/{id}/members with bounded pagination, and GET /bcmd/batches/{id}/export?format=napa. Use the approved staff URL prefix and client allow-list when implemented. API must enforce approved DSWD authorization, not just sidebar visibility. Replace in-memory BCMD saves only after persistence tests pass; retry/refresh must retain the batch.
6. **Enable DETAIL export after gates pass.** Use frozen snapshots and exact approved header/version. Existing local serializer is formatting-only; 38 synthetic tests passed. Add golden CSV contract tests, formula-injection, Unicode, quoting, leading zeros, DOB and deterministic ordering tests. Complete spec gates HEADER_FORMAT, COLUMN_MAPPING, DQA_RULES, DPO_DISCLOSURE and PAYROLL_SEPARATION. Validate stored hashes, audit downloads, enforce retention/purge concurrency rules. SUMMARY stays unavailable until totals are defined. NAPA export must never mark a payment paid.
7. **Acceptance and release.** Integration tests: create -> list -> retrieve -> export, duplicate submission, rollback on failure, denied agency/account, stale approvals, expiry/revocation, purge versus download and repeat export byte equality. Browser tests verify actual agency header, BCMD persistence and readable failures. Test synthetic DB first; reviewed migration/deployment and real-data acceptance remain separate approvals.

## Current local work

API: C:/Projects/4ps/tuloy/new/_napa-format, branch feat/napa-detail-format based on 66c85e2. Formatter, mapping helpers and local-only persistent batch APIs are implemented; no live NAPA export or migrations run. See BCMD_LOCAL_CANDIDATE.md.
Web: C:/Projects/4ps/tuloy/new/_agency-web, branch feat/agency-aware-titles based on c2c2d04. Signed-in title/header/profile now derives a display label from active grant agencies, with explicit multi-agency/unknown handling. This label does not prove employment affiliation and does not change authorization. Login branding remains institutional branding.

Nothing pushed, no real export produced, no database changes.

## Read-only source evidence (2026-10-30)

`napa-source-probe.json` records repeatable read-only metadata, reference and first-200
matched-row aggregate checks from `scripts/napa_source_probe.py`. The server reports
hostname `ppisv2-v3Master`, MySQL 8.0.43, database `dswd_4ps_tuloy_dev`, consistent
with earlier staging identity. This does not prove which configuration the deployed
API process currently uses.

**Correction to the initial sex mapping:** sex_tbl exists but is incompatible with
ODS person sex IDs. Using it would mislabel ID 1 and fail ID 2. The ODS V3 library
has 1=MALE, 2=FEMALE, 3=NONE. The sample contains 76 ID-1 and 124 ID-2 records;
all match the V3 lookup. NONE must block a FEMALE/MALE-only export.

The ODS extension library contains 18 suffix definitions. Every populated suffix
ID in the sample resolves. A missing suffix is distinct from an invalid lookup ID.
The unrelated partner/web-profile suffix fields are not needed as fallbacks.

All 200 sampled rows have a person, household and one roster. Their Listahanan IDs
are present. All four proposed geographic code fields are 10-digit values; barangay
codes agree between the two libraries. This is technical evidence for the candidate
mapping, not approval of NAPA's use of codes versus surrogate keys or a population
quality assessment. Names and DOB are populated in the sample.

**Family join resolved technically:** start from roster household ->
tbl_family_selected.household_id; join the selected family ID to
tbl_family_assoc_ops.family_id and verify person_id, hh_roster_id and archived=0.
EXPLAIN shows existing indexed lookups (approximately five association rows per
family), avoiding a direct per-person scan. The first attempt at a direct person
association scan hit the 5-second cap (3024); it was not retried. No index was added.
The indexed candidate gives one selected family in all 200 rows, with a matching
active person/roster association in 195. Five have no matching association and use NULL under Martin's decision below. No arbitrary
first-family selection is used.

**Lookups (0027):** the API reads the three lookups from `lib_sex`, `lib_ext_name` and
`lib_ip_group` in its own database, never from consultant_ods. They are copied, with the
same ids, names, codes and flags, by `docs/schema/bcmd_lookup_copy.sql` (see
docs/bcmd-napa-workflow.md). The evidence below was gathered on the consultant_ods originals.

**IP source evidence:** of 200 source people, 12 have a nonzero
ID recognized by consultant_ods.lib_ip_group_v3, 19 have ID 0 without a library entry,
and 169 have NULL. Household ip_label contains group-like IDs, not simply a boolean;
one person with NULL membership has a household group ID. Neither NULL nor 0 has a
documented NO meaning in the inspected metadata. Martin subsequently defined 0 as NO and NULL as NULL. Person/household membership
cannot silently substitute for each other. The reference-list capture is capped at 200; coverage aggregates
join the actual complete reference table.

Local `napa_mapping.py` now resolves ODS sex and suffix references, checks family
associations, and applies IP 0=NO / NULL=NULL with an explicit source selection. It rejects ambiguous,
inactive or unknown mappings without exposing raw values in errors. Mapping and
serializer tests: 59 passed. These helpers expose no live export route and do not
authorize disclosure.


## Martin's final mapping decisions (2026-10-30; supersede earlier open questions)

- Missing active family associations are allowed as NULL. Keep the member in the
  snapshot and export an empty FamilyID cell; do not exclude the member.
- HHID and non-null FamilyID both use tbl_household.household_id, not Listahanan ID
  or the CBMS family identifier. The family association proves presence only.
- IP 0 means NO; NULL stays NULL (an empty CSV cell). A recognized nonzero group
  maps to YES. The mapping helper still requires the caller to choose the source
  explicitly; the member projection uses person membership without household fallback.
- Geographic fields use PSGC codes. The verified current-schema tables are
  lib_psgc_barangay and lib_psgc_denorm; tbl_psgc_brgy was not found in this schema.
  Stored PSA code fields are char(10). The formatter validates ten ASCII digits
  and preserves zeros; it never emits internal geographic keys or pads bad codes.
- NULL is a data value in the future snapshot, never the literal text "NULL" in CSV.
  Only family_id and ip_indicator have newly approved default nullability. Suffix
  and other nullable fields still require explicit handling in the source contract.

Local implementation: napa_mapping.py and napa_detail.py now apply these decisions.
72 synthetic tests pass, including missing-family preservation, IP NULL versus NO,
household ID selection and invalid-code rejection. No database changes were made.

The approved family/IP/code rules no longer block mapping. Persistent batches, authenticated read/create APIs and frontend integration are now
implemented as a local-only candidate. Production migrations, export approvals,
download/purge flows and release acceptance remain; see BCMD_LOCAL_CANDIDATE.md.
