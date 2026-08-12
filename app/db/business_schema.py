"""一期商业化和组织归属领域的数据库表定义。"""

BUSINESS_TABLES = {
    "customer_lead": """
        CREATE TABLE IF NOT EXISTS `customer_lead` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `name` varchar(128) NOT NULL,
            `phone` varchar(32) DEFAULT NULL,
            `wechat` varchar(128) DEFAULT NULL,
            `source` varchar(64) NOT NULL,
            `intention_level` tinyint NOT NULL DEFAULT '1' COMMENT '1低 2中 3高',
            `status` varchar(32) NOT NULL DEFAULT 'NEW' COMMENT 'NEW/CONTACTED/INTENDED/CONVERTED/LOST/CLOSED',
            `matchmaker_id` bigint unsigned DEFAULT NULL,
            `organization_id` bigint unsigned DEFAULT NULL,
            `next_follow_at` datetime DEFAULT NULL,
            `converted_user_id` bigint unsigned DEFAULT NULL,
            `remark` varchar(2000) DEFAULT NULL,
            `created_by` bigint unsigned NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_customer_lead_status` (`status`, `created_at`),
            KEY `idx_customer_lead_matchmaker` (`matchmaker_id`, `status`),
            KEY `idx_customer_lead_phone` (`phone`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='后台客源线索'
    """,
    "customer_lead_follow_up": """
        CREATE TABLE IF NOT EXISTS `customer_lead_follow_up` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `lead_id` bigint unsigned NOT NULL,
            `method` varchar(32) NOT NULL COMMENT 'PHONE/WECHAT/VISIT/OTHER',
            `content` varchar(2000) NOT NULL,
            `intention_level` tinyint DEFAULT NULL,
            `next_follow_at` datetime DEFAULT NULL,
            `created_by` bigint unsigned NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_lead_follow_up_lead` (`lead_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='客源线索跟进记录'
    """,
    "member_follow_up": """
        CREATE TABLE IF NOT EXISTS `member_follow_up` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `method` varchar(32) NOT NULL COMMENT 'PHONE/WECHAT/VISIT/OTHER',
            `content` varchar(2000) NOT NULL,
            `next_follow_at` datetime DEFAULT NULL,
            `created_by` bigint unsigned NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_member_follow_up_user` (`user_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会员 CRM 跟进记录'
    """,
    "matchmaker_admin_account": """
        CREATE TABLE IF NOT EXISTS `matchmaker_admin_account` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `username` varchar(64) NOT NULL,
            `password_hash` varchar(255) NOT NULL,
            `matchmaker_user_id` bigint unsigned DEFAULT NULL,
            `display_name` varchar(128) NOT NULL,
            `data_scope` varchar(16) NOT NULL DEFAULT 'SELF' COMMENT 'SELF/STORE/ORGANIZATION/ALL',
            `organization_id` bigint unsigned DEFAULT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2停用',
            `failed_count` int unsigned NOT NULL DEFAULT '0',
            `locked_until` datetime DEFAULT NULL,
            `last_login_at` datetime DEFAULT NULL,
            `last_login_ip` varchar(64) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_admin_username` (`username`),
            KEY `idx_matchmaker_admin_user` (`matchmaker_user_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='独立红娘后台账号'
    """,
    "matchmaker_admin_session": """
        CREATE TABLE IF NOT EXISTS `matchmaker_admin_session` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `account_id` bigint unsigned NOT NULL,
            `refresh_token_hash` char(64) NOT NULL,
            `access_token_hash` char(64) DEFAULT NULL,
            `ip` varchar(64) DEFAULT NULL,
            `user_agent` varchar(255) DEFAULT NULL,
            `access_expire_at` datetime NOT NULL,
            `refresh_expire_at` datetime NOT NULL,
            `last_used_at` datetime NOT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2注销 3轮换',
            `revoked_at` datetime DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_admin_refresh` (`refresh_token_hash`),
            KEY `idx_matchmaker_admin_session_account` (`account_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='红娘后台登录会话'
    """,
    "matchmaker_admin_permission": """
        CREATE TABLE IF NOT EXISTS `matchmaker_admin_permission` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `account_id` bigint unsigned NOT NULL,
            `permission` varchar(128) NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_admin_permission` (`account_id`, `permission`),
            KEY `idx_matchmaker_admin_permission_account` (`account_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='独立红娘后台账号权限'
    """,
    "matchmaker_admin_login_log": """
        CREATE TABLE IF NOT EXISTS `matchmaker_admin_login_log` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `account_id` bigint unsigned DEFAULT NULL,
            `username` varchar(64) NOT NULL,
            `login_status` tinyint NOT NULL COMMENT '0失败 1成功',
            `ip` varchar(64) DEFAULT NULL,
            `user_agent` varchar(255) DEFAULT NULL,
            `device_id` varchar(128) DEFAULT NULL,
            `failure_reason` varchar(255) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_matchmaker_admin_login_account` (`account_id`, `created_at`),
            KEY `idx_matchmaker_admin_login_username` (`username`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='独立红娘后台登录日志'
    """,
    "matchmaker_admin_member_note": """
        CREATE TABLE IF NOT EXISTS `matchmaker_admin_member_note` (
            `user_id` bigint unsigned NOT NULL,
            `note` varchar(2000) DEFAULT NULL,
            `updated_by` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`user_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='后台会员备注'
    """,
    "organization": """
        CREATE TABLE IF NOT EXISTS `organization` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `parent_id` bigint unsigned DEFAULT NULL,
            `org_type` varchar(32) NOT NULL COMMENT 'platform/store',
            `code` varchar(64) NOT NULL,
            `name` varchar(128) NOT NULL,
            `display_name` varchar(128) DEFAULT NULL,
            `region_code` varchar(64) DEFAULT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2关闭 3停用',
            `auto_redirect` tinyint NOT NULL DEFAULT '0',
            `created_by` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_organization_code` (`code`),
            KEY `idx_organization_parent_status` (`parent_id`, `status`),
            KEY `idx_organization_region` (`region_code`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='平台和门店组织'
    """,
    "organization_member": """
        CREATE TABLE IF NOT EXISTS `organization_member` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `organization_id` bigint unsigned NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `role_code` varchar(64) NOT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2暂停 3结束',
            `granted_by` bigint unsigned DEFAULT NULL,
            `started_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `ended_at` datetime DEFAULT NULL,
            `end_reason` varchar(255) DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_organization_member_role` (`organization_id`, `user_id`, `role_code`, `status`),
            KEY `idx_organization_member_user` (`user_id`, `status`),
            KEY `idx_organization_member_org` (`organization_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='组织成员关系历史'
    """,
    "resource_assignment": """
        CREATE TABLE IF NOT EXISTS `resource_assignment` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL COMMENT '被分派的会员/客源用户',
            `organization_id` bigint unsigned DEFAULT NULL,
            `matchmaker_id` bigint unsigned DEFAULT NULL,
            `source` varchar(32) NOT NULL DEFAULT 'manual',
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1生效 2结束',
            `assigned_by` bigint unsigned DEFAULT NULL,
            `effective_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `ended_at` datetime DEFAULT NULL,
            `end_reason` varchar(255) DEFAULT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_resource_assignment_user` (`user_id`, `status`),
            KEY `idx_resource_assignment_matchmaker` (`matchmaker_id`, `status`),
            KEY `idx_resource_assignment_org` (`organization_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会员资源归属历史'
    """,
    "promotion_touch": """
        CREATE TABLE IF NOT EXISTS `promotion_touch` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `code` varchar(128) NOT NULL,
            `promoter_id` bigint unsigned DEFAULT NULL,
            `partner_team_id` bigint unsigned DEFAULT NULL,
            `registered_user_id` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `expires_at` datetime DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_promotion_touch_code` (`code`),
            KEY `idx_promotion_touch_promoter` (`promoter_id`),
            KEY `idx_promotion_touch_registered` (`registered_user_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推广触点'
    """,
    "promotion_attribution": """
        CREATE TABLE IF NOT EXISTS `promotion_attribution` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `promoter_id` bigint unsigned NOT NULL,
            `organization_id` bigint unsigned DEFAULT NULL,
            `touch_id` bigint unsigned DEFAULT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1有效 2结束 3作弊',
            `effective_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `ended_at` datetime DEFAULT NULL,
            `end_reason` varchar(255) DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_promotion_attribution_active` (`user_id`, `status`),
            KEY `idx_promotion_attribution_promoter` (`promoter_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会员推广归属'
    """,
    "partner_team": """
        CREATE TABLE IF NOT EXISTS `partner_team` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `owner_user_id` bigint unsigned NOT NULL,
            `name` varchar(128) NOT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2关闭 3冻结',
            `open_mode` varchar(32) NOT NULL DEFAULT 'manual' COMMENT 'manual/paid',
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_partner_team_owner` (`owner_user_id`),
            KEY `idx_partner_team_status` (`status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='合伙人团队'
    """,
    "partner_membership": """
        CREATE TABLE IF NOT EXISTS `partner_membership` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `team_id` bigint unsigned NOT NULL,
            `promoter_id` bigint unsigned NOT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1正常 2移出 3变更',
            `joined_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `left_at` datetime DEFAULT NULL,
            `changed_by` bigint unsigned DEFAULT NULL,
            `change_reason` varchar(255) DEFAULT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_partner_membership_active` (`promoter_id`, `status`),
            KEY `idx_partner_membership_team` (`team_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='合伙团队成员关系'
    """,
    "business_audit_log": """
        CREATE TABLE IF NOT EXISTS `business_audit_log` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `actor_user_id` bigint unsigned DEFAULT NULL,
            `action` varchar(128) NOT NULL,
            `resource_type` varchar(64) NOT NULL,
            `resource_id` bigint unsigned DEFAULT NULL,
            `before_json` json DEFAULT NULL,
            `after_json` json DEFAULT NULL,
            `reason` varchar(255) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_business_audit_resource` (`resource_type`, `resource_id`),
            KEY `idx_business_audit_actor` (`actor_user_id`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商业化业务审计日志'
    """,
    "matchmaker_service_product": """
        CREATE TABLE IF NOT EXISTS `matchmaker_service_product` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `code` varchar(32) NOT NULL,
            `name` varchar(128) NOT NULL,
            `service_type` tinyint unsigned NOT NULL COMMENT '1付费牵线 3私人定制',
            `price` decimal(12,2) NOT NULL,
            `description` varchar(2000) NOT NULL,
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1上架 2下架',
            `created_by` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_product_code` (`code`),
            KEY `idx_matchmaker_product_status` (`status`, `service_type`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='付费红娘服务商品'
    """,
    "matchmaker_service_contact": """
        CREATE TABLE IF NOT EXISTS `matchmaker_service_contact` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `service_id` bigint unsigned NOT NULL,
            `matchmaker_id` bigint unsigned NOT NULL,
            `wechat_contact` varchar(128) NOT NULL COMMENT '受控展示的红娘微信号',
            `delivered_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_service_contact` (`service_id`),
            KEY `idx_matchmaker_contact_matchmaker` (`matchmaker_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='红娘服务联系方式交付记录'
    """,
    "matchmaker_contact_exchange": """
        CREATE TABLE IF NOT EXISTS `matchmaker_contact_exchange` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `service_id` bigint unsigned NOT NULL,
            `source_user_id` bigint unsigned NOT NULL,
            `target_user_id` bigint unsigned NOT NULL,
            `source_consented_at` datetime DEFAULT NULL,
            `target_consented_at` datetime DEFAULT NULL,
            `status` varchar(32) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/ONE_SIDE_CONSENT/APPROVED/DELIVERED/REVOKED/HIDDEN',
            `delivered_at` datetime DEFAULT NULL,
            `hidden_at` datetime DEFAULT NULL,
            `hidden_reason` varchar(255) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_contact_exchange_service_target` (`service_id`, `source_user_id`, `target_user_id`),
            KEY `idx_matchmaker_contact_exchange_source` (`source_user_id`, `status`),
            KEY `idx_matchmaker_contact_exchange_target` (`target_user_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户双方联系方式授权交换'
    """,
    "matchmaker_service_quota": """
        CREATE TABLE IF NOT EXISTS `matchmaker_service_quota` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `available_count` int unsigned NOT NULL DEFAULT '0',
            `used_count` int unsigned NOT NULL DEFAULT '0',
            `refunded_count` int unsigned NOT NULL DEFAULT '0',
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_quota_user` (`user_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='牵线服务次数账户'
    """,
    "matchmaker_quota_entry": """
        CREATE TABLE IF NOT EXISTS `matchmaker_quota_entry` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `service_id` bigint unsigned NOT NULL,
            `entry_type` varchar(32) NOT NULL COMMENT 'consume/refund',
            `quantity` int unsigned NOT NULL,
            `idempotency_key` varchar(128) NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_matchmaker_quota_entry_key` (`idempotency_key`),
            KEY `idx_matchmaker_quota_entry_service` (`service_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='牵线次数流水'
    """,
    "meeting_request": """
        CREATE TABLE IF NOT EXISTS `meeting_request` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `user_id` bigint unsigned NOT NULL,
            `target_user_id` bigint unsigned NOT NULL,
            `matchmaker_id` bigint unsigned DEFAULT NULL,
            `service_id` bigint unsigned DEFAULT NULL COMMENT '关联红娘服务单',
            `organization_id` bigint unsigned DEFAULT NULL,
            `status` varchar(32) NOT NULL DEFAULT 'SUBMITTED' COMMENT 'SUBMITTED/CONTACTED/ACCEPTED/DECLINED/CLOSED',
            `note` varchar(2000) NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_meeting_request_user` (`user_id`, `created_at`),
            KEY `idx_meeting_request_target` (`target_user_id`, `status`)
            ,KEY `idx_meeting_request_service` (`service_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线下约见意向'
    """,
    "meeting_record": """
        CREATE TABLE IF NOT EXISTS `meeting_record` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `request_id` bigint unsigned NOT NULL,
            `organizer_id` bigint unsigned NOT NULL,
            `organization_id` bigint unsigned DEFAULT NULL,
            `scheduled_at` datetime NOT NULL,
            `location` varchar(255) NOT NULL,
            `status` varchar(32) NOT NULL DEFAULT 'SCHEDULED' COMMENT 'SCHEDULED/REMINDED/CHECKED_IN/COMPLETED/CANCELLED/NO_SHOW',
            `cancel_reason` varchar(255) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_meeting_record_request` (`request_id`),
            KEY `idx_meeting_record_time` (`scheduled_at`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='线下约会记录'
    """,
    "meeting_feedback": """
        CREATE TABLE IF NOT EXISTS `meeting_feedback` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `meeting_id` bigint unsigned NOT NULL,
            `user_id` bigint unsigned NOT NULL,
            `target_rating` tinyint unsigned DEFAULT NULL,
            `matchmaker_rating` tinyint unsigned DEFAULT NULL,
            `continue_intent` tinyint DEFAULT NULL COMMENT '1愿意 2不确定 3不愿意',
            `private_feedback` varchar(2000) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_meeting_feedback_user` (`meeting_id`, `user_id`),
            KEY `idx_meeting_feedback_meeting` (`meeting_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='约会反馈'
    """,
    "commission_rule": """
        CREATE TABLE IF NOT EXISTS `commission_rule` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `beneficiary_type` varchar(32) NOT NULL COMMENT 'service_matchmaker/store/promoter/partner',
            `name` varchar(128) NOT NULL,
            `mode` varchar(16) NOT NULL COMMENT 'fixed/rate',
            `fixed_amount` decimal(12,2) DEFAULT NULL,
            `rate_percent` decimal(7,4) DEFAULT NULL,
            `priority` int NOT NULL DEFAULT '0',
            `status` tinyint NOT NULL DEFAULT '1',
            `version` int unsigned NOT NULL DEFAULT '1',
            `created_by` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_commission_rule_scope` (`beneficiary_type`, `status`, `priority`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='分成规则版本'
    """,
    "product_commission_config": """
        CREATE TABLE IF NOT EXISTS `product_commission_config` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `product_id` bigint unsigned NOT NULL,
            `beneficiary_type` varchar(32) NOT NULL,
            `mode` varchar(16) NOT NULL COMMENT 'fixed/rate',
            `fixed_amount` decimal(12,2) DEFAULT NULL,
            `rate_percent` decimal(7,4) DEFAULT NULL,
            `version` int unsigned NOT NULL DEFAULT '1',
            `status` tinyint NOT NULL DEFAULT '1' COMMENT '1生效 2停用',
            `created_by` bigint unsigned DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_product_commission_active` (`product_id`, `beneficiary_type`, `status`),
            KEY `idx_product_commission_product` (`product_id`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品分成对象配置'
    """,
    "commission_entry": """
        CREATE TABLE IF NOT EXISTS `commission_entry` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `order_id` bigint unsigned NOT NULL,
            `beneficiary_type` varchar(32) NOT NULL,
            `beneficiary_id` bigint unsigned NOT NULL,
            `rule_id` bigint unsigned DEFAULT NULL,
            `rule_version` int unsigned DEFAULT NULL,
            `base_amount` decimal(12,2) NOT NULL,
            `amount` decimal(12,2) NOT NULL,
            `status` varchar(16) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/AVAILABLE/FROZEN/REVERSED',
            `idempotency_key` varchar(160) NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_commission_entry_key` (`idempotency_key`),
            KEY `idx_commission_entry_beneficiary` (`beneficiary_type`, `beneficiary_id`, `status`),
            KEY `idx_commission_entry_order` (`order_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单分成明细'
    """,
    "account_ledger": """
        CREATE TABLE IF NOT EXISTS `account_ledger` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `account_type` varchar(32) NOT NULL COMMENT 'user/store/platform',
            `account_id` bigint unsigned NOT NULL,
            `direction` varchar(8) NOT NULL COMMENT 'CREDIT/DEBIT',
            `amount` decimal(12,2) NOT NULL,
            `state` varchar(16) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/AVAILABLE/REVERSED',
            `source_type` varchar(32) NOT NULL,
            `source_id` bigint unsigned NOT NULL,
            `idempotency_key` varchar(160) NOT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_account_ledger_key` (`idempotency_key`),
            KEY `idx_account_ledger_account` (`account_type`, `account_id`, `state`),
            KEY `idx_account_ledger_source` (`source_type`, `source_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='不可变资金账本'
    """,
    "withdrawal_request": """
        CREATE TABLE IF NOT EXISTS `withdrawal_request` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `account_type` varchar(32) NOT NULL,
            `account_id` bigint unsigned NOT NULL,
            `amount` decimal(12,2) NOT NULL,
            `status` varchar(16) NOT NULL DEFAULT 'PENDING_REVIEW' COMMENT 'PENDING_REVIEW/APPROVED/REJECTED/PROCESSING/SUCCEEDED/FAILED',
            `payee_masked` varchar(128) DEFAULT NULL,
            `reviewed_by` bigint unsigned DEFAULT NULL,
            `reviewed_at` datetime DEFAULT NULL,
            `failure_reason` varchar(255) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_withdrawal_account` (`account_type`, `account_id`, `status`),
            KEY `idx_withdrawal_status` (`status`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='提现申请'
    """,
    "withdrawal_event": """
        CREATE TABLE IF NOT EXISTS `withdrawal_event` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `withdrawal_id` bigint unsigned NOT NULL,
            `event_type` varchar(32) NOT NULL,
            `provider_event_id` varchar(128) DEFAULT NULL,
            `payload_hash` char(64) DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_withdrawal_provider_event` (`provider_event_id`),
            KEY `idx_withdrawal_event_withdrawal` (`withdrawal_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='提现支付事件'
    """,
    "chat_session_request": """
        CREATE TABLE IF NOT EXISTS `chat_session_request` (
            `id` bigint unsigned NOT NULL AUTO_INCREMENT,
            `session_id` bigint unsigned NOT NULL,
            `requester_id` bigint unsigned NOT NULL,
            `responder_id` bigint unsigned NOT NULL,
            `request_type` varchar(32) NOT NULL,
            `payload` json DEFAULT NULL,
            `status` varchar(16) NOT NULL DEFAULT 'PENDING',
            `expire_at` datetime DEFAULT NULL,
            `responded_at` datetime DEFAULT NULL,
            `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_chat_request_session` (`session_id`,`status`,`created_at`),
            KEY `idx_chat_request_responder` (`responder_id`,`status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话结构化请求'
    """,
}
