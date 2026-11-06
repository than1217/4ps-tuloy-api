-- 0012: bcmd_audit_event, local BCMD candidate based on reviewed spec Revision 4.
-- Proposed for release; do not apply remotely without exact-schema/target verification.
CREATE TABLE IF NOT EXISTS `bcmd_audit_event` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `occurred_at` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `event_type` varchar(24) NOT NULL,
  `batch_id` bigint unsigned DEFAULT NULL,
  `export_id` bigint unsigned DEFAULT NULL,
  `parent_event_id` bigint unsigned DEFAULT NULL,
  `actor_api_user_id` int DEFAULT NULL,
  `actor_label` varchar(64) DEFAULT NULL,
  `actor_agency` varchar(16) DEFAULT NULL,
  `request_id` varchar(64) NOT NULL,
  `client_ip` varchar(45) DEFAULT NULL,
  `reason_code` varchar(32) DEFAULT NULL,
  `sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_bcmd_audit_result` (`parent_event_id`),
  KEY `ix_bcmd_audit_batch` (`batch_id`, `id`),
  KEY `ix_bcmd_audit_pending` (`event_type`, `occurred_at`),
  CONSTRAINT `fk_bcmd_audit_batch` FOREIGN KEY (`batch_id`) REFERENCES `bcmd_batch` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_audit_export` FOREIGN KEY (`export_id`) REFERENCES `bcmd_batch_export` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_audit_parent` FOREIGN KEY (`parent_event_id`) REFERENCES `bcmd_audit_event` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_audit_actor` FOREIGN KEY (`actor_api_user_id`) REFERENCES `api_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_audit_type` CHECK (`event_type` IN ('BATCH_CREATED','DQA_COMPLETED','EXPORT_STARTED','EXPORT_COMPLETED','EXPORT_FAILED','EXPORT_ABORTED','DOWNLOAD_ADMITTED','DOWNLOAD_COMPLETED','DOWNLOAD_FAILED','DOWNLOAD_UNCERTAIN','ACCESS_DENIED','TAMPER_DETECTED','BATCH_PURGED')),
  CONSTRAINT `ck_bcmd_audit_actor` CHECK (`actor_api_user_id` IS NOT NULL OR `actor_label` IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
