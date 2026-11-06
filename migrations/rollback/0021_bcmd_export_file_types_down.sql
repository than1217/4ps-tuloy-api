-- Rollback for 0021 (not picked up by scripts/migrate.py). Roll back the API first.
-- Step 1 FAILS while any export row was made from a DQA run (SUMMARY, FAILED, or a
-- run-based DETAIL with dqa_run_id): the older schema and API cannot represent or verify
-- them. Keep the release rolled forward, or archive and remove those rows (and their
-- audit events) as an audited admin action before retrying.
-- The statements are not atomic. If you stop after step 1, remove the guard with
--   ALTER TABLE `bcmd_batch_export` DROP CHECK `ck_bcmd_export_rollback_guard`;
-- before running the current API again: its readiness check compares the table's check
-- constraints, so while the guard exists every BCMD route returns 503 BCMD_NOT_READY.
ALTER TABLE `bcmd_batch_export` ADD CONSTRAINT `ck_bcmd_export_rollback_guard`
  CHECK (`file_type` = 'DETAIL' AND `dqa_run_id` IS NULL);
ALTER TABLE `bcmd_batch_export`
  DROP CHECK `ck_bcmd_export_rollback_guard`,
  DROP CHECK `ck_bcmd_export_file`,
  DROP FOREIGN KEY `fk_bcmd_export_dqa_run`,
  DROP KEY `uq_bcmd_export_run_type`,
  DROP COLUMN `dqa_run_id`,
  ADD CONSTRAINT `ck_bcmd_export_file` CHECK (`file_type` = 'DETAIL');
DELETE FROM `schema_migrations` WHERE `version` = '0021';
