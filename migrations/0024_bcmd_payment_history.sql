-- 0024: payment-history safeguards for payment generation (docs/bcmd-napa-workflow.md,
-- "Payment history"). Additive: two new BCMD-owned tables. No payments-owned table, existing
-- row, result, export or payment is changed. Apply before the API that uses it.
-- One row per beneficiary that payment generation has locked. The key is a SHA-256 digest,
-- never a member ID. Generation locks these rows in key order inside its transaction, so two
-- generations for the same beneficiary (any term or assistance type) run one after the other.
CREATE TABLE `bcmd_payment_member_lock` (
  `member_key` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`member_key`),
  CONSTRAINT `ck_bcmd_payment_member_lock_key` CHECK (CHAR_LENGTH(`member_key`) = 64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- Why a PASS member was not paid: finding code, payment reference and line number only.
-- Written in the generation transaction; no beneficiary value is stored.
CREATE TABLE `bcmd_payment_exclusion` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `batch_id` bigint unsigned NOT NULL,
  `stage` varchar(16) NOT NULL,
  `request_id` varchar(64) NOT NULL,
  `line_no` int unsigned NOT NULL,
  `finding_code` varchar(32) NOT NULL,
  `payment_ref` varchar(64) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  KEY `ix_bcmd_payment_exclusion_batch` (`batch_id`, `id`),
  CONSTRAINT `fk_bcmd_payment_exclusion_batch` FOREIGN KEY (`batch_id`) REFERENCES `bcmd_batch` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_bcmd_payment_exclusion_stage` CHECK (`stage` IN ('GENERATION')),
  CONSTRAINT `ck_bcmd_payment_exclusion_fields` CHECK (`finding_code` <> '' AND `payment_ref` <> '' AND `request_id` <> '')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
