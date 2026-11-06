-- 0027: BCMD reference lookups in the application database.
-- The batch source query reads sex, name extension and IP group names from these tables, so the
-- API reads nothing outside its own database. Ids, names, codes and flags match the PPIS v3
-- libraries consultant_ods.lib_sex_v3, lib_ext_name_v3 and lib_ip_group_v3 (same column types);
-- their PPIS audit columns are not kept. Additive: this only creates the tables. Their rows are
-- copied after it with docs/schema/bcmd_lookup_copy.sql; BCMD stays not ready while one is empty.
CREATE TABLE `lib_sex` (
  `sex_id` int unsigned NOT NULL,
  `name` varchar(50) DEFAULT NULL,
  `code` varchar(15) DEFAULT NULL,
  `archived` tinyint unsigned DEFAULT NULL,
  PRIMARY KEY (`sex_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE `lib_ext_name` (
  `ext_name_id` int NOT NULL,
  `name` varchar(50) DEFAULT NULL,
  `archived` tinyint unsigned DEFAULT NULL,
  PRIMARY KEY (`ext_name_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE `lib_ip_group` (
  `ip_group_id` int unsigned NOT NULL,
  `name` varchar(50) DEFAULT NULL,
  `code` varchar(15) DEFAULT NULL,
  `status` int unsigned DEFAULT NULL,
  `archived` tinyint unsigned DEFAULT NULL,
  PRIMARY KEY (`ip_group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
