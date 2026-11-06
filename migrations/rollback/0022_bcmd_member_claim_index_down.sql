-- Rollback for 0022 (not picked up by scripts/migrate.py). Roll back the API first.
ALTER TABLE `bcmd_batch_member` DROP KEY `ix_bcmd_member_claim`;
DELETE FROM `schema_migrations` WHERE `version` = '0022';
