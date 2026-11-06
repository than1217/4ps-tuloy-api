-- Retain historical native and labelled system events; attribute SSO events exactly.
ALTER TABLE `bcmd_audit_event`
  ADD COLUMN `actor_staff_user_id` bigint unsigned NULL AFTER `actor_api_user_id`,
  ADD CONSTRAINT `fk_bcmd_audit_staff_actor` FOREIGN KEY (`actor_staff_user_id`) REFERENCES `staff_users` (`id`) ON DELETE RESTRICT,
  DROP CHECK `ck_bcmd_audit_actor`,
  ADD CONSTRAINT `ck_bcmd_audit_actor` CHECK (
    NOT (`actor_api_user_id` IS NOT NULL AND `actor_staff_user_id` IS NOT NULL)
    AND (`actor_api_user_id` IS NOT NULL OR `actor_staff_user_id` IS NOT NULL OR `actor_label` IS NOT NULL));
