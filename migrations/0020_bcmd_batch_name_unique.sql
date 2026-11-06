-- BCMD-06: a batch name may exist only once. The column collation (utf8mb4_0900_ai_ci)
-- makes the index case- and accent-insensitive. The UUID public_id stays the public key.
-- Any pre-existing duplicate names (from before generated names) keep the oldest batch's
-- name; later duplicates get " #<id>" appended so the index can be created. Re-runnable.
UPDATE bcmd_batch b
  JOIN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) AS rn FROM bcmd_batch) ranked
        WHERE ranked.rn > 1) dup ON dup.id = b.id
  SET b.name = CONCAT(LEFT(b.name, 100), ' #', b.id);
ALTER TABLE `bcmd_batch` ADD UNIQUE KEY `uq_bcmd_batch_name` (`name`);
