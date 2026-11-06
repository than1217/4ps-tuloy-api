-- 0010: bcmd_dqa_issue, local BCMD candidate based on reviewed spec Revision 4.
-- Proposed for release; do not apply remotely without exact-schema/target verification.
CREATE TABLE IF NOT EXISTS `bcmd_dqa_issue` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `batch_id` bigint unsigned NOT NULL,
  `member_id` bigint unsigned DEFAULT NULL,
  `member_key` bigint unsigned GENERATED ALWAYS AS (IFNULL(`member_id`, 0)) STORED NOT NULL,
  `rule_code` varchar(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `field_no` tinyint unsigned NOT NULL DEFAULT 0,
  `severity` varchar(5) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_bcmd_issue` (`batch_id`, `member_key`, `field_no`, `rule_code`),
  KEY `ix_bcmd_issue_rule` (`batch_id`, `rule_code`),
  CONSTRAINT `fk_bcmd_issue_member` FOREIGN KEY (`batch_id`, `member_id`) REFERENCES `bcmd_batch_member` (`batch_id`, `id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_bcmd_issue_batch` FOREIGN KEY (`batch_id`) REFERENCES `bcmd_batch` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_issue_sev` CHECK (`severity` IN ('WARN','BLOCK'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
