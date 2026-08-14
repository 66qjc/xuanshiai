-- Reverse the phase-one hardening. The runner refuses rollback while AI tasks
-- are active and stops on non-idempotent data conflicts.

DROP TABLE IF EXISTS `ai_consent_operation`;

ALTER TABLE `ai_generation_audit` DROP INDEX `idx_ai_generation_audit_retention`;
ALTER TABLE `ai_compatibility_snapshot` DROP INDEX `idx_ai_compat_snapshot_purge`;
ALTER TABLE `ai_feature_projection` DROP INDEX `idx_ai_feature_projection_purge`;
ALTER TABLE `ai_task` DROP INDEX `idx_ai_task_finished_at`;
ALTER TABLE `ai_search_result` DROP INDEX `idx_ai_search_result_retention`;
ALTER TABLE `ai_search_snapshot` DROP INDEX `idx_ai_search_snapshot_expiry`;
ALTER TABLE `ai_search_draft` DROP INDEX `idx_ai_search_draft_expiry`;
ALTER TABLE `ai_profile_turn` DROP INDEX `idx_ai_profile_turn_retention`;
ALTER TABLE `ai_profile_draft` DROP INDEX `idx_ai_profile_draft_expiry`;

ALTER TABLE `ai_profile_revision_field` DROP COLUMN `source_span`;
ALTER TABLE `ai_profile_draft_field` DROP COLUMN `source_span`;

ALTER TABLE `ai_compatibility_snapshot` DROP COLUMN `updated_at`;
ALTER TABLE `ai_compatibility_snapshot` DROP COLUMN `consent_snapshot_pair_json`;
ALTER TABLE `ai_compatibility_snapshot` DROP COLUMN `source_revision_pair_json`;

-- Defect 25: re-adding the global rank UNIQUE KEY can fail if duplicate
-- (snapshot_id, rank_position) pairs were created while the key was
-- absent (non-UNIQUE idx_ai_search_result_snapshot_rank). The migration
-- runner (_execute_file) treats MySQL error 1062 (duplicate key) as
-- fatal, so the ADD UNIQUE KEY below naturally aborts the rollback when
-- duplicates exist -- the run is recorded as rollback_failed. A separate
-- pre-check using SIGNAL SQLSTATE '45000' would require a multi-statement
-- stored procedure, but the runner splits on ';' and executes one
-- statement at a time, so SIGNAL cannot be used here. Aborting on 1062
-- is the equivalent safety gate: it refuses to silently accept bad data.
ALTER TABLE `ai_search_result` DROP INDEX `idx_ai_search_result_snapshot_rank`;
ALTER TABLE `ai_search_result` ADD UNIQUE KEY `uk_ai_search_result_rank` (`snapshot_id`, `rank_position`);
ALTER TABLE `ai_search_result` DROP COLUMN `updated_at`;
ALTER TABLE `ai_search_result` DROP COLUMN `source_revision_json`;
ALTER TABLE `ai_search_result` DROP COLUMN `consent_snapshot_json`;
ALTER TABLE `ai_search_result` DROP COLUMN `source_hash`;
ALTER TABLE `ai_search_result` DROP COLUMN `projection_id`;

ALTER TABLE `ai_search_snapshot` DROP COLUMN `degraded`;
ALTER TABLE `ai_search_snapshot` DROP COLUMN `result_total`;

ALTER TABLE `ai_search_draft` DROP COLUMN `last_patch_response_json`;
ALTER TABLE `ai_search_draft` DROP COLUMN `last_patch_request_digest`;
ALTER TABLE `ai_search_draft` DROP COLUMN `last_patch_idempotency_key`;

ALTER TABLE `ai_profile_draft` DROP COLUMN `last_operation_response_json`;
ALTER TABLE `ai_profile_draft` DROP COLUMN `last_operation_request_digest`;
ALTER TABLE `ai_profile_draft` DROP COLUMN `last_operation_idempotency_key`;

ALTER TABLE `ai_profile_session` DROP INDEX `uk_ai_profile_session_active`;
ALTER TABLE `ai_profile_session` ADD UNIQUE KEY `uk_ai_profile_session_active` (`user_id`, `subject`, `active_status`);
ALTER TABLE `ai_profile_session` DROP COLUMN `active_slot`;

ALTER TABLE `ai_profile_turn` DROP INDEX `uk_ai_profile_turn_id`;
ALTER TABLE `ai_profile_turn` DROP COLUMN `updated_at`;
-- Defect 24: client_turn_id width is intentionally NOT rolled back to
-- varchar(64). During the hardened phase (up migration) the column was
-- widened to varchar(128) and may already hold 65-128 character values.
-- Truncating back to varchar(64) would silently corrupt those rows, and
-- a guarded rollback (SIGNAL SQLSTATE '45000') cannot be expressed as a
-- single statement under the migration runner's per-statement executor
-- (which splits on ';'). Keeping varchar(128) is safe: the legacy schema
-- only required <=64 chars, so a wider column is backward compatible and
-- preserves any data written during the hardened window. A future data
-- cleanup that first verifies no value exceeds 64 chars can re-introduce
-- the narrower type if needed.
-- ALTER TABLE `ai_profile_turn` MODIFY COLUMN `client_turn_id` varchar(64) NOT NULL;
ALTER TABLE `ai_profile_turn` DROP COLUMN `turn_id`;

ALTER TABLE `ai_consent_grant` DROP COLUMN `updated_at`;
