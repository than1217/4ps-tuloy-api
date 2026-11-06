-- Independent COR decisions. Never updates partner enrollment or payroll tables.
-- Draft: verify migration numbering against the target ledger before applying.
CREATE TABLE `cor_review` (
  `agency` varchar(5) NOT NULL,
  `source_id` bigint unsigned NOT NULL,
  `course_kind` varchar(12) NOT NULL,
  `revision` int unsigned NOT NULL DEFAULT 0,
  `decision` varchar(8) DEFAULT NULL,
  `source_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  PRIMARY KEY (`agency`,`source_id`,`course_kind`),
  CONSTRAINT `ck_cor_source` CHECK ((agency='CHED' AND course_kind='degree') OR (agency='TESDA' AND course_kind IN ('diploma','short_course'))),
  CONSTRAINT `ck_cor_decision` CHECK ((revision=0 AND decision IS NULL AND source_sha256 IS NULL) OR (revision>0 AND decision IN ('APPROVED','DENIED') AND source_sha256 IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `cor_review_event` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `agency` varchar(5) NOT NULL,
  `source_id` bigint unsigned NOT NULL,
  `course_kind` varchar(12) NOT NULL,
  `revision` int unsigned NOT NULL,
  `decision` varchar(8) NOT NULL,
  `reason` varchar(1000) NOT NULL,
  `source_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `actor_api_user_id` int DEFAULT NULL,
  `actor_staff_user_id` bigint unsigned DEFAULT NULL,
  `request_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_cor_revision` (`agency`,`source_id`,`course_kind`,`revision`),
  UNIQUE KEY `uq_cor_request` (`request_key`),
  CONSTRAINT `fk_cor_event_review` FOREIGN KEY (`agency`,`source_id`,`course_kind`) REFERENCES `cor_review` (`agency`,`source_id`,`course_kind`) ON DELETE RESTRICT,
  CONSTRAINT `fk_cor_actor_api` FOREIGN KEY (`actor_api_user_id`) REFERENCES `api_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_cor_actor_staff` FOREIGN KEY (`actor_staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_cor_event_actor` CHECK ((actor_api_user_id IS NULL) <> (actor_staff_user_id IS NULL)),
  CONSTRAINT `ck_cor_event_decision` CHECK (decision IN ('APPROVED','DENIED') AND revision>0 AND CHAR_LENGTH(TRIM(reason))>=3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
