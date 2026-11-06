-- Rollback for 0027 (not picked up by scripts/migrate.py). Roll back the API first: the earlier API
-- reads the lookups from consultant_ods instead.
-- Dropping these tables loses only the copied lookup rows; consultant_ods still holds the source.
-- The statements are not atomic. If one fails, fix the cause and re-run the remaining ones.
DROP TABLE IF EXISTS `lib_ip_group`;
DROP TABLE IF EXISTS `lib_ext_name`;
DROP TABLE IF EXISTS `lib_sex`;
DELETE FROM `schema_migrations` WHERE `version` = '0027';
