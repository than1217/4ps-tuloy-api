-- Rollback for 0020 (not picked up by scripts/migrate.py). Roll back the API first.
-- Names renamed by 0020 (" #<id>" suffix) are not restored.
ALTER TABLE `bcmd_batch` DROP KEY `uq_bcmd_batch_name`;
DELETE FROM `schema_migrations` WHERE `version` = '0020';
