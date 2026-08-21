-- Task 10: unified outbox state, receipts and retention tombstones.

ALTER TABLE `derivation_outbox`
    ADD COLUMN `status` varchar(16) NOT NULL DEFAULT 'pending' AFTER `published_at`;
ALTER TABLE `derivation_outbox`
    ADD COLUMN `attempt_count` int unsigned NOT NULL DEFAULT '0' AFTER `status`;
ALTER TABLE `derivation_outbox`
    ADD COLUMN `last_error_code` varchar(64) DEFAULT NULL AFTER `attempt_count`;
ALTER TABLE `derivation_outbox`
    ADD COLUMN `dead_letter_at` datetime DEFAULT NULL AFTER `last_error_code`;
ALTER TABLE `derivation_outbox` DROP INDEX `idx_derivation_outbox_publish`;
ALTER TABLE `derivation_outbox`
    ADD KEY `idx_derivation_outbox_publish` (`status`, `published_at`, `priority`, `occurred_at`);
ALTER TABLE `derivation_outbox`
    ADD KEY `idx_derivation_outbox_dead_letter` (`dead_letter_at`, `status`);

ALTER TABLE `derivation_consumer_receipt`
    ADD COLUMN `event_type` varchar(64) NOT NULL DEFAULT '' AFTER `consumer_name`;
ALTER TABLE `derivation_consumer_receipt`
    ADD COLUMN `outcome` varchar(16) NOT NULL DEFAULT 'processed' AFTER `event_type`;
ALTER TABLE `derivation_consumer_receipt`
    ADD COLUMN `duration_ms` int unsigned NOT NULL DEFAULT '0' AFTER `outcome`;

ALTER TABLE `ai_task`
    MODIFY COLUMN `owner_user_id` bigint unsigned DEFAULT NULL;
ALTER TABLE `ai_task`
    ADD COLUMN `owner_tombstone` char(64) DEFAULT NULL AFTER `owner_user_id`;

ALTER TABLE `ai_consent_grant`
    MODIFY COLUMN `user_id` bigint unsigned DEFAULT NULL;
ALTER TABLE `ai_consent_grant`
    ADD COLUMN `user_tombstone` char(64) DEFAULT NULL AFTER `user_id`;

ALTER TABLE `ai_task` ADD KEY `idx_ai_task_tombstone_retention` (`finished_at`, `owner_user_id`);
ALTER TABLE `ai_consent_grant` ADD KEY `idx_ai_consent_tombstone_retention` (`revoked_at`, `user_id`);
ALTER TABLE `derivation_consumer_receipt` ADD KEY `idx_derivation_receipt_retention` (`processed_at`, `outcome`);
