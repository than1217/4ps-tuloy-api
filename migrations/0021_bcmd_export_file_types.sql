-- NAPA-05: three exports per batch (failed records with remarks, NAPA detailed, NAPA V2
-- summary), each tied to the DQA run it was built from. Legacy DETAIL exports keep a NULL
-- run. One completed file per run and type keeps re-downloads immutable.
ALTER TABLE `bcmd_batch_export`
  ADD COLUMN `dqa_run_id` bigint unsigned NULL AFTER `batch_id`,
  ADD UNIQUE KEY `uq_bcmd_export_run_type` (`dqa_run_id`, `file_type`),
  ADD CONSTRAINT `fk_bcmd_export_dqa_run` FOREIGN KEY (`dqa_run_id`) REFERENCES `bcmd_dqa_run` (`id`) ON DELETE RESTRICT,
  DROP CHECK `ck_bcmd_export_file`,
  ADD CONSTRAINT `ck_bcmd_export_file` CHECK (`file_type` IN ('DETAIL','SUMMARY','FAILED'));
