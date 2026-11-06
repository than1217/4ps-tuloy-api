-- Rollback for 0025 (not picked up by scripts/migrate.py). Roll back the API first.
-- Limits: the restored checks FAIL while any TESDA or MIXED batch or TESDA member exists. Keep the
-- release rolled forward, or archive and purge those batches as an audited admin action first.
-- Dropping payment_excluded_count discards the save-time payment exclusion counts.
-- The statements are not atomic. If one fails, fix the cause and re-run the remaining ones.
ALTER TABLE `bcmd_batch`
  DROP CHECK `ck_bcmd_batch_source`,
  DROP COLUMN `payment_excluded_count`,
  MODIFY COLUMN `source_agency` char(4) NOT NULL DEFAULT 'CHED',
  ADD CONSTRAINT `ck_bcmd_batch_source` CHECK (`source_agency` = 'CHED');
ALTER TABLE `bcmd_batch_member`
  DROP CHECK `ck_bcmd_member_source`,
  DROP INDEX `uq_bcmd_member_source`,
  DROP COLUMN `course_type`,
  DROP COLUMN `source_agency`;
DELETE FROM `schema_migrations` WHERE `version` = '0025';
