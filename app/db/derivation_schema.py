"""Revision-vector state and derivation-outbox table definitions.

These tables back the AI profile/search/matchability derivation pipeline:
each user mutation bumps one dimension of ``user_revision_state`` and writes a
derivation event into ``derivation_outbox`` inside the same transaction, while
``derivation_consumer_receipt`` makes downstream consumption idempotent.
"""

DERIVATION_TABLES = {
    "user_revision_state": """
        CREATE TABLE IF NOT EXISTS `user_revision_state` (
            `user_id` bigint unsigned NOT NULL,
            `profile_revision` int unsigned NOT NULL DEFAULT '0',
            `preference_revision` int unsigned NOT NULL DEFAULT '0',
            `privacy_revision` int unsigned NOT NULL DEFAULT '0',
            `relationship_revision` int unsigned NOT NULL DEFAULT '0',
            `policy_revision` int unsigned NOT NULL DEFAULT '0',
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`user_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户派生投影版本向量'
    """,
    "derivation_outbox": """
        CREATE TABLE IF NOT EXISTS `derivation_outbox` (
            `event_id` varchar(64) NOT NULL,
            `aggregate_type` varchar(32) NOT NULL,
            `aggregate_id` bigint unsigned NOT NULL,
            `event_type` varchar(64) NOT NULL,
            `changed_fields` json DEFAULT NULL,
            `source_revision_json` json DEFAULT NULL,
            `privacy_revision` int unsigned NOT NULL DEFAULT '0',
            `payload_minimal` json DEFAULT NULL,
            `occurred_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `priority` int NOT NULL DEFAULT '50',
            `published_at` datetime DEFAULT NULL,
            `lease_owner` varchar(64) DEFAULT NULL,
            `lease_until` datetime DEFAULT NULL,
            PRIMARY KEY (`event_id`),
            KEY `idx_derivation_outbox_publish` (`published_at`, `priority`, `occurred_at`),
            KEY `idx_derivation_outbox_aggregate` (`aggregate_type`, `aggregate_id`, `occurred_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='派生事件外发盒'
    """,
    "derivation_consumer_receipt": """
        CREATE TABLE IF NOT EXISTS `derivation_consumer_receipt` (
            `event_id` varchar(64) NOT NULL,
            `consumer_name` varchar(64) NOT NULL,
            `processed_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `lease_until` datetime DEFAULT NULL,
            PRIMARY KEY (`event_id`, `consumer_name`),
            KEY `idx_derivation_receipt_consumer` (`consumer_name`, `processed_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='派生事件消费收据'
    """,
}
