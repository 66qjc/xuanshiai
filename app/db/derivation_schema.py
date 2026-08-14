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
            `status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/processing/succeeded/dead_letter',
            `attempt_count` int unsigned NOT NULL DEFAULT '0',
            `last_error_code` varchar(64) DEFAULT NULL,
            `dead_letter_at` datetime DEFAULT NULL,
            `lease_owner` varchar(64) DEFAULT NULL,
            `lease_until` datetime DEFAULT NULL,
            PRIMARY KEY (`event_id`),
            KEY `idx_derivation_outbox_publish` (`status`, `published_at`, `priority`, `occurred_at`),
            KEY `idx_derivation_outbox_dead_letter` (`dead_letter_at`, `status`),
            KEY `idx_derivation_outbox_aggregate` (`aggregate_type`, `aggregate_id`, `occurred_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='派生事件外发盒'
    """,
    "derivation_consumer_receipt": """
        CREATE TABLE IF NOT EXISTS `derivation_consumer_receipt` (
            `event_id` varchar(64) NOT NULL,
            `consumer_name` varchar(64) NOT NULL,
            `event_type` varchar(64) NOT NULL DEFAULT '',
            `outcome` varchar(16) NOT NULL DEFAULT 'processed' COMMENT 'processed/noop/dead_letter',
            `duration_ms` int unsigned NOT NULL DEFAULT '0',
            `processed_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `lease_until` datetime DEFAULT NULL,
            PRIMARY KEY (`event_id`, `consumer_name`),
            KEY `idx_derivation_receipt_consumer` (`consumer_name`, `processed_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='派生事件消费收据'
    """,
}


DERIVATION_TASK10_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "derivation_outbox": {
        "status": "`status` varchar(16) NOT NULL DEFAULT 'pending'",
        "attempt_count": "`attempt_count` int unsigned NOT NULL DEFAULT '0'",
        "last_error_code": "`last_error_code` varchar(64) DEFAULT NULL",
        "dead_letter_at": "`dead_letter_at` datetime DEFAULT NULL",
    },
    "derivation_consumer_receipt": {
        "event_type": "`event_type` varchar(64) NOT NULL DEFAULT ''",
        "outcome": "`outcome` varchar(16) NOT NULL DEFAULT 'processed'",
        "duration_ms": "`duration_ms` int unsigned NOT NULL DEFAULT '0'",
    },
}


def ensure_derivation_task10_columns(cursor: object) -> None:
    """Add Task 10 columns to a database created before the second migration."""
    for table_name, required_columns in DERIVATION_TASK10_REQUIRED_COLUMNS.items():
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
