"""AI-CORE, M04 profile, M03 search and M06 compatibility table definitions.

These 16 tables are the single authority for the AI profile/search/matchability
pipeline (unified plan ``AI画像-搜索-匹配度统一实施方案-2026-08-07.md`` §10).
The tables back the AI feature gates, consent grants, generic task machine,
provider audit, profile drafts/revisions, search drafts/snapshots, the minimal
feature projection and the compatibility shadow snapshots.

Every table follows the existing idempotent ``CREATE TABLE IF NOT EXISTS``
bootstrap pattern used by ``derivation_schema.py`` and ``business_schema.py``.
JSON columns carry controlled structures only; they never bypass field
constraints and are always expanded through Pydantic schemas before leaving the
service.  No table stores raw provider prompts, provider responses or secrets.
"""

from __future__ import annotations

from typing import Any

AI_TABLES = {
    # ============ AI-CORE（§10.1）============
    "ai_consent_grant": """
        CREATE TABLE IF NOT EXISTS `ai_consent_grant` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned DEFAULT NULL,
            `user_tombstone` char(64) DEFAULT NULL,
            `scope` varchar(64) NOT NULL COMMENT 'profile_text_extract/search_parse/compatibility_shadow/compatibility_display',
            `version` varchar(32) NOT NULL COMMENT '授权文案版本',
            `policy_revision` varchar(64) NOT NULL COMMENT '策略版本，当前冻结 ai-policy-2026-08-07-v1',
            `granted_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `revoked_at` datetime DEFAULT NULL,
            `revoke_reason` varchar(255) DEFAULT NULL,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_consent_user_scope_version` (`user_id`, `scope`, `version`, `granted_at`),
            KEY `idx_ai_consent_user_scope_revoked` (`user_id`, `scope`, `revoked_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 授权授予与撤回记录'
    """,
    "ai_task": """
        CREATE TABLE IF NOT EXISTS `ai_task` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `task_id` varchar(64) NOT NULL COMMENT '对外任务ID',
            `owner_user_id` bigint unsigned DEFAULT NULL,
            `owner_tombstone` char(64) DEFAULT NULL,
            `task_type` varchar(32) NOT NULL COMMENT 'profile_extract/search_parse/search_execute/compatibility/cleanup...',
            `scene` varchar(32) NOT NULL,
            `idempotency_key` varchar(128) NOT NULL,
            `request_digest` char(64) DEFAULT NULL COMMENT '请求摘要哈希，不存原文',
            `status` varchar(24) NOT NULL DEFAULT 'queued' COMMENT 'queued/leased/running/retry_wait/succeeded/failed/cancelled/superseded',
            `stage` varchar(32) DEFAULT NULL,
            `progress_percent` tinyint unsigned DEFAULT NULL COMMENT '0-100 阶段进度，仅展示用途',
            `attempt_count` int unsigned NOT NULL DEFAULT '0',
            `max_attempts` int unsigned NOT NULL DEFAULT '3',
            `next_run_at` datetime DEFAULT NULL,
            `lease_owner` varchar(64) DEFAULT NULL,
            `lease_until` datetime DEFAULT NULL,
            `consent_snapshot_json` json DEFAULT NULL,
            `source_revision_json` json DEFAULT NULL,
            `payload_summary` json DEFAULT NULL COMMENT '仅受控摘要，不含原文',
            `error_code` varchar(64) DEFAULT NULL,
            `error_message` varchar(1000) DEFAULT NULL,
            `result_ref` varchar(128) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            `started_at` datetime DEFAULT NULL,
            `finished_at` datetime DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_task_task_id` (`task_id`),
            UNIQUE KEY `uk_ai_task_owner_type_key` (`owner_user_id`, `task_type`, `idempotency_key`),
            KEY `idx_ai_task_status_next_run` (`status`, `next_run_at`),
            KEY `idx_ai_task_lease_status` (`lease_until`, `status`),
            KEY `idx_ai_task_owner_created` (`owner_user_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 通用任务事实源'
    """,
    "ai_generation_audit": """
        CREATE TABLE IF NOT EXISTS `ai_generation_audit` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `request_id` varchar(64) NOT NULL,
            `task_id` varchar(64) DEFAULT NULL,
            `scene` varchar(32) NOT NULL,
            `provider` varchar(64) NOT NULL,
            `model` varchar(64) DEFAULT NULL,
            `prompt_version` varchar(32) DEFAULT NULL,
            `schema_version` varchar(32) DEFAULT NULL,
            `input_revision_json` json DEFAULT NULL,
            `duration_ms` int unsigned DEFAULT NULL,
            `token_usage_json` json DEFAULT NULL,
            `cost` decimal(10,6) DEFAULT NULL,
            `safety_result_json` json DEFAULT NULL,
            `error_code` varchar(64) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_generation_audit_request_id` (`request_id`),
            KEY `idx_ai_generation_audit_task` (`task_id`, `created_at`),
            KEY `idx_ai_generation_audit_scene` (`scene`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI Provider 调用最小审计，不存原始 prompt/response'
    """,
    # ============ M04 AI 画像（§10.2）============
    "ai_profile_session": """
        CREATE TABLE IF NOT EXISTS `ai_profile_session` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `session_id` varchar(64) NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `input_mode` varchar(16) NOT NULL DEFAULT 'text',
            `status` varchar(24) NOT NULL DEFAULT 'draft' COMMENT 'draft/extracting/awaiting_confirmation/paused/published/failed/cancelled/stale',
            `active_status` tinyint NOT NULL DEFAULT '1' COMMENT '1活动 0已关闭',
            `consent_version` varchar(32) NOT NULL,
            `policy_revision` varchar(64) NOT NULL,
            `current_question_id` varchar(64) DEFAULT NULL,
            `skipped_field_keys` json DEFAULT NULL COMMENT '用户跳过、本次不再追问的字段',
            `profile_revision` int unsigned NOT NULL DEFAULT '0',
            `preference_revision` int unsigned NOT NULL DEFAULT '0',
            `expires_at` datetime DEFAULT NULL,
            `ended_at` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_session_id` (`session_id`),
            `active_slot` tinyint GENERATED ALWAYS AS (CASE WHEN `active_status` = 1 THEN 1 ELSE NULL END) STORED,
            UNIQUE KEY `uk_ai_profile_session_active` (`user_id`, `subject`, `active_slot`),
            KEY `idx_ai_profile_session_user_status` (`user_id`, `status`, `updated_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像会话（personal/ideal_partner）'
    """,
    "ai_profile_turn": """
        CREATE TABLE IF NOT EXISTS `ai_profile_turn` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `turn_id` varchar(128) NOT NULL COMMENT '稳定的服务端 turn ID',
            `session_id` varchar(64) NOT NULL,
            `client_turn_id` varchar(128) NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `turn_no` int unsigned NOT NULL DEFAULT '0',
            `role` varchar(16) NOT NULL DEFAULT 'user' COMMENT 'user/assistant',
            `answer_text` text NOT NULL COMMENT '原始回答，不入普通日志',
            `status` varchar(24) NOT NULL DEFAULT 'saved',
            `source_type` varchar(24) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_turn_id` (`turn_id`),
            UNIQUE KEY `uk_ai_profile_turn_session_client` (`session_id`, `client_turn_id`),
            KEY `idx_ai_profile_turn_session_no` (`session_id`, `turn_no`),
            KEY `idx_ai_profile_turn_user_created` (`user_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像会话原始回答与确认'
    """,
    "ai_profile_draft": """
        CREATE TABLE IF NOT EXISTS `ai_profile_draft` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `draft_id` varchar(64) NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `session_id` varchar(64) DEFAULT NULL,
            `status` varchar(24) NOT NULL DEFAULT 'draft',
            `expected_revision` int unsigned NOT NULL DEFAULT '0' COMMENT '乐观锁，单调递增',
            `consent_snapshot_json` json DEFAULT NULL,
            `policy_revision` varchar(64) NOT NULL,
            `prompt_version` varchar(32) DEFAULT NULL,
            `schema_version` varchar(32) NOT NULL DEFAULT 'profile-extract-v1',
            `published_revision_id` bigint unsigned DEFAULT NULL,
            `last_operation_idempotency_key` varchar(128) DEFAULT NULL,
            `last_operation_request_digest` char(64) DEFAULT NULL,
            `last_operation_response_json` json DEFAULT NULL,
            `expires_at` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_draft_draft_id` (`draft_id`),
            KEY `idx_ai_profile_draft_user_subject` (`user_id`, `subject`, `status`, `updated_at`),
            KEY `idx_ai_profile_draft_session` (`session_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像可编辑草稿版本'
    """,
    "ai_profile_draft_field": """
        CREATE TABLE IF NOT EXISTS `ai_profile_draft_field` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `draft_id` varchar(64) NOT NULL,
            `field_key` varchar(64) NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `field_kind` enum('structured','entry') NOT NULL DEFAULT 'structured' COMMENT 'structured=受控字段/entry=条目（WP-P1，决策D2并存扩展）',
            `category` varchar(32) DEFAULT NULL COMMENT 'entry 分类，服务层校验 9 枚举',
            `content` varchar(200) DEFAULT NULL COMMENT 'entry 正文，≤200 字由服务层双保险',
            `replaces_field_key` varchar(64) DEFAULT NULL COMMENT 'entry 改写指向的被替换条目 field_key',
            `value_json` json DEFAULT NULL,
            `display_value` varchar(500) DEFAULT NULL,
            `source_type` varchar(24) DEFAULT NULL,
            `source_turn_ids` json DEFAULT NULL,
            `source_span` varchar(500) DEFAULT NULL,
            `confidence` decimal(5,4) NOT NULL DEFAULT '0.0000' COMMENT '0..1',
            `visibility` varchar(32) DEFAULT NULL,
            `consent_scope` varchar(64) DEFAULT NULL,
            `schema_version` varchar(32) NOT NULL DEFAULT 'profile-extract-v1',
            `prompt_version` varchar(32) DEFAULT NULL,
            `content_hash` char(64) DEFAULT NULL,
            `confirmation_status` varchar(24) NOT NULL DEFAULT 'suggested' COMMENT 'suggested/confirmed/rejected/deleted',
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_draft_field` (`draft_id`, `field_key`),
            KEY `idx_ai_profile_draft_field_status` (`draft_id`, `confirmation_status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像字段候选与来源证据'
    """,
    "ai_profile_revision": """
        CREATE TABLE IF NOT EXISTS `ai_profile_revision` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `revision_no` int unsigned NOT NULL,
            `draft_id` varchar(64) DEFAULT NULL,
            `source_revision_json` json DEFAULT NULL,
            `policy_revision` varchar(64) NOT NULL,
            `published_by` bigint unsigned DEFAULT NULL COMMENT '发布者只能本人或系统事务',
            `published_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_revision` (`user_id`, `subject`, `revision_no`),
            KEY `idx_ai_profile_revision_user_subject` (`user_id`, `subject`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像发布后的不可变版本'
    """,
    "ai_profile_revision_field": """
        CREATE TABLE IF NOT EXISTS `ai_profile_revision_field` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `revision_id` bigint unsigned NOT NULL,
            `field_key` varchar(64) NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `field_kind` enum('structured','entry') NOT NULL DEFAULT 'structured' COMMENT 'structured=受控字段/entry=条目（WP-P1，决策D2并存扩展）',
            `category` varchar(32) DEFAULT NULL COMMENT 'entry 分类，服务层校验 9 枚举',
            `content` varchar(200) DEFAULT NULL COMMENT 'entry 正文，≤200 字由服务层双保险',
            `replaces_field_key` varchar(64) DEFAULT NULL COMMENT 'entry 改写指向的被替换条目 field_key',
            `value_json` json DEFAULT NULL,
            `display_value` varchar(500) DEFAULT NULL,
            `confidence` decimal(5,4) DEFAULT NULL,
            `source_type` varchar(24) DEFAULT NULL,
            `source_turn_ids` json DEFAULT NULL,
            `source_span` varchar(500) DEFAULT NULL,
            `content_hash` char(64) NOT NULL,
            `schema_version` varchar(32) NOT NULL DEFAULT 'profile-extract-v1',
            `prompt_version` varchar(32) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_profile_revision_field` (`revision_id`, `field_key`),
            KEY `idx_ai_profile_revision_field_rev` (`revision_id`, `field_key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像发布版本逐字段快照'
    """,
    "ai_profile_summary": """
        CREATE TABLE IF NOT EXISTS `ai_profile_summary` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `session_id` varchar(64) DEFAULT NULL,
            `draft_id` varchar(64) DEFAULT NULL,
            `revision_id` bigint unsigned DEFAULT NULL,
            `user_id` bigint unsigned NOT NULL,
            `subject` varchar(24) NOT NULL COMMENT 'personal/ideal_partner',
            `summary_text` text DEFAULT NULL,
            `status` varchar(24) NOT NULL DEFAULT 'draft',
            `content_hash` char(64) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_ai_profile_summary_revision_subject` (`revision_id`, `subject`),
            KEY `idx_ai_profile_summary_user_subject` (`user_id`, `subject`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 画像总结草稿/发布文本及引用'
    """,
    # ============ M03 搜索、投影与 M06 兼容度（§10.3）============
    "ai_search_draft": """
        CREATE TABLE IF NOT EXISTS `ai_search_draft` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `draft_id` varchar(64) NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `query_text` text NOT NULL COMMENT '原始查询文本，不入普通日志',
            `source` varchar(24) DEFAULT NULL,
            `locale` varchar(16) DEFAULT NULL,
            `status` varchar(24) NOT NULL DEFAULT 'parsing' COMMENT 'parsing/awaiting_confirmation/confirmed/expired/failed',
            `condition_revision` int unsigned NOT NULL DEFAULT '0' COMMENT '条件编辑乐观锁，单调递增',
            `condition_schema_version` varchar(32) NOT NULL DEFAULT 'search-condition-v1',
            `policy_revision` varchar(64) NOT NULL,
            `consent_snapshot_json` json DEFAULT NULL,
            `last_patch_idempotency_key` varchar(128) DEFAULT NULL,
            `last_patch_request_digest` char(64) DEFAULT NULL,
            `last_patch_response_json` json DEFAULT NULL,
            `expires_at` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_search_draft_draft_id` (`draft_id`),
            KEY `idx_ai_search_draft_user_status` (`user_id`, `status`, `updated_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 搜索草稿原文与解析版本入口'
    """,
    "ai_search_condition": """
        CREATE TABLE IF NOT EXISTS `ai_search_condition` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `draft_id` varchar(64) NOT NULL,
            `condition_revision` int unsigned NOT NULL DEFAULT '0',
            `condition_no` int unsigned NOT NULL,
            `field_key` varchar(64) NOT NULL,
            `operator` varchar(24) NOT NULL,
            `value_json` json DEFAULT NULL,
            `condition_kind` varchar(16) NOT NULL COMMENT 'hard/soft/rank',
            `confidence` decimal(5,4) NOT NULL DEFAULT '0.0000' COMMENT '0..1',
            `source_span` varchar(500) DEFAULT NULL,
            `user_action` varchar(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/confirmed/edited/removed',
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_search_condition` (`draft_id`, `condition_revision`, `condition_no`),
            KEY `idx_ai_search_condition_action` (`draft_id`, `user_action`),
            KEY `idx_ai_search_condition_field_kind` (`field_key`, `condition_kind`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 搜索条件 AST 一行一条件'
    """,
    "ai_search_snapshot": """
        CREATE TABLE IF NOT EXISTS `ai_search_snapshot` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `snapshot_id` varchar(64) NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `draft_id` varchar(64) DEFAULT NULL,
            `snapshot_hash` char(64) NOT NULL,
            `status` varchar(24) NOT NULL DEFAULT 'completed',
            `condition_schema_version` varchar(32) NOT NULL DEFAULT 'search-condition-v1',
            `policy_revision` varchar(64) NOT NULL,
            `consent_snapshot_json` json DEFAULT NULL,
            `source_revision_json` json DEFAULT NULL,
            `result_total` int unsigned NOT NULL DEFAULT '0',
            `degraded` tinyint NOT NULL DEFAULT '0',
            `expires_at` datetime DEFAULT NULL,
            `invalidated_at` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_search_snapshot_snapshot_id` (`snapshot_id`),
            KEY `idx_ai_search_snapshot_hash` (`snapshot_hash`),
            KEY `idx_ai_search_snapshot_user_status` (`user_id`, `status`, `expires_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 搜索用户确认后的不可变查询快照'
    """,
    "ai_search_result": """
        CREATE TABLE IF NOT EXISTS `ai_search_result` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `snapshot_id` varchar(64) NOT NULL,
            `target_user_id` bigint unsigned NOT NULL,
            `projection_id` bigint unsigned DEFAULT NULL,
            `source_hash` char(64) DEFAULT NULL,
            `rank_position` int unsigned NOT NULL,
            `matched_condition_count` int unsigned NOT NULL DEFAULT '0',
            `matched_conditions` json DEFAULT NULL,
            `unknown_conditions` json DEFAULT NULL,
            `reason_codes` json DEFAULT NULL,
            `profile_revision` int unsigned NOT NULL DEFAULT '0',
            `consent_snapshot_json` json DEFAULT NULL,
            `source_revision_json` json DEFAULT NULL,
            `result_expires_at` datetime DEFAULT NULL,
            `stale` tinyint NOT NULL DEFAULT '0',
            `generation` int unsigned NOT NULL DEFAULT '1' COMMENT 'Task8 Step2：原子 generation，每次 execute 写新 generation，成功后原子切换 active generation',
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_search_result_snapshot_target` (`snapshot_id`, `target_user_id`),
            KEY `idx_ai_search_result_snapshot_rank` (`snapshot_id`, `rank_position`),
            KEY `idx_ai_search_result_target_expires` (`target_user_id`, `result_expires_at`),
            KEY `idx_ai_search_result_snapshot_generation` (`snapshot_id`, `generation`, `stale`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 搜索结果卡片引用、满足数与证据'
    """,
    "ai_feature_projection": """
        CREATE TABLE IF NOT EXISTS `ai_feature_projection` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `subject_user_id` bigint unsigned NOT NULL,
            `projection_kind` varchar(32) NOT NULL COMMENT 'personal_searchable/personal_compatibility/ideal_partner_preference',
            `source_hash` char(64) NOT NULL,
            `projection_version` varchar(32) NOT NULL,
            `fields_json` json DEFAULT NULL COMMENT '仅 allowlist 字段，不含原文',
            `source_revision_json` json DEFAULT NULL COMMENT '五维版本向量快照（profile/preference/privacy/relationship/policy），写入必须显式提供',
            `profile_revision` int unsigned NOT NULL DEFAULT '0',
            `preference_revision` int unsigned NOT NULL DEFAULT '0',
            `privacy_revision` int unsigned NOT NULL DEFAULT '0',
            `relationship_revision` int unsigned NOT NULL DEFAULT '0',
            `policy_revision` int unsigned NOT NULL DEFAULT '0',
            `consent_snapshot_json` json NOT NULL COMMENT '必填；§10.3 privacy/consent 投影授权证据，写入必须显式提供',
            `visibility_class` varchar(32) NOT NULL DEFAULT 'searchable' COMMENT 'searchable/self_only；self_only（ideal_partner_preference）仅本人偏好计算读取，不得作为候选资料返回',
            `status` varchar(24) NOT NULL DEFAULT 'active' COMMENT 'active/invalidated',
            `invalidated_at` datetime DEFAULT NULL,
            `invalidated_reason` varchar(64) DEFAULT NULL COMMENT '失效原因：ai_profile_deleted/ai_preference_deleted/ai_profile_field_deleted/rebuild',
            `expires_at` datetime DEFAULT NULL,
            `purge_after` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_feature_projection` (`subject_user_id`, `projection_kind`, `source_hash`, `projection_version`),
            KEY `idx_ai_feature_projection_user_status` (`subject_user_id`, `status`),
            KEY `idx_ai_feature_projection_privacy` (`privacy_revision`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 已确认资料的最小结构化投影'
    """,
    "ai_compatibility_snapshot": """
        CREATE TABLE IF NOT EXISTS `ai_compatibility_snapshot` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `snapshot_id` varchar(64) NOT NULL,
            `viewer_user_id` bigint unsigned NOT NULL,
            `target_user_id` bigint unsigned NOT NULL,
            `algorithm_version` varchar(32) NOT NULL DEFAULT 'compatibility-rule-v1',
            `snapshot_hash` char(64) NOT NULL,
            `status` varchar(24) NOT NULL DEFAULT 'ready' COMMENT 'ready/stale/blocked/coverage_insufficient',
            `score_semantics` varchar(48) NOT NULL DEFAULT 'rule_based_reference_shadow',
            `compatibility_index` decimal(5,2) DEFAULT NULL,
            `coverage` decimal(5,4) DEFAULT NULL COMMENT '0..1',
            `direction_json` json DEFAULT NULL COMMENT 'viewer_to_target/target_to_viewer',
            `reason_codes` json DEFAULT NULL,
            `evidence_json` json DEFAULT NULL COMMENT '原因码引用，不存对方敏感原文',
            `profile_revision_pair_json` json DEFAULT NULL,
            `privacy_revision_pair_json` json DEFAULT NULL,
            `source_revision_pair_json` json DEFAULT NULL,
            `consent_snapshot_pair_json` json DEFAULT NULL,
            `experiment_bucket` varchar(24) NOT NULL DEFAULT 'shadow',
            `display_eligible` tinyint NOT NULL DEFAULT '0',
            `disclaimer` varchar(500) DEFAULT NULL,
            `calculated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `expires_at` datetime DEFAULT NULL,
            `invalidated_at` datetime DEFAULT NULL,
            `purge_after` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_ai_compat_snapshot_snapshot_id` (`snapshot_id`),
            UNIQUE KEY `uk_ai_compat_snapshot_pair` (`viewer_user_id`, `target_user_id`, `algorithm_version`, `snapshot_hash`),
            KEY `idx_ai_compat_snapshot_viewer_target` (`viewer_user_id`, `target_user_id`, `status`),
            KEY `idx_ai_compat_snapshot_expires` (`expires_at`, `status`),
            CONSTRAINT `chk_ai_compat_viewer_not_target` CHECK (`viewer_user_id` <> `target_user_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 双向资料合拍参考 shadow 快照'
    """,

    # ============ 语音转写结果（P-04 / Phase 4）============
    # voice_transcribe 任务的转写结果独立存储，不写入 ai_task.payload_summary
    # （complete_task 成功时会 NULL 清除 payload_summary；handler 直接写 ai_task
    # 还会与 complete_task 的 SELECT FOR UPDATE 行锁死锁）。handler 在自身会话中
    # 写入本表，complete_task 在 finalize_db 中操作 ai_task，两表无行锁冲突。
    # result_ref（varchar(128)）存 task_id 作为引用键，路由通过它关联到本表。
    "voice_transcript": """
        CREATE TABLE IF NOT EXISTS `voice_transcript` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `task_id` varchar(64) NOT NULL COMMENT '关联 ai_task.task_id',
            `owner_user_id` bigint unsigned NOT NULL,
            `transcript` text NOT NULL COMMENT '转写文本',
            `confidence` float DEFAULT NULL,
            `duration_ms` int unsigned DEFAULT NULL,
            `detected_language` varchar(16) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_voice_transcript_task` (`task_id`),
            KEY `idx_voice_transcript_owner` (`owner_user_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='语音转写结果'
    """,
}

