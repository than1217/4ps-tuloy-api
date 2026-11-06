-- 0025: CHED and TESDA records in one batch (docs/bcmd-napa-workflow.md, "Unified sources").
-- Additive for existing rows: every existing member is a CHED degree record, which the column
-- defaults state; existing batches keep source_agency 'CHED'. Apply before the API that uses it.
-- Per-member provenance; a CHED and a TESDA record with the same source row ID cannot collide.
ALTER TABLE `bcmd_batch_member`
  ADD COLUMN `source_agency` varchar(5) NOT NULL DEFAULT 'CHED',
  ADD COLUMN `course_type` varchar(16) NOT NULL DEFAULT 'degree',
  ADD UNIQUE KEY `uq_bcmd_member_source` (`batch_id`, `source_agency`, `course_type`, `source_record_id`),
  ADD CONSTRAINT `ck_bcmd_member_source` CHECK ((`source_agency` = 'CHED' AND `course_type` = 'degree')
    OR (`source_agency` = 'TESDA' AND `course_type` = 'diploma'));
-- A batch is CHED, TESDA or MIXED; the per-member source is authoritative. payment_excluded_count:
-- records matching a criteria save that payment history excluded at save time. NULL when not
-- counted: explicit-record saves, earlier batches, or a count that did not finish in time.
ALTER TABLE `bcmd_batch`
  MODIFY COLUMN `source_agency` varchar(5) NOT NULL DEFAULT 'CHED',
  ADD COLUMN `payment_excluded_count` int unsigned NULL DEFAULT NULL,
  DROP CHECK `ck_bcmd_batch_source`,
  ADD CONSTRAINT `ck_bcmd_batch_source` CHECK (`source_agency` IN ('CHED', 'TESDA', 'MIXED'));
