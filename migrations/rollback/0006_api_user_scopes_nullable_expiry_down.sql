-- Rollback for 0006 (was 0004) (not picked up by scripts/migrate.py: it reads migrations/*.sql only).
-- Run with the admin account, statement by statement, stopping at the first error.
-- Step 1 FAILS (error 3819) while any non-expiring grant row exists. This guard matters: without
-- it MySQL would silently turn each NULL into a zero or current timestamp when expires_at becomes
-- NOT NULL again. Revoking such a row does not clear its NULL. Export the history first, then give
-- each remaining NULL row an explicit date as an audited admin edit
-- (UPDATE api_user_scopes SET expires_at = <date> WHERE expires_at IS NULL) and re-run.
ALTER TABLE `api_user_scopes` ADD CONSTRAINT `ck_api_user_scopes_rollback_guard` CHECK (`expires_at` IS NOT NULL);
ALTER TABLE `api_user_scopes`
  DROP CHECK `ck_api_user_scopes_rollback_guard`,
  DROP CHECK `ck_api_user_scopes_expiry`,
  MODIFY COLUMN `expires_at` timestamp NOT NULL,
  ADD CONSTRAINT `ck_api_user_scopes_expiry` CHECK (`expires_at` > `granted_at`);
DELETE FROM `schema_migrations` WHERE `version` = '0006';
