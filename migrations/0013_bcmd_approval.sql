-- 0013: bcmd_approval, local BCMD candidate based on reviewed spec Revision 4.
-- Proposed for release; do not apply remotely without exact-schema/target verification.
CREATE TABLE IF NOT EXISTS `bcmd_approval` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `gate_code` varchar(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `decision` varchar(8) NOT NULL,
  `approver_role` varchar(24) NOT NULL,
  `approver_label` varchar(64) NOT NULL,
  `reference` varchar(120) NOT NULL,
  `approved_on` date NOT NULL,
  `artifact_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `recorded_at` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `ix_bcmd_approval_gate` (`gate_code`, `id`),
  CONSTRAINT `ck_bcmd_approval_gate` CHECK (`gate_code` IN ('HEADER_FORMAT','COLUMN_MAPPING','DQA_RULES','DPO_DISCLOSURE','PAYROLL_SEPARATION')),
  CONSTRAINT `ck_bcmd_approval_decision` CHECK (`decision` IN ('APPROVED','REVOKED')),
  CONSTRAINT `ck_bcmd_approval_role` CHECK (
    (`gate_code` = 'HEADER_FORMAT' AND `approver_role` = 'BCMD_OWNER') OR
    (`gate_code` = 'COLUMN_MAPPING' AND `approver_role` IN ('DBA','DATA_OWNER')) OR
    (`gate_code` = 'DQA_RULES' AND `approver_role` = 'BCMD_OWNER') OR
    (`gate_code` = 'DPO_DISCLOSURE' AND `approver_role` = 'DPO') OR
    (`gate_code` = 'PAYROLL_SEPARATION' AND `approver_role` = 'MARTIN')),
  CONSTRAINT `ck_bcmd_approval_artifact` CHECK (`gate_code` = 'PAYROLL_SEPARATION' OR `artifact_sha256` IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
