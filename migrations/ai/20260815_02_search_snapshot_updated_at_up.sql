-- G4-A 补漏：删除/失效路径写 updated_at（delete_ai_profile 代际 fence 与
-- delete_search_snapshot），但 ai_search_snapshot 未随 20260809_01_hardening
-- 的其他 AI 表补齐该列，真实库 UPDATE 报 1054 Unknown column。
-- 加法迁移：仅补列，不破坏现有数据。

ALTER TABLE `ai_search_snapshot`
    ADD COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
        COMMENT '行更新时间（删除/失效标记路径写入）'
        AFTER `created_at`;
