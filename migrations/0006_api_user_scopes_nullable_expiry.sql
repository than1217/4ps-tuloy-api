-- 0006: allow non-expiring grants (amendment to ADR 2026-10-26-tuloy-api-main-rbac-direct-scope-grants).
-- Renumbered from 0004 on 2026-10-29: staging (dswd_4ps_tuloy_dev) recorded 0004/0005 from the Keycloak branch
-- (api_user_identities, staff_users), so those files keep their numbers and checksums here.
-- expires_at becomes NULLable: NULL means the grant does not expire. A dated grant must still end
-- after it began. Nothing else changes: revoked_at/revoked_by, the is_active uniqueness and the
-- append-only history stay as in 0002. Existing rows keep their dates. This does not touch account
-- state (api_users.disabled) or the login token lifetime.
-- Authorization must read expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP (security.py).
-- Roll back with migrations/rollback/0006_api_user_scopes_nullable_expiry_down.sql.

ALTER TABLE `api_user_scopes`
  MODIFY COLUMN `expires_at` timestamp NULL DEFAULT NULL,
  DROP CHECK `ck_api_user_scopes_expiry`,
  ADD CONSTRAINT `ck_api_user_scopes_expiry` CHECK (`expires_at` IS NULL OR `expires_at` > `granted_at`)
