-- Phase 1 Contract v1.1 P1-A：墨相师候选理解池 + 构建邀请 + journey_stage + profile_dimension
-- 两张新表通过 CREATE TABLE IF NOT EXISTS 定义（与 ai_schema.py 同源；旧库无表时
-- bootstrap 的 AI_TABLES 也会创建，本迁移执行可幂等通过 1050 重复键错误）。
-- ai_profile_session.journey_stage、ai_profile_draft_field.profile_dimension、
-- ai_profile_revision_field.profile_dimension 通过 ALTER TABLE 补列。
-- 存量行 profile_dimension 保持 NULL（P1-A 不进行猜测式 AI 回填）。

CREATE TABLE IF NOT EXISTS `ai_profile_candidate` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
    `candidate_id` varchar(64) NOT NULL,
    `session_id` varchar(64) NOT NULL,
    `user_id` bigint unsigned NOT NULL,
    `subject` varchar(32) NOT NULL COMMENT 'personal/ideal_partner',
    `profile_dimension` varchar(64) NOT NULL COMMENT 'Contract v1.1 §1.3 六维之一',
    `field_kind` varchar(16) NOT NULL COMMENT 'structured/entry',
    `field_key` varchar(64) DEFAULT NULL,
    `category` varchar(64) DEFAULT NULL,
    `content` text DEFAULT NULL,
    `value_json` json DEFAULT NULL,
    `confidence` decimal(5,4) NOT NULL,
    `source_turn_ids` json NOT NULL COMMENT '产生该候选的 turn_id 列表，用于证据回溯',
    `source_span` varchar(512) DEFAULT NULL,
    `consent_version` varchar(32) NOT NULL,
    `policy_revision` varchar(64) NOT NULL,
    `status` varchar(16) NOT NULL DEFAULT 'active' COMMENT 'active/promoted/dismissed/expired',
    `content_hash` char(64) NOT NULL,
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_ai_profile_candidate_id` (`candidate_id`),
    UNIQUE KEY `uk_candidate_session_hash` (`session_id`, `content_hash`),
    KEY `idx_candidate_session_status` (`session_id`, `status`),
    KEY `idx_candidate_session_dimension` (`session_id`, `profile_dimension`, `confidence`),
    KEY `idx_candidate_user_subject` (`user_id`, `subject`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='墨相师候选理解池';

CREATE TABLE IF NOT EXISTS `ai_profile_build_invite` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
    `invite_id` varchar(96) NOT NULL,
    `session_id` varchar(64) NOT NULL,
    `user_id` bigint unsigned NOT NULL,
    `subject` varchar(32) NOT NULL COMMENT 'personal/ideal_partner',
    `status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/accepted/snoozed/expired',
    `trigger_kind` varchar(16) NOT NULL DEFAULT 'auto' COMMENT 'auto/manual',
    `invite_no` int unsigned NOT NULL,
    `summary_json` json NOT NULL,
    `effective_turn_count_at_create` int unsigned NOT NULL,
    `dimension_count` int unsigned NOT NULL,
    `candidate_count` int unsigned NOT NULL,
    `snoozed_at_effective_turn_count` int unsigned DEFAULT NULL,
    `accepted_at` datetime DEFAULT NULL,
    `snoozed_at` datetime DEFAULT NULL,
    `expired_at` datetime DEFAULT NULL,
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `active_slot` tinyint GENERATED ALWAYS AS (
        CASE WHEN `status` = 'pending' THEN 1 ELSE NULL END
    ) STORED,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_ai_profile_build_invite_id` (`invite_id`),
    UNIQUE KEY `uk_ai_profile_build_invite_no` (`session_id`, `invite_no`),
    UNIQUE KEY `uk_ai_profile_build_invite_pending` (`session_id`, `active_slot`),
    KEY `idx_ai_profile_build_invite_owner` (`user_id`, `subject`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='墨相师构建邀请';

ALTER TABLE `ai_profile_session`
    ADD COLUMN `journey_stage` enum('chatting','building','ready','published')
        NOT NULL DEFAULT 'chatting'
        COMMENT 'chatting/building/ready/published（Contract v1.1）';

ALTER TABLE `ai_profile_draft_field`
    ADD COLUMN `profile_dimension` varchar(64) DEFAULT NULL
        COMMENT 'Contract v1.1 §1.3 六维之一；旧字段保持 NULL 不参与完整度'
        AFTER `field_kind`,
    ADD KEY `idx_draft_field_dimension` (`draft_id`, `profile_dimension`);

ALTER TABLE `ai_profile_revision_field`
    ADD COLUMN `profile_dimension` varchar(64) DEFAULT NULL
        COMMENT 'Contract v1.1 §1.3 六维之一；旧字段保持 NULL 不参与完整度'
        AFTER `field_kind`,
    ADD KEY `idx_revision_field_dimension` (`revision_id`, `profile_dimension`);