-- Roll back only the Task 10 additive storage contract. Invalidated rows are
-- intentionally not restored or made readable by this operation.

ALTER TABLE `derivation_consumer_receipt` DROP INDEX `idx_derivation_receipt_retention`;
ALTER TABLE `ai_consent_grant` DROP INDEX `idx_ai_consent_tombstone_retention`;
ALTER TABLE `ai_task` DROP INDEX `idx_ai_task_tombstone_retention`;

ALTER TABLE `ai_consent_grant` DROP COLUMN `user_tombstone`;
ALTER TABLE `ai_task` DROP COLUMN `owner_tombstone`;

-- Deleted owners remain deliberately detached.  The pre-Task-10 schema was
-- NOT NULL, so use an unreachable sentinel only for structural rollback; no
-- content is made visible by this conversion.
UPDATE `ai_consent_grant` SET `user_id` = 0 WHERE `user_id` IS NULL;
UPDATE `ai_task` SET `owner_user_id` = 0 WHERE `owner_user_id` IS NULL;
ALTER TABLE `ai_consent_grant` MODIFY COLUMN `user_id` bigint unsigned NOT NULL;
ALTER TABLE `ai_task` MODIFY COLUMN `owner_user_id` bigint unsigned NOT NULL;

ALTER TABLE `derivation_consumer_receipt` DROP COLUMN `duration_ms`;
ALTER TABLE `derivation_consumer_receipt` DROP COLUMN `outcome`;
ALTER TABLE `derivation_consumer_receipt` DROP COLUMN `event_type`;

ALTER TABLE `derivation_outbox` DROP INDEX `idx_derivation_outbox_dead_letter`;
ALTER TABLE `derivation_outbox` DROP INDEX `idx_derivation_outbox_publish`;
ALTER TABLE `derivation_outbox` ADD KEY `idx_derivation_outbox_publish` (`published_at`, `priority`, `occurred_at`);
ALTER TABLE `derivation_outbox` DROP COLUMN `dead_letter_at`;
ALTER TABLE `derivation_outbox` DROP COLUMN `last_error_code`;
ALTER TABLE `derivation_outbox` DROP COLUMN `attempt_count`;
ALTER TABLE `derivation_outbox` DROP COLUMN `status`;
