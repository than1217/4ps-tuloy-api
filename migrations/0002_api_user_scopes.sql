-- 0002: per-user scope grants (ADR 2026-10-26-tuloy-api-main-rbac-direct-scope-grants).
-- Each row is one grant: a (scope, partner_id, agency) tuple for one api_users row, with a
-- mandatory expiry. The API reads active grants on every request; JWT claims carry no authority.
--
-- History is the table itself: rows are never deleted or edited. Revoking sets revoked_at and
-- revoked_by once (scripts/grants.py); a later regrant is a new row. The runtime accounts get
-- SELECT only on this table; grant changes go through the admin account.
--
-- Active-grant uniqueness: MySQL has no partial unique index. is_active is 1 for unrevoked rows
-- and NULL for revoked ones; a UNIQUE index allows many NULLs, so the composite key below admits
-- one unrevoked row per (user, scope, partner, agency) and any number of revoked ones. The real
-- columns are indexed (not a concatenation), so no two distinct tuples can collide.

CREATE TABLE IF NOT EXISTS `api_user_scopes` (
  `grant_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `scope` varchar(64) NOT NULL,
  `partner_id` varchar(64) NOT NULL,
  `agency` varchar(16) NOT NULL,
  `granted_by` varchar(255) NOT NULL,
  `granted_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` timestamp NOT NULL,
  `revoked_at` timestamp NULL DEFAULT NULL,
  `revoked_by` varchar(255) DEFAULT NULL,
  `is_active` tinyint GENERATED ALWAYS AS (IF(`revoked_at` IS NULL, 1, NULL)) STORED,
  PRIMARY KEY (`grant_id`),
  UNIQUE KEY `uq_api_user_scopes_active` (`user_id`, `scope`, `partner_id`, `agency`, `is_active`),
  KEY `ix_api_user_scopes_lookup` (`user_id`, `revoked_at`, `expires_at`),
  CONSTRAINT `fk_api_user_scopes_user` FOREIGN KEY (`user_id`) REFERENCES `api_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_api_user_scopes_expiry` CHECK (`expires_at` > `granted_at`),
  CONSTRAINT `ck_api_user_scopes_revoker` CHECK ((`revoked_at` IS NULL) = (`revoked_by` IS NULL)),
  CONSTRAINT `ck_api_user_scopes_fields` CHECK (`scope` <> '' AND `partner_id` <> '' AND `agency` <> '' AND `granted_by` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