AI_CONSENT_OPERATION_TABLE = """
    CREATE TABLE IF NOT EXISTS `ai_consent_operation` (
        `id` bigint unsigned NOT NULL AUTO_INCREMENT,
        `operation_id` varchar(64) NOT NULL,
        `user_id` bigint unsigned NOT NULL,
        `scope` varchar(64) NOT NULL,
        `operation` varchar(16) NOT NULL COMMENT 'grant/revoke',
        `idempotency_key` varchar(128) NOT NULL,
        `request_digest` char(64) NOT NULL,
        `response_json` json NOT NULL,
        `cleanup_task_id` varchar(64) DEFAULT NULL,
        `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_ai_consent_operation_id` (`operation_id`),
        UNIQUE KEY `uk_ai_consent_operation_key` (`user_id`, `operation`, `idempotency_key`),
        KEY `idx_ai_consent_operation_scope` (`user_id`, `scope`, `created_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI consent idempotency records'
"""

# ----------------------------------------------------------------------
# Task 9 字段补全：ai_feature_projection 新增的版本向量/可见性列
# ----------------------------------------------------------------------
#
# 全新库由上面的 CREATE TABLE IF NOT EXISTS 直接获得完整列；旧库的
# CREATE TABLE 不会补齐新增列，因此在数据库初始化流程中调用
# ``ensure_ai_projection_columns`` 幂等补列（机制与
# database_setup_marriage.py 的 _ensure_required_columns 一致）。Task 9 文件
# 清单不含 database_setup_marriage.py，故本 helper 落在 ai_schema.py；部署侧
# 可在初始化流程（AUTO_INIT_DB）或发布窗口调用一次。privacy_revision 与
# consent_snapshot_json 在旧表已存在，无需补。
AI_PROJECTION_REQUIRED_COLUMNS: dict[str, str] = {
    "profile_revision": "`profile_revision` int unsigned NOT NULL DEFAULT '0'",
    "preference_revision": "`preference_revision` int unsigned NOT NULL DEFAULT '0'",
    "relationship_revision": "`relationship_revision` int unsigned NOT NULL DEFAULT '0'",
    "policy_revision": "`policy_revision` int unsigned NOT NULL DEFAULT '0'",
    "visibility_class": (
        "`visibility_class` varchar(32) NOT NULL DEFAULT 'searchable' "
        "COMMENT 'searchable/self_only；self_only 仅本人偏好计算读取'"
    ),
    "invalidated_reason": (
        "`invalidated_reason` varchar(64) DEFAULT NULL COMMENT '失效原因'"
    ),
    "expires_at": "`expires_at` datetime DEFAULT NULL",
}


