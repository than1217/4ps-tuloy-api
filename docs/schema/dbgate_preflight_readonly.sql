-- Read-only evidence collection, NOT a migration or an apply script.
-- Run on the intended staging connection in DBGate before finalizing migration numbers.
-- If DBGate stops on any error while the transaction is open, execute ROLLBACK.
START TRANSACTION READ ONLY;

SELECT DATABASE() AS selected_database, @@hostname AS server_hostname,
       @@version AS mysql_version, UTC_TIMESTAMP() AS captured_utc;

SELECT version, name, checksum, applied_at
FROM schema_migrations ORDER BY version;

SELECT TABLE_NAME, ENGINE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA=DATABASE()
  AND (TABLE_NAME='app_settings' OR TABLE_NAME LIKE 'bcmd_%');

SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,
       CHARACTER_SET_NAME,COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='app_settings'
ORDER BY ORDINAL_POSITION;

SELECT TABLE_NAME,INDEX_NAME,COLUMN_NAME,SEQ_IN_INDEX,NON_UNIQUE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='app_settings'
ORDER BY INDEX_NAME,SEQ_IN_INDEX;

ROLLBACK;

-- Optional read-only DDL capture; it may report that app_settings does not exist.
SHOW CREATE TABLE app_settings;
