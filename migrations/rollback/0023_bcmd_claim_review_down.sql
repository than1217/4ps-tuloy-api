-- Rollback for 0023 (not picked up by scripts/migrate.py). Roll back the API and stop the DQA
-- worker first. Limits:
-- * The restored CHECK constraints FAIL while any EXCLUDED result, non-zero excluded_count or
--   CLAIM_RESOLVED / CLAIM_REVERSED / DQA_RUN_INVALIDATED audit event exists. Keep the
--   release rolled forward, or archive and remove those rows as an audited admin action.
-- * Dropping the three tables discards every duplicate-selection decision and its history.
--   Export them for the record before running this file.
-- The statements are not atomic. If one fails, the later ones have not run: fix the cause and
-- re-run the remaining statements only. The current API returns 503 BCMD_NOT_READY until
-- either the whole rollback or the whole migration is in place.
ALTER TABLE `bcmd_audit_event`
  DROP CHECK `ck_bcmd_audit_type`,
  ADD CONSTRAINT `ck_bcmd_audit_type` CHECK (`event_type` IN
 ('BATCH_CREATED','DQA_COMPLETED','EXPORT_STARTED','EXPORT_COMPLETED','EXPORT_FAILED',
  'EXPORT_ABORTED','DOWNLOAD_ADMITTED','DOWNLOAD_COMPLETED','DOWNLOAD_FAILED',
  'DOWNLOAD_UNCERTAIN','ACCESS_DENIED','TAMPER_DETECTED','BATCH_PURGED',
  'DQA_RUN_QUEUED','DQA_RUN_COMPLETED','DQA_RUN_FAILED'));
ALTER TABLE `bcmd_dqa_result`
  DROP CHECK `ck_dqa_result_outcome`,
  ADD CONSTRAINT `ck_dqa_result_outcome` CHECK (`outcome` IN ('PASS','FAIL','REVIEW'));
ALTER TABLE `bcmd_dqa_run`
  DROP CHECK `ck_dqa_run_counts`,
  ADD CONSTRAINT `ck_dqa_run_counts` CHECK (`processed` = `passed` + `failed` + `review_count` AND `processed` <= `total`),
  DROP COLUMN `claim_epoch`,
  DROP COLUMN `excluded_count`;
ALTER TABLE `bcmd_batch` DROP COLUMN `claim_epoch`;
ALTER TABLE `bcmd_claim_conflict` DROP FOREIGN KEY `fk_bcmd_conflict_current`;
DROP TABLE `bcmd_claim_resolution_member`;
DROP TABLE `bcmd_claim_resolution`;
DROP TABLE `bcmd_claim_conflict`;
DELETE FROM `schema_migrations` WHERE `version` = '0023';
