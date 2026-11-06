-- Preserve native creators; SSO creators reference staff_users, never api_users.
-- One atomic ALTER per version. Apply explicitly during the BCMD maintenance window.
ALTER TABLE `bcmd_batch`
  MODIFY COLUMN `created_by_api_user_id` int NULL,
  ADD COLUMN `created_by_staff_user_id` bigint unsigned NULL AFTER `created_by_api_user_id`,
  ADD UNIQUE KEY `uq_bcmd_batch_staff_idem` (`created_by_staff_user_id`, `idempotency_key`),
  ADD CONSTRAINT `fk_bcmd_batch_staff_creator` FOREIGN KEY (`created_by_staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `ck_bcmd_batch_creator_identity` CHECK ((`created_by_api_user_id` IS NOT NULL) + (`created_by_staff_user_id` IS NOT NULL) = 1);
