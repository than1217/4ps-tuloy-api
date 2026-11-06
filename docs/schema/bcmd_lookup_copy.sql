-- Copy the BCMD reference lookups from consultant_ods into dswd_4ps_tuloy_dev (after 0027).
-- Run this WHOLE file in Workbench with dswd_4ps_tuloy_dev as the default schema, in a clean
-- autocommit session, after docs/schema/dbgate-apply/0027_bcmd_lookup_tables.sql reported VERIFIED.
-- Reads consultant_ods.lib_sex_v3, lib_ext_name_v3 and lib_ip_group_v3; writes only lib_sex,
-- lib_ext_name and lib_ip_group. Nothing in consultant_ods is changed. The API itself never reads
-- consultant_ods: this file is the only place that does.
-- Re-runnable: missing ids are added and changed names, codes and flags refreshed. No row is
-- deleted, so a lookup removed from the source makes copy_status STOP (decide by hand).
-- Either node of the staging multi-master cluster may run it.
-- Stop on any SQL error. copy_status is COPIED only when every table equals its source.
SET @copy_target_ok=(DATABASE()='dswd_4ps_tuloy_dev' AND @@hostname IN ('ppisv2-v3Master','ppisv2-v3slaveG13') AND @@autocommit=1);
SET @copy_ready=(@copy_target_ok AND (SELECT COUNT(*) FROM schema_migrations WHERE version='0027')=1);
SET @copy_sql=IF(@copy_ready,
 'INSERT INTO `lib_sex` (`sex_id`,`name`,`code`,`archived`) SELECT * FROM (SELECT `sex_id`,`name`,`code`,`archived` FROM `consultant_ods`.`lib_sex_v3`) AS src ON DUPLICATE KEY UPDATE `name`=src.`name`,`code`=src.`code`,`archived`=src.`archived`',
 'SELECT ''lib_sex not copied - inspect copy_status'' AS copy_note');
PREPARE copy_step FROM @copy_sql;
EXECUTE copy_step;
DEALLOCATE PREPARE copy_step;
SET @copy_sql=IF(@copy_ready,
 'INSERT INTO `lib_ext_name` (`ext_name_id`,`name`,`archived`) SELECT * FROM (SELECT `ext_name_id`,`name`,`archived` FROM `consultant_ods`.`lib_ext_name_v3`) AS src ON DUPLICATE KEY UPDATE `name`=src.`name`,`archived`=src.`archived`',
 'SELECT ''lib_ext_name not copied - inspect copy_status'' AS copy_note');
PREPARE copy_step FROM @copy_sql;
EXECUTE copy_step;
DEALLOCATE PREPARE copy_step;
SET @copy_sql=IF(@copy_ready,
 'INSERT INTO `lib_ip_group` (`ip_group_id`,`name`,`code`,`status`,`archived`) SELECT * FROM (SELECT `ip_group_id`,`name`,`code`,`status`,`archived` FROM `consultant_ods`.`lib_ip_group_v3`) AS src ON DUPLICATE KEY UPDATE `name`=src.`name`,`code`=src.`code`,`status`=src.`status`,`archived`=src.`archived`',
 'SELECT ''lib_ip_group not copied - inspect copy_status'' AS copy_note');
PREPARE copy_step FROM @copy_sql;
EXECUTE copy_step;
DEALLOCATE PREPARE copy_step;
-- Postflight: same number of rows, and every source row has an identical copy.
SET @copy_ok=(@copy_ready
 AND (SELECT COUNT(*) FROM `lib_sex`)=(SELECT COUNT(*) FROM `consultant_ods`.`lib_sex_v3`)
 AND NOT EXISTS (SELECT 1 FROM `consultant_ods`.`lib_sex_v3` s LEFT JOIN `lib_sex` t ON t.`sex_id`=s.`sex_id`
   AND t.`name` COLLATE utf8mb4_0900_bin <=> CONVERT(s.`name` USING utf8mb4) COLLATE utf8mb4_0900_bin
   AND t.`code` COLLATE utf8mb4_0900_bin <=> CONVERT(s.`code` USING utf8mb4) COLLATE utf8mb4_0900_bin
   AND t.`archived` <=> s.`archived` WHERE t.`sex_id` IS NULL)
 AND (SELECT COUNT(*) FROM `lib_ext_name`)=(SELECT COUNT(*) FROM `consultant_ods`.`lib_ext_name_v3`)
 AND NOT EXISTS (SELECT 1 FROM `consultant_ods`.`lib_ext_name_v3` s LEFT JOIN `lib_ext_name` t ON t.`ext_name_id`=s.`ext_name_id`
   AND t.`name` COLLATE utf8mb4_0900_bin <=> CONVERT(s.`name` USING utf8mb4) COLLATE utf8mb4_0900_bin
   AND t.`archived` <=> s.`archived` WHERE t.`ext_name_id` IS NULL)
 AND (SELECT COUNT(*) FROM `lib_ip_group`)=(SELECT COUNT(*) FROM `consultant_ods`.`lib_ip_group_v3`)
 AND NOT EXISTS (SELECT 1 FROM `consultant_ods`.`lib_ip_group_v3` s LEFT JOIN `lib_ip_group` t ON t.`ip_group_id`=s.`ip_group_id`
   AND t.`name` COLLATE utf8mb4_0900_bin <=> CONVERT(s.`name` USING utf8mb4) COLLATE utf8mb4_0900_bin
   AND t.`code` COLLATE utf8mb4_0900_bin <=> CONVERT(s.`code` USING utf8mb4) COLLATE utf8mb4_0900_bin
   AND t.`status` <=> s.`status` AND t.`archived` <=> s.`archived` WHERE t.`ip_group_id` IS NULL));
SELECT 'lib_sex' AS lookup,(SELECT COUNT(*) FROM `consultant_ods`.`lib_sex_v3`) AS source_rows,(SELECT COUNT(*) FROM `lib_sex`) AS copied_rows
UNION ALL SELECT 'lib_ext_name',(SELECT COUNT(*) FROM `consultant_ods`.`lib_ext_name_v3`),(SELECT COUNT(*) FROM `lib_ext_name`)
UNION ALL SELECT 'lib_ip_group',(SELECT COUNT(*) FROM `consultant_ods`.`lib_ip_group_v3`),(SELECT COUNT(*) FROM `lib_ip_group`);
SELECT CASE WHEN @copy_ok THEN 'COPIED' ELSE 'STOP' END AS copy_status,
 @copy_target_ok AS target_ok,@copy_ready AS migration_0027_recorded;