def ensure_ai_projection_columns(cursor: Any) -> None:
    """Idempotently add the Task 9 ``ai_feature_projection`` columns to a legacy DB.

    ``cursor`` is a synchronous MySQL cursor (pymysql).  A missing table is
    silently skipped — the CREATE TABLE above already defines the full schema.
    """
    try:
        cursor.execute("SHOW COLUMNS FROM `ai_feature_projection`")
        existing = {row["Field"] for row in cursor.fetchall()}
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        return
    for column_name, column_def in AI_PROJECTION_REQUIRED_COLUMNS.items():
        if column_name not in existing:
            cursor.execute(
                f"ALTER TABLE `ai_feature_projection` ADD COLUMN {column_def}"
            )


# ----------------------------------------------------------------------
# WP-S1 / F9：ai_task 新增展示用进度列（旧库幂等补列）
# ----------------------------------------------------------------------
#
# 全新库由上面的 CREATE TABLE IF NOT EXISTS 直接获得完整列；旧库的
# CREATE TABLE 不会补齐新增列，与 ``ensure_ai_projection_columns`` 同模式
# 幂等补列（SHOW COLUMNS→ALTER TABLE ADD COLUMN；表不存在时静默跳过）。
AI_TASK_REQUIRED_COLUMNS: dict[str, str] = {
    "progress_percent": (
        "`progress_percent` tinyint unsigned DEFAULT NULL "
        "COMMENT '0-100 阶段进度，仅展示用途'"
    ),
}


