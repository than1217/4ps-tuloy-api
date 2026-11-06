-- Staff identities and grants are independent of api_users and API client grants.
-- The registration API inserts identities only; no permission is granted on login.
CREATE TABLE IF NOT EXISTS `staff_users` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `issuer` varbinary(512) NOT NULL,
  `subject` varbinary(1020) NOT NULL,
  `disabled` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_staff_identity` (`issuer`, `subject`),
  CONSTRAINT `ck_staff_identity` CHECK (`issuer` <> '' AND `subject` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `staff_user_scopes` (
  `grant_id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `staff_user_id` bigint unsigned NOT NULL,
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
  UNIQUE KEY `uq_staff_scope_active` (`staff_user_id`, `scope`, `partner_id`, `agency`, `is_active`),
  KEY `ix_staff_scope_lookup` (`staff_user_id`, `revoked_at`, `expires_at`),
  CONSTRAINT `fk_staff_scope_user` FOREIGN KEY (`staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_staff_scope_expiry` CHECK (`expires_at` > `granted_at`),
  CONSTRAINT `ck_staff_scope_revoker` CHECK ((`revoked_at` IS NULL) = (`revoked_by` IS NULL)),
  CONSTRAINT `ck_staff_scope_fields` CHECK (`scope` <> '' AND `partner_id` <> '' AND `agency` <> '' AND `granted_by` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
