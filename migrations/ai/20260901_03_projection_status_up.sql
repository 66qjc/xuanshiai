-- Phase 4 P4-01 —— projection_status 表:追踪每个 (user, kind) 投影是否对
-- 搜索/匹配/推荐可见。
--
-- 设计动机:
-- ai_feature_projection 是"数据行",projection_status 是"准入位"。
-- 即便数据行存在,只要 projection_status 不在 active,下游消费者一律不读。
-- 这样保证:发布/删除/撤回授权/恢复旧版 都能在毫秒级别让所有下游
-- (search / compatibility / recommend) 一致地屏蔽/放行。
--
-- 状态机:
-- pending     -- profile_projection 任务入队,等待 worker 完成(搜索/匹配/推荐
--                视为"无投影",不参与候选计算)
-- active      -- 唯一有效状态,下游可读 ai_feature_projection 的最新匹配行
-- invalidated -- 有新版本发布,旧 active 投影被踢到 invalidated(数据保留)
-- deleted     -- 画像被删除/授权被撤回,数据保留但下游永远不读
-- failed      -- 投影构建失败,可由下一次发布重新置 pending
--
-- UNIQUE(user_id, kind):每个用户每种 kind 只能有一行"最新状态"
-- 切换状态走 UPDATE;不要 INSERT ON DUPLICATE KEY 之外的方式。
--
-- 已知 kind 枚举(契约外加注):
-- personal_searchable         -- 搜索(给其他用户搜到)
-- personal_compatibility      -- 匹配度(本人 compatibility 计算)
-- ideal_partner_preference    -- 愿遇之相(推荐/匹配)

CREATE TABLE IF NOT EXISTS `ai_profile_projection_status` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
    `user_id` bigint unsigned NOT NULL,
    `kind` varchar(32) NOT NULL COMMENT 'personal_searchable/personal_compatibility/ideal_partner_preference',
    `status` varchar(24) NOT NULL DEFAULT 'pending' COMMENT 'pending/active/invalidated/deleted/failed',
    `source_revision` int unsigned DEFAULT NULL COMMENT '关联的 ai_profile_revision.id(同主体),非该 kind 主体时为 NULL',
    `projection_id` bigint unsigned DEFAULT NULL COMMENT '关联的 ai_feature_projection.id',
    `last_error` varchar(255) DEFAULT NULL,
    `activated_at` datetime DEFAULT NULL,
    `invalidated_at` datetime DEFAULT NULL,
    `deleted_at` datetime DEFAULT NULL,
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_ai_profile_projection_status_user_kind` (`user_id`, `kind`),
    KEY `idx_ai_profile_projection_status_status` (`status`, `updated_at`),
    KEY `idx_ai_profile_projection_status_user_status` (`user_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 投影准入位:每个 (user, kind) 只能有一行 active 投影'
