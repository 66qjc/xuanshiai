-- Defect 62：回退 Defect 47 的 user_id_coalesce 去重，唯一键改回 user_id。
-- COALESCE(user_id,0) 使同一秒内（DATETIME fsp=0）两次授权产生的墓碑行在删除
-- 擦洗时撞唯一键（1062）；且删除后重新授权会与旧墓碑同秒撞键。墓碑行只能由
-- 存活期唯一的行擦洗而来，不会出现重复，无需参与去重。NULL 天然豁免唯一键。
-- 数据损失范围：无业务字段，仅索引/生成列变更。

ALTER TABLE `ai_consent_grant` DROP INDEX `uk_ai_consent_user_scope_version`;

ALTER TABLE `ai_consent_grant` DROP COLUMN `user_id_coalesce`;

ALTER TABLE `ai_consent_grant`
    ADD UNIQUE KEY `uk_ai_consent_user_scope_version`
    (`user_id`, `scope`, `version`, `granted_at`);