def ensure_ai_task_columns(cursor: Any) -> None:
    """Idempotently add legacy-DB columns to ``ai_task``（如 progress_percent）。"""
    try:
        cursor.execute("SHOW COLUMNS FROM `ai_task`")
        existing = {row["Field"] for row in cursor.fetchall()}
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        return
    for column_name, column_def in AI_TASK_REQUIRED_COLUMNS.items():
        if column_name not in existing:
            cursor.execute(f"ALTER TABLE `ai_task` ADD COLUMN {column_def}")


# ----------------------------------------------------------------------
# WP-P1 / F4：画像字段表条目化补列（旧库幂等补列）
# ----------------------------------------------------------------------
#
# draft_field 与 revision_field 同构补 4 列；field_kind 默认 'structured'
# 保证存量行与既有 structured 链路零感知（决策 D2 并存扩展，entry 不改变
# 建构门槛边界）。与 ensure_ai_task_columns 同模式：SHOW COLUMNS→
# ALTER TABLE ADD COLUMN；表不存在时静默跳过（新库走 CREATE TABLE 完整列）。
AI_PROFILE_ENTRY_REQUIRED_COLUMNS: dict[str, str] = {
    "field_kind": (
        "`field_kind` enum('structured','entry') NOT NULL DEFAULT 'structured' "
        "COMMENT 'structured=受控字段/entry=条目（WP-P1，决策D2并存扩展）'"
    ),
    "category": (
        "`category` varchar(32) DEFAULT NULL "
        "COMMENT 'entry 分类，服务层校验 9 枚举'"
    ),
    "content": (
        "`content` varchar(200) DEFAULT NULL "
        "COMMENT 'entry 正文，≤200 字由服务层双保险'"
    ),
    "replaces_field_key": (
        "`replaces_field_key` varchar(64) DEFAULT NULL "
        "COMMENT 'entry 改写指向的被替换条目 field_key'"
    ),
}

