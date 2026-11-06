-- Rollback for 0026 (not picked up by scripts/migrate.py). Roll back the API first: the earlier API
-- creates NAPA files without a handoff.
-- Limits: dropping bcmd_napa_handoff discards every recorded handoff (keep a copy for audit first).
-- The restored audit check FAILS while any NAPA_HANDOFF_RECORDED event exists; those events are
-- audit history and are not deleted here. Keep the release rolled forward in that case.
-- The statements are not atomic. If one fails, fix the cause and re-run the remaining ones.
ALTER TABLE `bcmd_audit_event`
  DROP CHECK `ck_bcmd_audit_type`,
  ADD CONSTRAINT `ck_bcmd_audit_type` CHECK (`event_type` IN
 ('BATCH_CREATED','DQA_COMPLETED','EXPORT_STARTED','EXPORT_COMPLETED','EXPORT_FAILED',
  'EXPORT_ABORTED','DOWNLOAD_ADMITTED','DOWNLOAD_COMPLETED','DOWNLOAD_FAILED',
  'DOWNLOAD_UNCERTAIN','ACCESS_DENIED','TAMPER_DETECTED','BATCH_PURGED',
  'DQA_RUN_QUEUED','DQA_RUN_COMPLETED','DQA_RUN_FAILED',
  'CLAIM_RESOLVED','CLAIM_REVERSED','DQA_RUN_INVALIDATED'));
DROP TABLE IF EXISTS `bcmd_napa_handoff`;
DELETE FROM `schema_migrations` WHERE `version` = '0026';
