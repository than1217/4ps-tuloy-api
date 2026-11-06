-- 0001 baseline: application-owned tables exactly as found on dswd_4ps_tuloy_dev (SHOW CREATE TABLE, 2026-10-26).
-- Replaces the CREATE TABLE statements that main.py used to run on every startup.
-- IF NOT EXISTS: on the existing database this is a no-op; it creates the tables on a fresh local database.
-- Note: the old startup DDL also declared a FK from enroll_matched_CHED_tbl.enrollment_id to
-- education_enrollment, but that FK does not exist on the live table, so it is not added here.

CREATE TABLE IF NOT EXISTS `api_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(255) NOT NULL,
  `hashed_password` varchar(255) NOT NULL,
  `disabled` tinyint(1) DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `matching_transaction_CHED_tbl` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `tuloy_id` varchar(100) NOT NULL COMMENT 'id from TULOY',
  `status` varchar(50) NOT NULL COMMENT 'e.g., FOUND, NOT FOUND, ERROR',
  `date_time_matched` datetime DEFAULT CURRENT_TIMESTAMP,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tuloy_id` (`tuloy_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `enroll_matched_CHED_tbl` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `match_id` int unsigned DEFAULT NULL,
  `enrollment_id` int unsigned NOT NULL,
  `tuloy_id` varchar(50) DEFAULT NULL COMMENT 'Unique id across agency pre-generated from TULOY',
  `member_id` int unsigned NOT NULL COMMENT 'Entry_id',
  `student_id` varchar(50) DEFAULT NULL COMMENT 'CHED/Agency student id',
  `mm_first_name` varchar(100) DEFAULT NULL,
  `mm_middle_name` varchar(100) DEFAULT NULL,
  `mm_last_name` varchar(100) DEFAULT NULL,
  `degree_program_name` varchar(150) DEFAULT NULL,
  `degree_date_enrolled` date DEFAULT NULL,
  `degree_program_duration` int DEFAULT NULL COMMENT 'E.g. IT - 4, Accounting - 5',
  `degree_academic_year` varchar(20) DEFAULT NULL,
  `degree_year_level` int DEFAULT NULL,
  `degree_semester` int DEFAULT NULL,
  `enrollment_status` varchar(50) DEFAULT NULL,
  `date_encoded` datetime DEFAULT NULL COMMENT 'When the record encoded to their system',
  `date_updated` datetime DEFAULT NULL COMMENT 'When the record updated to their system',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `fk_matched_enrollment` (`enrollment_id`),
  KEY `fk_matched_transaction` (`match_id`),
  CONSTRAINT `fk_matched_transaction` FOREIGN KEY (`match_id`) REFERENCES `matching_transaction_CHED_tbl` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
