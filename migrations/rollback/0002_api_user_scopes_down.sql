-- Rollback for 0002 (not picked up by scripts/migrate.py: it reads migrations/*.sql only).
-- Run with the admin account only after revoking access at the route level is no longer needed:
-- dropping the table makes every scoped route deny (the API treats a missing table as no grants).
-- It DESTROYS grant history: export it first (scripts/grants.py list, or a SELECT into a file).
DROP TABLE IF EXISTS `api_user_scopes`;
DELETE FROM `schema_migrations` WHERE `version` = '0002';
