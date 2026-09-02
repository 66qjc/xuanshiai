-- Phase 3 P3-01 — 成稿预览(ai_profile_preview)
--
-- 用途:绑定 draft + expected_revision 的预览生成任务记录。
-- 状态机(active / confirmed / stale / failed)用于让"这就是我"发布时验证
-- 用户看到的预览与正式版本一致(防止 version mismatch)。
--
-- 与 Phase 1 P1-A 的 ai_profile_draft / ai_profile_revision 共存,不修改它们。
-- 旧客户端不传 preview 仍可走 ai_profile.py 原发布流程。

CREATE TABLE IF NOT EXISTS `ai_profile_preview` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `preview_id` varchar(96) NOT NULL COMMENT '服务端稳定 ID,前端轮询 GET 用',
  `draft_id` varchar(64) NOT NULL,
  `expected_revision` int unsigned NOT NULL COMMENT '乐观锁,绑定 draft.expected_revision',
  `user_id` bigint unsigned NOT NULL,
  `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
  `content` mediumtext NOT NULL COMMENT '预览正文(narrative + 维度摘要)',
  `status` enum('active','confirmed','stale','failed') NOT NULL DEFAULT 'active',
  `task_id` varchar(96) DEFAULT NULL COMMENT 'profile_preview worker task_id',
  `last_error` varchar(512) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_profile_preview_id` (`preview_id`),
  UNIQUE KEY `uk_ai_profile_preview_draft_revision` (`draft_id`, `expected_revision`),
  KEY `idx_ai_profile_preview_user_subject` (`user_id`, `subject`, `status`),
  KEY `idx_ai_profile_preview_draft_status` (`draft_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='墨相师独立画像预览(Phase 3 P3-01)';