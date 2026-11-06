# Guarded DBGate apply package

## 0023–0026: duplicate review, payment history, CHED/TESDA, NAPA handoff (prepared 2026-11-04)

Run these after 0018–0022 are VERIFIED. Nothing in these files has been run on
staging by the assistant. They need the API built from the BCMD/NAPA alignment
branch; the API now on staging does not use them.

**Before you start**

- The ledger must end at 0022 with its recorded checksum. Each file checks the
  rows before it, so a file run early or twice returns STOP or ALREADY_RECORDED
  and changes nothing.
- Stop the DQA worker and pause batch creation and payment generation.
- Back up the `bcmd_*` tables. Use the same Workbench settings as for 0018–0022
  (read timeout 600, `USE ...; SET SQL_SAFE_UPDATES = 0;`, Execute All).

| File | Effect |
|---|---|
| 0023 | Duplicate-selection review: `bcmd_claim_conflict`, `bcmd_claim_resolution`, `bcmd_claim_resolution_member`, run counters, the batch claim epoch and wider CHECK lists |
| 0024 | Payment generation safeguards: `bcmd_payment_member_lock` (one SHA-256 digest per beneficiary, never a member ID) and `bcmd_payment_exclusion` (skipped members: reason code, line number, payment row reference) |
| 0025 | CHED and TESDA in one batch: per-member `source_agency` and `course_type` (defaults CHED / degree, so existing members are unchanged), batch `source_agency` CHED, TESDA or MIXED, nullable `payment_excluded_count` |
| 0026 | NAPA handoff table: `bcmd_napa_handoff` (one row per run recorded by older clients, never updated or deleted) and the `NAPA_HANDOFF_RECORDED` audit event. The API no longer requires a handoff |

No payments-owned or external table is changed: `payment_transaction` is only read.

**Account rights** (the DBA grants them; these files do not change grants):

- BCMD runtime account: SELECT, INSERT, UPDATE on `bcmd_claim_conflict`; SELECT,
  INSERT on `bcmd_claim_resolution`, `bcmd_claim_resolution_member` and
  `bcmd_napa_handoff`; SELECT on `payment_transaction` and `enroll_matched_TESDA_tbl`.
- DQA worker: SELECT on `payment_transaction` and `enroll_matched_TESDA_tbl`,
  plus the claim-table rights above.
- Payments (ODS/app) account: SELECT, INSERT, UPDATE on `bcmd_payment_member_lock`;
  SELECT, INSERT on `bcmd_payment_exclusion`; SELECT on `bcmd_batch`,
  `bcmd_batch_member`, `bcmd_dqa_run`, `bcmd_dqa_result`, `bcmd_claim_conflict` and
  `bcmd_claim_resolution_member`.

Without a read right the routes fail closed: BCMD returns 503 `BCMD_NOT_READY`
and payment history reads return 503 `PAYMENT_HISTORY_UNAVAILABLE`.

**After 0026**

1. Deploy the API and restart it together with the DQA worker; deploy the web last.
2. `GET /v1/api/tuloy/staff/bcmd/status` should return `available: true`.
3. No approvals and no handoff step: a batch is in NAPA as soon as its latest
   DQA run completes. A batch whose latest run used an older ruleset needs a
   fresh DQA run in BCMD before new files. Issued files stay downloadable.

**Rollback** (`migrations/rollback/`, API first, then 0026 → 0023; not atomic, so
fix a failed statement and re-run the rest):

- 0026 stops at its first statement once any handoff has been recorded, so
  recorded handoffs are never dropped. Keep the release rolled forward then.
- 0025 refuses while any TESDA or MIXED batch or TESDA member exists.
- 0024 drops the generation exclusion audit: export it first.
- 0023 refuses while any EXCLUDED result, excluded count or review audit event
  exists, and dropping its tables discards every review decision.

## 0018–0022: DQA runs and NAPA files (prepared 2026-11-04)

The API that needs these tables is already on staging (API MR !28). Until 0022
returns VERIFIED, every BCMD request returns 503 `BCMD_NOT_READY`. The rest of
the API is unaffected. Nothing in these files has been run on staging by the
assistant.

**Before you start**

- The ledger must hold exactly 0001–0017. Staging recorded 0017 (COR review)
  through the migration runner, so 0017 is not in this package. With any other
  ledger, 0018 returns STOP and changes nothing.
- Back up the `bcmd_*` tables, because 0020 renames rows.
- Keep the DQA worker stopped. New batches can't be created while BCMD is
  unavailable, so 0020 already runs with batch creation paused.
- In MySQL Workbench:
  1. Raise Preferences → SQL Editor → *DBMS connection read timeout* to 600
     and reconnect. On 2026-11-04 the default 30 seconds dropped the
     connection during the shape check in 0019, after its change but before
     its ledger row was written. Running the file again recorded it.
  2. Put `USE dswd_4ps_tuloy_dev; SET SQL_SAFE_UPDATES = 0;` on top of each
     file.
  3. Use Execute All, and read the result tab that has `step_status`.

**Run these files in order.** For each one, open the whole file in a clean
autocommit connection and run it, using the same rules as the older files
below. Go on to the next file only after `step_status` is VERIFIED.

| File | Effect |
|---|---|
| 0018 | Create `bcmd_dqa_run` (run progress) and `bcmd_dqa_result` (result per member) |
| 0019 | Allow the three DQA-run audit event types. This drops and re-adds the event-type CHECK; existing events are unchanged |
| 0020 | Rename later duplicate batch names: the oldest batch keeps its name, later copies get ` #<id>` appended. Then add the unique index on the name |
| 0021 | Link NAPA exports to their DQA run and allow DETAIL, SUMMARY and FAILED files. This rebuilds the small `bcmd_batch_export` table |
| 0022 | Add the duplicate-claim lookup index on frozen members |

