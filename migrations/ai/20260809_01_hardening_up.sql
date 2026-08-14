-- AI phase-one schema hardening. The runner executes each statement separately
-- and treats duplicate-column/index errors as idempotent replays.

CREATE TABLE IF NOT EXISTS `ai_consent_operation` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
    `operation_id` varchar(64) NOT NULL,
    `user_id` bigint unsigned NOT NULL,
    `scope` varchar(64) NOT NULL,
    `operation` varchar(16) NOT NULL,
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE `ai_consent_grant`
    ADD COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;

ALTER TABLE `ai_profile_turn`
    ADD COLUMN `turn_id` varchar(128) NULL AFTER `id`;

UPDATE `ai_profile_turn`
SET `turn_id` = CONCAT('legacy-turn-', `id`)
WHERE `turn_id` IS NULL;

ALTER TABLE `ai_profile_turn`
    MODIFY COLUMN `turn_id` varchar(128) NOT NULL;

ALTER TABLE `ai_profile_turn`
    MODIFY COLUMN `client_turn_id` varchar(128) NOT NULL;

ALTER TABLE `ai_profile_turn`
    ADD COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;

ALTER TABLE `ai_profile_turn`
    ADD UNIQUE KEY `uk_ai_profile_turn_id` (`turn_id`);

ALTER TABLE `ai_profile_session`
    ADD COLUMN `active_slot` tinyint GENERATED ALWAYS AS (CASE WHEN `active_status` = 1 THEN 1 ELSE NULL END) STORED;

ALTER TABLE `ai_profile_session`
    DROP INDEX `uk_ai_profile_session_active`;

ALTER TABLE `ai_profile_session`
    ADD UNIQUE KEY `uk_ai_profile_session_active` (`user_id`, `subject`, `active_slot`);

ALTER TABLE `ai_search_result`
    ADD COLUMN `projection_id` bigint unsigned DEFAULT NULL AFTER `target_user_id`;

ALTER TABLE `ai_search_result`
    ADD COLUMN `source_hash` char(64) DEFAULT NULL AFTER `projection_id`;

ALTER TABLE `ai_search_result`
    ADD COLUMN `consent_snapshot_json` json DEFAULT NULL AFTER `profile_revision`;

ALTER TABLE `ai_search_result`
    ADD COLUMN `source_revision_json` json DEFAULT NULL AFTER `consent_snapshot_json`;

ALTER TABLE `ai_search_snapshot`
    ADD COLUMN `result_total` int unsigned NOT NULL DEFAULT '0' AFTER `source_revision_json`;

ALTER TABLE `ai_search_snapshot`
    ADD COLUMN `degraded` tinyint NOT NULL DEFAULT '0' AFTER `result_total`;

ALTER TABLE `ai_search_draft`
    ADD COLUMN `last_patch_idempotency_key` varchar(128) DEFAULT NULL AFTER `consent_snapshot_json`;

ALTER TABLE `ai_search_draft`
    ADD COLUMN `last_patch_request_digest` char(64) DEFAULT NULL AFTER `last_patch_idempotency_key`;

ALTER TABLE `ai_search_draft`
    ADD COLUMN `last_patch_response_json` json DEFAULT NULL AFTER `last_patch_request_digest`;

ALTER TABLE `ai_profile_draft`
    ADD COLUMN `last_operation_idempotency_key` varchar(128) DEFAULT NULL AFTER `published_revision_id`;

ALTER TABLE `ai_profile_draft`
    ADD COLUMN `last_operation_request_digest` char(64) DEFAULT NULL AFTER `last_operation_idempotency_key`;

ALTER TABLE `ai_profile_draft`
    ADD COLUMN `last_operation_response_json` json DEFAULT NULL AFTER `last_operation_request_digest`;

ALTER TABLE `ai_search_result`
    ADD COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;

ALTER TABLE `ai_search_result`
    DROP INDEX `uk_ai_search_result_rank`;

ALTER TABLE `ai_search_result`
    ADD KEY `idx_ai_search_result_snapshot_rank` (`snapshot_id`, `rank_position`);

ALTER TABLE `ai_compatibility_snapshot`
    ADD COLUMN `source_revision_pair_json` json DEFAULT NULL AFTER `privacy_revision_pair_json`,
    ADD COLUMN `consent_snapshot_pair_json` json DEFAULT NULL AFTER `source_revision_pair_json`,
    ADD COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;

ALTER TABLE `ai_profile_draft`
    ADD KEY `idx_ai_profile_draft_expiry` (`expires_at`, `status`);

ALTER TABLE `ai_profile_draft_field`
    ADD COLUMN `source_span` varchar(500) DEFAULT NULL AFTER `source_turn_ids`;

ALTER TABLE `ai_profile_revision_field`
    ADD COLUMN `source_span` varchar(500) DEFAULT NULL AFTER `source_turn_ids`;

ALTER TABLE `ai_profile_turn`
    ADD KEY `idx_ai_profile_turn_retention` (`created_at`, `updated_at`);

ALTER TABLE `ai_search_draft`
    ADD KEY `idx_ai_search_draft_expiry` (`expires_at`, `status`);

ALTER TABLE `ai_search_snapshot`
    ADD KEY `idx_ai_search_snapshot_expiry` (`expires_at`, `invalidated_at`);

ALTER TABLE `ai_search_result`
    ADD KEY `idx_ai_search_result_retention` (`result_expires_at`, `stale`);

ALTER TABLE `ai_task`
    ADD KEY `idx_ai_task_finished_at` (`finished_at`, `status`);

ALTER TABLE `ai_feature_projection`
    ADD KEY `idx_ai_feature_projection_purge` (`purge_after`, `status`);

ALTER TABLE `ai_compatibility_snapshot`
    ADD KEY `idx_ai_compat_snapshot_purge` (`purge_after`, `status`);

ALTER TABLE `ai_generation_audit`
    ADD KEY `idx_ai_generation_audit_retention` (`created_at`);
