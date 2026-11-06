-- Keep export retry keys distinct even when native and staff numeric IDs match.
ALTER TABLE `bcmd_batch_export`
  MODIFY COLUMN `created_by_api_user_id` int NULL,
  ADD COLUMN `created_by_staff_user_id` bigint unsigned NULL AFTER `created_by_api_user_id`,
  ADD UNIQUE KEY `uq_bcmd_export_staff_idem` (`batch_id`, `created_by_staff_user_id`, `idempotency_key`),
  ADD CONSTRAINT `fk_bcmd_export_staff_creator` FOREIGN KEY (`created_by_staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  ADD CONSTRAINT `ck_bcmd_export_creator_identity` CHECK ((`created_by_api_user_id` IS NOT NULL) + (`created_by_staff_user_id` IS NOT NULL) = 1);
