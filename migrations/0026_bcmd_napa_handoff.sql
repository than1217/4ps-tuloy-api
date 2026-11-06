-- 0026: durable BCMD-to-NAPA handoff (docs/bcmd-napa-workflow.md, "Handoff to NAPA").
-- Additive. A handoff names the exact DQA run it was recorded against; it is never updated or
-- deleted, so earlier handoffs stay as history after a rerun or a review invalidates them.
-- passed_count: PASS records of that run that no recorded payment blocked at handoff time.
CREATE TABLE `bcmd_napa_handoff` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `public_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `batch_id` bigint unsigned NOT NULL,
  `dqa_run_id` bigint unsigned NOT NULL,
  `claim_epoch` int unsigned NOT NULL,
  `passed_count` int unsigned NOT NULL,
  `actor_ref` varchar(64) NOT NULL,
  `actor_agency` varchar(16) DEFAULT NULL,
  `request_id` varchar(64) NOT NULL,
  `idempotency_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `request_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_bcmd_handoff_public` (`public_id`),
  UNIQUE KEY `uq_bcmd_handoff_run` (`dqa_run_id`),
  UNIQUE KEY `uq_bcmd_handoff_request` (`batch_id`, `actor_ref`, `idempotency_key`),
  KEY `ix_bcmd_handoff_batch` (`batch_id`, `id`),
  CONSTRAINT `fk_bcmd_handoff_batch` FOREIGN KEY (`batch_id`) REFERENCES `bcmd_batch` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_handoff_run` FOREIGN KEY (`dqa_run_id`) REFERENCES `bcmd_dqa_run` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_handoff_passed` CHECK (`passed_count` > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- Extend the audit vocabulary without rewriting prior events.
ALTER TABLE `bcmd_audit_event`
  DROP CHECK `ck_bcmd_audit_type`,
  ADD CONSTRAINT `ck_bcmd_audit_type` CHECK (`event_type` IN
 ('BATCH_CREATED','DQA_COMPLETED','EXPORT_STARTED','EXPORT_COMPLETED','EXPORT_FAILED',
  'EXPORT_ABORTED','DOWNLOAD_ADMITTED','DOWNLOAD_COMPLETED','DOWNLOAD_FAILED',
  'DOWNLOAD_UNCERTAIN','ACCESS_DENIED','TAMPER_DETECTED','BATCH_PURGED',
  'DQA_RUN_QUEUED','DQA_RUN_COMPLETED','DQA_RUN_FAILED',
  'CLAIM_RESOLVED','CLAIM_REVERSED','DQA_RUN_INVALIDATED','NAPA_HANDOFF_RECORDED'));