AI_PROFILE_ENTRY_FIELD_TABLES = ("ai_profile_draft_field", "ai_profile_revision_field")


def ensure_ai_profile_entry_columns(cursor: Any) -> None:
    """Idempotently add entry columns to both profile field tables（旧库幂等补列）。"""
    for table_name in AI_PROFILE_ENTRY_FIELD_TABLES:
        try:
            cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            existing = {row["Field"] for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
            continue
        for column_name, column_def in AI_PROFILE_ENTRY_REQUIRED_COLUMNS.items():
            if column_name not in existing:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN {column_def}"
                )


# These additive columns keep an older bootstrap-created database readable until
# the reviewed hardening migration is applied. Index and constraint changes stay
# in migrations/ai so production rollout remains explicit and auditable.
AI_LEGACY_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "ai_consent_grant": {
        "updated_at": "`updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    },
    "ai_profile_session": {
        "skipped_field_keys": "`skipped_field_keys` json DEFAULT NULL COMMENT '用户跳过、本次不再追问的字段'",
    },
    "ai_profile_turn": {
        "turn_id": "`turn_id` varchar(128) NULL COMMENT '稳定的服务端 turn ID'",
        "updated_at": "`updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    },
    "ai_profile_draft_field": {
        "source_span": "`source_span` varchar(500) DEFAULT NULL",
    },
    "ai_profile_draft": {
        "last_operation_idempotency_key": "`last_operation_idempotency_key` varchar(128) DEFAULT NULL",
        "last_operation_request_digest": "`last_operation_request_digest` char(64) DEFAULT NULL",
        "last_operation_response_json": "`last_operation_response_json` json DEFAULT NULL",
    },
    "ai_profile_revision_field": {
        "source_span": "`source_span` varchar(500) DEFAULT NULL",
    },
    "ai_search_result": {
        "projection_id": "`projection_id` bigint unsigned DEFAULT NULL",
        "source_hash": "`source_hash` char(64) DEFAULT NULL",
        "consent_snapshot_json": "`consent_snapshot_json` json DEFAULT NULL",
        "source_revision_json": "`source_revision_json` json DEFAULT NULL",
        "updated_at": "`updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        # Task8 Step2：原子 generation 列（加法，默认 1）。每次 execute 写新
        # generation 行，成功后原子切换 active generation，再清理旧 generation。
        "generation": "`generation` int unsigned NOT NULL DEFAULT '1' COMMENT 'Task8 Step2：原子 generation'",
    },
    "ai_search_snapshot": {
        "result_total": "`result_total` int unsigned NOT NULL DEFAULT '0'",
        "degraded": "`degraded` tinyint NOT NULL DEFAULT '0'",
    },
    "ai_search_draft": {
        "last_patch_idempotency_key": "`last_patch_idempotency_key` varchar(128) DEFAULT NULL",
        "last_patch_request_digest": "`last_patch_request_digest` char(64) DEFAULT NULL",
        "last_patch_response_json": "`last_patch_response_json` json DEFAULT NULL",
    },
    "ai_compatibility_snapshot": {
        "source_revision_pair_json": "`source_revision_pair_json` json DEFAULT NULL",
        "consent_snapshot_pair_json": "`consent_snapshot_pair_json` json DEFAULT NULL",
        "updated_at": "`updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    },
}

# Task 10 retention columns are additive so bootstrap can bring an existing
# phase-two database to a readable shape before the reviewed migration runs.
AI_TASK10_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "ai_task": {
        "owner_tombstone": "`owner_tombstone` char(64) DEFAULT NULL",
    },
    "ai_consent_grant": {
        "user_tombstone": "`user_tombstone` char(64) DEFAULT NULL",
    },
}


def ensure_ai_legacy_columns(cursor: Any) -> None:
    """Add Task 2's additive AI columns during legacy bootstrap.

    The reviewed migration owns unique keys, generated columns and retention
    indexes. This helper only makes old tables accept the new row payloads; it
    is intentionally idempotent and safe to call after ``CREATE TABLE``.
    """
    for table_name, required_columns in AI_LEGACY_REQUIRED_COLUMNS.items():
        try:
            cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            existing = {row["Field"] for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001, S112 - legacy bootstrap is best effort
            continue
        for column_name, column_def in required_columns.items():
            if column_name not in existing:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN {column_def}"
                )

    for table_name, required_columns in AI_TASK10_REQUIRED_COLUMNS.items():
        try:
            cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            existing = {row["Field"] for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001, S112 - legacy bootstrap is best effort
            continue
        for column_name, column_def in required_columns.items():
            if column_name not in existing:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN {column_def}"
                )
        if table_name == "ai_task" and "owner_user_id" in existing:
            cursor.execute(
                "ALTER TABLE `ai_task` MODIFY COLUMN `owner_user_id` bigint unsigned DEFAULT NULL"
            )
        if table_name == "ai_consent_grant" and "user_id" in existing:
            cursor.execute(
                "ALTER TABLE `ai_consent_grant` MODIFY COLUMN `user_id` bigint unsigned DEFAULT NULL"
            )
            # Defect 62: once user_id is nullable (Task 10), tombstone rows
            # with user_id=NULL leave the unique key (SQL NULLs never compare
            # equal). The original Defect 47 response — a stored
            # user_id_coalesce = COALESCE(user_id, 0) key column — collapses
            # every scrubbed row to 0 and collides with 1062 when two grants
            # share the same DATETIME second (fsp=0). Revert to keying the
            # unique constraint on the raw user_id; scrubbed rows are exempt
            # from dedup, which is safe because a tombstone can only come from
            # a row that was unique while live. Idempotent in both directions.
            ensure_ai_consent_unique_key(cursor)

    try:
        cursor.execute("SHOW COLUMNS FROM `ai_profile_turn`")
        profile_turn_columns = {row["Field"] for row in cursor.fetchall()}
        if "turn_id" in profile_turn_columns:
            cursor.execute(
                "UPDATE `ai_profile_turn` "
                "SET `turn_id` = CONCAT('legacy-turn-', `id`) "
                "WHERE `turn_id` IS NULL OR `turn_id` = ''"
            )
            cursor.execute(
                "ALTER TABLE `ai_profile_turn` "
                "MODIFY COLUMN `turn_id` varchar(128) NOT NULL "
                "COMMENT '稳定的服务端 turn ID'"
            )
            cursor.execute(
                "ALTER TABLE `ai_profile_turn` "
                "MODIFY COLUMN `client_turn_id` varchar(128) NOT NULL"
            )
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        # The reviewed migration reports and classifies any incompatible data.
        return


def ensure_ai_consent_unique_key(cursor: Any) -> None:
    """Idempotently revert the Defect 47 ``user_id_coalesce`` dedup and rebuild
    the consent unique key on the raw ``user_id`` (Defect 62).

    After Task 10 makes ``ai_consent_grant.user_id`` nullable, tombstone rows
    (``user_id IS NULL``) are exempt from ``uk_ai_consent_user_scope_version
    (user_id, scope, version, granted_at)`` because SQL ``NULL`` values never
    compare equal.  Defect 47's stored ``user_id_coalesce = COALESCE(user_id,
    0)`` key was meant to dedup tombstones too, but it collapses every
    scrubbed row to ``0``: two grants of the same scope/version recorded in
    the same DATETIME second (fsp=0) collide with ``1062`` when the second one
    is scrubbed — and a re-grant after deletion collides with the tombstone on
    INSERT.  A tombstone can only ever be produced by scrubbing a row that was
    unique while live, so duplicate tombstones cannot arise and the dedup is
    unnecessary; keying on ``user_id`` (NULL exempt) restores both paths.

    Idempotent in both directions: it drops the generated column and the key
    when they reference ``user_id_coalesce``, and rebuilds the key on
    ``user_id`` when it is missing.
    """
    try:
        cursor.execute(
            "SELECT column_name AS column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'ai_consent_grant'"
        )
        consent_columns = {str(row["column_name"]) for row in cursor.fetchall()}
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        return
    if "user_id" not in consent_columns:
        return

    try:
        cursor.execute(
            "SELECT index_name AS index_name, column_name AS column_name, seq_in_index AS seq_in_index "
            "FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'ai_consent_grant' "
            "AND index_name = 'uk_ai_consent_user_scope_version' "
            "ORDER BY seq_in_index"
        )
        key_columns = [
            str(row["column_name"]) for row in cursor.fetchall()
        ]
    except Exception:  # noqa: BLE001 - legacy bootstrap is best effort
        return

    if key_columns and key_columns[0] == "user_id_coalesce":
        cursor.execute(
            "ALTER TABLE `ai_consent_grant` DROP INDEX `uk_ai_consent_user_scope_version`"
        )
        key_columns = []

    if "user_id_coalesce" in consent_columns:
        cursor.execute(
            "ALTER TABLE `ai_consent_grant` DROP COLUMN `user_id_coalesce`"
        )

    if not key_columns:
        cursor.execute(
            "ALTER TABLE `ai_consent_grant` "
            "ADD UNIQUE KEY `uk_ai_consent_user_scope_version` "
            "(`user_id`, `scope`, `version`, `granted_at`)"
        )
