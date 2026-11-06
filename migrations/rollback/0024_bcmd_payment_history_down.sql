-- Rollback for 0024 (not picked up by scripts/migrate.py). Roll back the API first: the current
-- API refuses payment generation (and BCMD returns 503 BCMD_NOT_READY) without these tables.
-- Limits: dropping bcmd_payment_exclusion discards the generation exclusion audit. Export it for
-- the record before running this file. bcmd_payment_member_lock holds only digests.
-- The statements are not atomic. If one fails, fix the cause and re-run the remaining ones.
DROP TABLE `bcmd_payment_exclusion`;
DROP TABLE `bcmd_payment_member_lock`;
DELETE FROM `schema_migrations` WHERE `version` = '0024';
