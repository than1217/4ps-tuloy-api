# DBGate release preparation

The candidate is implemented and tested locally. **Do not apply
bcmd_candidate.sql to staging yet.** It is an unnumbered six-table candidate,
not the final migration package.

DBGate can execute the reviewed DDL. It does not replace version/checksum tracking
or deploy API/frontend code. Manually creating tables without reconciling the
ledger can cause the next automated migration to fail or mistake a drifted table
for an approved schema.

## Ready now

Run `dbgate_preflight_readonly.sql` on the intended staging connection. It returns
server identity, migration history, existing BCMD tables and app_settings schema
metadata, not beneficiary rows. If a query fails while the read-only transaction
is open, run ROLLBACK. The final SHOW CREATE is optional and runs after ROLLBACK.

An attempted read-only preflight from this development machine on 2026-10-30 failed
at the SSH gateway (10.255.9.40:22, WinError 10060). No ledger query or DDL reached
the database in that attempt. Earlier schema evidence is not a fresh ledger check.

## Finalize after that evidence

1. Match the live ledger to origin/main's exact files/checksums (currently 0001-0006).
   Preserve the Keycloak-owned 0004/0005 identities. Report any drift; do not edit
   applied migration files or rewrite/delete ledger rows.
2. The reviewed plan reserves 0007 for an app_settings baseline and 0008-0013 for
   the six BCMD tables. The older _bcmd worktree's untracked 0007 is a competing
   local proposal, not authority over the live ledger. Leave that worktree alone.
3. Finalize the app_settings baseline from its actual DDL, assign unique BCMD
   filenames, and rehearse each exact file plus recovery on synthetic MySQL.
   IP is now an additional nullable personal snapshot column: account for all
   **18** personal fields in the eventual purge implementation and column grants.
4. Review separate runtime/source/approver/purge privileges, backup/recovery and
   migration preflight/postflight checks. The API contains no DDL or ledger writes.
5. Prepare the exact DBGate SQL and checksum/ledger procedure from those files;
   apply one reviewed file at a time and verify it before proceeding. MySQL DDL
   autocommits, so ROLLBACK alone cannot undo a CREATE TABLE.

## Code activation is separate

The API defaults BCMD_ENABLED=false. This implementation deliberately permits
activation only for local loopback databases named tuloy_napa_* with a separate
write-account configuration. Setting the flag on staging currently fails startup.
That guard must be replaced by the reviewed production readiness/privilege checks
before a later release; creating tables alone cannot activate NAPA.

Local functionality now covers persistent selected-record batches, source
snapshots, validation issues, read APIs and durable create/DQA audit. It does not
implement export approval recording, download audit/hash/retention/purge flows,
or enable CSV release. The export endpoint returns 501. No credentials, grants,
source records or staging schema have been changed by this implementation.


## Update after Martin's DBGate ledger and DDL results (2026-10-30)

The reported applied versions are exactly 0001, 0002, 0004 and 0005. All four
checksums match the local migration files; no Keycloak migration was renumbered or
edited. Proposed numbered files are now prepared: 0007_app_settings_baseline and
0008-0013 for the six BCMD tables. The other worktree's old 0007 proposal remains
untouched. The snapshot includes IP as its eighteenth nullable personal field.

The exact local apply sequence has passed a real-MySQL rehearsal, including the
original settings row/schema/counter and existing idx_member_id preservation:
explicit backfill 0003, then 0006, then 0007 through 0013 one file at a time.
Checksums are recorded in migration_manifest.json. The settings baseline was also
verified on a fresh table against Martin's complete SHOW CREATE TABLE output.

Martin confirmed database dswd_4ps_tuloy_dev and hostname ppisv2-v3Master.
Guarded wrappers are now prepared under dbgate-apply and have passed a local
MySQL rehearsal, including wrong-target and schema-drift stops. No remote apply
has been performed; the local-only API activation/export limitations above remain. Keep bcmd_candidate.sql as the design reference, not a manual apply shortcut.


## Operator-reported staging completion (2026-10-30)

Martin supplied the final staging ledger for dswd_4ps_tuloy_dev on
ppisv2-v3Master. All 13 recorded checksums, 0001-0013, match the current local
migration files. Evidence is docs/schema/staging-ledger-verified-20261030.json
(relative to the API repository). This confirms the supplied migration history;
it does not independently verify current table contents or deploy/enable code.
No additional schema apply is requested. Historical pending-migration statements
above describe the pre-apply state. Application release limitations remain.
