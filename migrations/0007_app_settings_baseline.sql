-- 0007: app_settings baseline from Martin's complete SHOW CREATE TABLE, 2026-10-30.
-- Existing tables require exact-schema verification before this no-op baseline.
-- No settings rows or existing AUTO_INCREMENT state are changed.
CREATE TABLE IF NOT EXISTS `app_settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_name` varchar(255) NOT NULL,
  `app_tagline` varchar(255) DEFAULT NULL,
  `app_subtext` varchar(500) DEFAULT NULL,
  `app_logo_url` longtext,
  `app_logo_fit` varchar(20) DEFAULT NULL,
  `app_logo_height` int DEFAULT NULL,
  `app_logo_sidebar_height` int DEFAULT NULL,
  `favicon_url` longtext,
  `bg_type` varchar(20) NOT NULL,
  `bg_color` varchar(255) DEFAULT NULL,
  `bg_image_url` longtext,
  `bg_overlay_color` varchar(255) DEFAULT NULL,
  `bg_overlay_opacity` int DEFAULT NULL,
  `text_title_color` varchar(255) DEFAULT NULL,
  `text_tagline_color` varchar(255) DEFAULT NULL,
  `text_subtext_color` varchar(255) DEFAULT NULL,
  `logos_json` longtext,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_app_settings_id` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