Each of these files can hold several statements. A statement runs only while
its table still has the exact shape it had before that statement. If a file
stops on an SQL error partway through:

1. Keep the output.
2. Close the connection to release the migration lock.
3. After review, run the same file again. It continues with the remaining
   statements and ends with VERIFIED.

Running a finished file again returns ALREADY_RECORDED.

Rollback scripts exist for 0020–0022 in `migrations/rollback/`. There are none
for 0018 and 0019. Roll back the API before the database.

**After 0022**

1. **Account rights:** the BCMD account needs SELECT, INSERT and UPDATE on
   `bcmd_dqa_run`, and SELECT and INSERT on `bcmd_dqa_result`. That is already
   covered if it holds rights on the whole database. These files do not change
   grants.
2. **Check:** no API restart is needed, because readiness is checked on every
   request. `GET /v1/api/tuloy/staff/bcmd/status` should return
   `available: true`.
3. **Worker:** start the DQA worker service.
4. **Approvals:** none. The export approval gates were removed on 2026-11-05.

**Local rehearsal** (`tests/test_bcmd_mysql.py`, synthetic MySQL) covers:

- a missing 0017 ledger row, which gives STOP;
- files run out of order, which gives STOP;
- an interruption after the first statement of 0018 and of 0019, recovered by
  running the file again;
- duplicate names renamed by 0020;
- running a finished file again, which gives ALREADY_RECORDED;
- a final ledger and table shape identical to what the API readiness check
  requires.

The 0003–0016 files and `expected_schema.json` regenerate byte-for-byte
unchanged.

---

Update 2026-10-31: Martin authorized and the assistant applied actor upgrades
0014-0016 on the verified staging target. All three returned VERIFIED; existing
batch/export/audit rows were unchanged. See ../../BCMD_SSO_ACTORS.md for evidence,
encrypted backup location and the API deployment still required. The original
0003-0013 preparation instructions below are historical; do not replay them on
the upgraded schema.

Target confirmed by Martin on 2026-10-30:
`dswd_4ps_tuloy_dev` on `ppisv2-v3Master`.

These files **change staging schema and add migration-ledger rows** when their
guards pass. They do not change grant assignments, beneficiary rows or settings
values, and do not deploy/activate the local NAPA code. Nothing here has been run
on staging by the assistant.

## How to run

For the SSO actor upgrade on a database already verified through 0013, start
with **0014**, then 0015 and 0016. Do not rerun old CREATE-table steps after
upgrade. Follow ../../BCMD_SSO_ACTORS.md for the maintenance/deployment sequence.

Use a clean staging query connection, with autocommit enabled and no open
transaction. Open **one complete file** from this folder in the SQL editor and run
all statements in that file on the same connection. Do not combine all files.
Do not use the plain migrations/ SQL or bcmd_candidate.sql as substitutes.

Start with **0003_enroll_matched_tuloy_id_index.sql**. Inspect the row containing
`step_status`:

- `VERIFIED`: this step's schema and recorded migration checksum match.
- `ALREADY_RECORDED`: matching schema and ledger row already exist; no duplicate
  ledger row was inserted.
- `STOP`, or any SQL error: **do not continue**. Save the output for review. If an
  error interrupted the file before lock release, close that query connection to
  release its session-owned migration lock before a reviewed retry.

Only after a verified result, proceed in order:

| File prefix | Effect |
|---|---|
| 0003 | Add the existing migration's online, nonunique Tuloy-ID index |
| 0006 | Allow NULL grant expiry; preserve existing dates and assignments |
| 0007 | Record the exact existing settings baseline; preserve settings and counter |
| 0008 | Create batch headers |
| 0009 | Create frozen members, including nullable IP |
| 0010 | Create validation issues |
| 0011 | Create the future export-version ledger |
| 0012 | Create durable audit events |
| 0013 | Create the future approval ledger |
| 0014 | Add the distinct SSO batch creator FK and retry-key uniqueness |
| 0015 | Add the distinct SSO export creator FK and retry-key uniqueness |
| 0016 | Add the distinct SSO audit actor FK; preserve native/system events |

## Guards and recovery

Each file checks the exact target, autocommit, the shared migration lock and the
complete expected prior ledger. New tables are allowed only if absent or already
an exact metadata match. The existing settings table must match before adoption.
Columns, nullability/defaults, charsets/collations, generated expressions, indexes,
foreign keys/rules and CHECKs are compared. Volatile AUTO_INCREMENT counters are
not reset or treated as schema drift.

The ledger INSERT is conditional on preflight and postflight success. No existing
ledger row is updated or deleted. Exact schema already committed before a crash
can be verified and recorded by the same guarded retry. An incompatible object
produces STOP instead of being silently accepted by IF NOT EXISTS.

MySQL DDL autocommits: **ROLLBACK does not undo an applied CREATE/ALTER/INDEX**.
If a file errors after DDL, keep its state and output for review rather than
dropping tables, manually changing ledger history or running the next file.
Future populated-table recovery must preserve snapshots/audit evidence. The
existing 0006 rollback refuses to restore NOT NULL when null-expiry rows exist.

The recorded checksum belongs to the original numbered migration file. Wrapper
file hashes are separately listed in package_hashes.json. The generator is
scripts/build_napa_dbgate_package.py and only connects to the isolated local test
container when building these wrappers.

Local rehearsal covered wrong target, mismatched settings schema, all nine steps,
DDL-before-ledger crash recovery, safe repeated steps and settings preservation.
This is schema preparation only: CSV release, production API readiness, runtime
privileges and the later reviewed code deployment remain separate work.
