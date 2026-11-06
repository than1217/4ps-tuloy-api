-- 0023: reviewer decisions for duplicate selections (SELECTED_IN_OTHER_BATCH).
-- See docs/bcmd-duplicate-selection-review.md. Additive: three new tables, two counters on
-- runs, one epoch on batches and wider CHECK lists. Existing rows, results, exports and
-- audit events are unchanged. Apply before the API that uses it; runtime never runs DDL.
-- One conflict per claim identity; current_resolution_id is the single effective decision.
CREATE TABLE `bcmd_claim_conflict` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `public_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `member_ref` varchar(32) NOT NULL,
  `academic_year` varchar(20) NOT NULL DEFAULT '',
  `semester` int NOT NULL DEFAULT 0,
  `version` int unsigned NOT NULL DEFAULT 0,
  `current_resolution_id` bigint unsigned DEFAULT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_bcmd_conflict_public` (`public_id`),
  UNIQUE KEY `uq_bcmd_conflict_claim` (`member_ref`, `academic_year`, `semester`),
  KEY `ix_bcmd_conflict_current` (`id`, `current_resolution_id`),
  CONSTRAINT `ck_bcmd_conflict_ref` CHECK (`member_ref` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- Append-only decision history. (conflict_id, version) keeps it linear under concurrency.
CREATE TABLE `bcmd_claim_resolution` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `public_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `conflict_id` bigint unsigned NOT NULL,
  `version` int unsigned NOT NULL,
  `action` varchar(8) NOT NULL,
  `retained_batch_id` bigint unsigned DEFAULT NULL,
  `retained_member_id` bigint unsigned DEFAULT NULL,
  `reverses_resolution_id` bigint unsigned DEFAULT NULL,
  `reason` varchar(500) NOT NULL,
  `actor_api_user_id` int DEFAULT NULL,
  `actor_staff_user_id` bigint unsigned DEFAULT NULL,
  `actor_label` varchar(64) NOT NULL,
  `actor_agency` varchar(16) NOT NULL,
  `request_id` varchar(64) NOT NULL,
  `idempotency_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `request_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_bcmd_resolution_public` (`public_id`),
  UNIQUE KEY `uq_bcmd_resolution_version` (`conflict_id`, `version`),
  UNIQUE KEY `uq_bcmd_resolution_conflict` (`conflict_id`, `id`),
  UNIQUE KEY `uq_bcmd_resolution_reversal` (`conflict_id`, `reverses_resolution_id`),
  UNIQUE KEY `uq_bcmd_resolution_api_idem` (`actor_api_user_id`, `idempotency_key`),
  UNIQUE KEY `uq_bcmd_resolution_staff_idem` (`actor_staff_user_id`, `idempotency_key`),
  KEY `ix_bcmd_resolution_retained` (`retained_batch_id`, `retained_member_id`),
  CONSTRAINT `fk_bcmd_resolution_conflict` FOREIGN KEY (`conflict_id`) REFERENCES `bcmd_claim_conflict` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_resolution_retained` FOREIGN KEY (`retained_batch_id`, `retained_member_id`) REFERENCES `bcmd_batch_member` (`batch_id`, `id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_resolution_reverses` FOREIGN KEY (`conflict_id`, `reverses_resolution_id`) REFERENCES `bcmd_claim_resolution` (`conflict_id`, `id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_resolution_api_actor` FOREIGN KEY (`actor_api_user_id`) REFERENCES `api_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_resolution_staff_actor` FOREIGN KEY (`actor_staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_resolution_action` CHECK (
    (`action` = 'RESOLVE' AND `retained_batch_id` IS NOT NULL AND `retained_member_id` IS NOT NULL AND `reverses_resolution_id` IS NULL)
    OR (`action` = 'REVERSE' AND `retained_batch_id` IS NULL AND `retained_member_id` IS NULL AND `reverses_resolution_id` IS NOT NULL)),
  CONSTRAINT `ck_bcmd_resolution_actor` CHECK ((`actor_api_user_id` IS NOT NULL) + (`actor_staff_user_id` IS NOT NULL) = 1),
  CONSTRAINT `ck_bcmd_resolution_fields` CHECK (`version` > 0 AND CHAR_LENGTH(`reason`) >= 10 AND `request_id` <> '' AND `actor_label` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- Selections a RESOLVE covered when recorded; at most one RETAINED row per decision.
CREATE TABLE `bcmd_claim_resolution_member` (
  `resolution_id` bigint unsigned NOT NULL,
  `batch_id` bigint unsigned NOT NULL,
  `member_id` bigint unsigned NOT NULL,
  `decision` varchar(8) NOT NULL,
  `retained_flag` tinyint GENERATED ALWAYS AS (IF(`decision` = 'RETAINED', 1, NULL)) STORED,
  PRIMARY KEY (`resolution_id`, `member_id`),
  UNIQUE KEY `uq_bcmd_resolution_member_retained` (`resolution_id`, `retained_flag`),
  KEY `ix_bcmd_resolution_member_lookup` (`batch_id`, `member_id`),
  CONSTRAINT `fk_bcmd_resolution_member_resolution` FOREIGN KEY (`resolution_id`) REFERENCES `bcmd_claim_resolution` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_resolution_member_member` FOREIGN KEY (`batch_id`, `member_id`) REFERENCES `bcmd_batch_member` (`batch_id`, `id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_resolution_member_decision` CHECK (`decision` IN ('RETAINED','EXCLUDED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- The effective decision may only point at a decision of the same conflict.
ALTER TABLE `bcmd_claim_conflict`
  ADD CONSTRAINT `fk_bcmd_conflict_current` FOREIGN KEY (`id`, `current_resolution_id`) REFERENCES `bcmd_claim_resolution` (`conflict_id`, `id`) ON DELETE RESTRICT;
-- Each decision bumps the epoch of every affected batch; a run records the epoch it started with.
ALTER TABLE `bcmd_batch` ADD COLUMN `claim_epoch` int unsigned NOT NULL DEFAULT 0;
ALTER TABLE `bcmd_dqa_run`
  ADD COLUMN `excluded_count` int unsigned NOT NULL DEFAULT 0,
  ADD COLUMN `claim_epoch` int unsigned NOT NULL DEFAULT 0,
  DROP CHECK `ck_dqa_run_counts`,
  ADD CONSTRAINT `ck_dqa_run_counts` CHECK (`processed` = `passed` + `failed` + `review_count` + `excluded_count` AND `processed` <= `total`);
ALTER TABLE `bcmd_dqa_result`
  DROP CHECK `ck_dqa_result_outcome`,
  ADD CONSTRAINT `ck_dqa_result_outcome` CHECK (`outcome` IN ('PASS','FAIL','REVIEW','EXCLUDED'));
ALTER TABLE `bcmd_audit_event`
  DROP CHECK `ck_bcmd_audit_type`,
  ADD CONSTRAINT `ck_bcmd_audit_type` CHECK (`event_type` IN
 ('BATCH_CREATED','DQA_COMPLETED','EXPORT_STARTED','EXPORT_COMPLETED','EXPORT_FAILED',
  'EXPORT_ABORTED','DOWNLOAD_ADMITTED','DOWNLOAD_COMPLETED','DOWNLOAD_FAILED',
  'DOWNLOAD_UNCERTAIN','ACCESS_DENIED','TAMPER_DETECTED','BATCH_PURGED',
  'DQA_RUN_QUEUED','DQA_RUN_COMPLETED','DQA_RUN_FAILED',
  'CLAIM_RESOLVED','CLAIM_REVERSED','DQA_RUN_INVALIDATED'));
