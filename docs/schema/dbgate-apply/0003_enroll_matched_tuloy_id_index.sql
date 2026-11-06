-- Guarded DBGate step 0003: 0003_enroll_matched_tuloy_id_index.sql
-- DDL is from that migration; text checksum 2d4a394479c46a4d7bc6dbfdefb3e3b37b2d692f2973539fb0e249bf2beac275.
-- Run this WHOLE file in a clean autocommit session. Stop on any SQL error or STOP result.
-- This file changes schema and INSERTs one ledger row only after matching postflight.
-- No existing ledger row is updated or deleted. AUTO_INCREMENT counters and row data are not reset.
SET @napa_pre_ok=0, @napa_post_ok=0, @napa_locked=0, @napa_need_ddl=0;
SET @napa_target_ok=(DATABASE()='dswd_4ps_tuloy_dev' AND @@hostname='ppisv2-v3Master' AND @@autocommit=1);
SET @napa_locked=IF(@napa_target_ok, GET_LOCK('tuloy_schema_migrate',0),0);
SET SESSION lock_wait_timeout=10;
SET SESSION max_execution_time=5000;
SET @napa_pre_ok=(@napa_target_ok AND @napa_locked=1 AND ((SELECT COUNT(*) FROM schema_migrations WHERE 1=1)=4
 AND (SELECT COUNT(*) FROM schema_migrations WHERE 1=1 AND BINARY version <=> BINARY '0001' AND BINARY name <=> BINARY '0001_baseline.sql' AND BINARY checksum <=> BINARY '2f66d78675dc741081d3995121fd1d302e03212da0fc7cf77528c7f3e963d72e')=1
 AND (SELECT COUNT(*) FROM schema_migrations WHERE 1=1 AND BINARY version <=> BINARY '0002' AND BINARY name <=> BINARY '0002_api_user_scopes.sql' AND BINARY checksum <=> BINARY '9867704136d773881803ad725e1707d30415ff0848267c38dad443b948c4540f')=1
 AND (SELECT COUNT(*) FROM schema_migrations WHERE 1=1 AND BINARY version <=> BINARY '0004' AND BINARY name <=> BINARY '0004_api_user_identities.sql' AND BINARY checksum <=> BINARY 'e2959efc709c173fbee5e8b15ad53ecd58ed0e31534f8e8dfd17ae3cd1f53947')=1
 AND (SELECT COUNT(*) FROM schema_migrations WHERE 1=1 AND BINARY version <=> BINARY '0005' AND BINARY name <=> BINARY '0005_staff_users.sql' AND BINARY checksum <=> BINARY '91bad111c68d501bb6413545da1f9be2a3ee6ebeda2c4a7d34b05fdefed1707c')=1) AND ((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id')=0 OR ((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id')=1
 AND (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id' AND BINARY INDEX_NAME <=> BINARY 'idx_enroll_matched_tuloy_id' AND NON_UNIQUE <=> 1 AND SEQ_IN_INDEX <=> 1 AND BINARY COLUMN_NAME <=> BINARY 'tuloy_id' AND SUB_PART <=> NULL AND BINARY COLLATION <=> BINARY 'A' AND BINARY INDEX_TYPE <=> BINARY 'BTREE' AND BINARY IS_VISIBLE <=> BINARY 'YES' AND EXPRESSION <=> NULL)=1)));
SET @napa_need_ddl=((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id')=0);
SET @napa_sql=IF(@napa_pre_ok AND @napa_need_ddl,
 'CREATE INDEX `idx_enroll_matched_tuloy_id` ON `enroll_matched_CHED_tbl` (`tuloy_id`) ALGORITHM=INPLACE LOCK=NONE', 'SELECT ''No DDL executed - inspect final step_status'' AS ddl_note');
PREPARE napa_step FROM @napa_sql;
EXECUTE napa_step;
DEALLOCATE PREPARE napa_step;
SET @napa_post_ok=(((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id')=1
 AND (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id' AND BINARY INDEX_NAME <=> BINARY 'idx_enroll_matched_tuloy_id' AND NON_UNIQUE <=> 1 AND SEQ_IN_INDEX <=> 1 AND BINARY COLUMN_NAME <=> BINARY 'tuloy_id' AND SUB_PART <=> NULL AND BINARY COLLATION <=> BINARY 'A' AND BINARY INDEX_TYPE <=> BINARY 'BTREE' AND BINARY IS_VISIBLE <=> BINARY 'YES' AND EXPRESSION <=> NULL)=1));
INSERT INTO schema_migrations(version,name,checksum)
SELECT '0003','0003_enroll_matched_tuloy_id_index.sql','2d4a394479c46a4d7bc6dbfdefb3e3b37b2d692f2973539fb0e249bf2beac275'
WHERE @napa_pre_ok AND @napa_post_ok AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version='0003');
SELECT '0003' AS version,
 CASE WHEN @napa_pre_ok AND @napa_post_ok AND (SELECT COUNT(*) FROM schema_migrations WHERE version='0003' AND name='0003_enroll_matched_tuloy_id_index.sql' AND checksum='2d4a394479c46a4d7bc6dbfdefb3e3b37b2d692f2973539fb0e249bf2beac275')=1 THEN 'VERIFIED'
      WHEN @napa_target_ok AND @napa_locked=1 AND @napa_post_ok AND (SELECT COUNT(*) FROM schema_migrations WHERE version='0003' AND name='0003_enroll_matched_tuloy_id_index.sql' AND checksum='2d4a394479c46a4d7bc6dbfdefb3e3b37b2d692f2973539fb0e249bf2beac275')=1 THEN 'ALREADY_RECORDED'
      ELSE 'STOP' END AS step_status,
 @napa_target_ok AS target_ok,@napa_pre_ok AS preflight_ok,@napa_post_ok AS postflight_ok;
SELECT IF(@napa_locked=1,RELEASE_LOCK('tuloy_schema_migrate'),0) AS migration_lock_released;
