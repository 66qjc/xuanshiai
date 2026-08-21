-- 回滚 Defect 62：恢复 Defect 47 的 user_id_coalesce 生成列与 COALESCE 去重键。
-- 数据损失范围：无；但若已存在同 scope/version 同秒的多个墓碑行（新键允许），
-- 恢复 COALESCE 唯一键将报 1062 冲突并中止回滚——回滚前需先收敛此类墓碑行。

ALTER TABLE `ai_consent_grant` DROP INDEX `uk_ai_consent_user_scope_version`;

ALTER TABLE `ai_consent_grant`
    ADD COLUMN `user_id_coalesce` bigint unsigned
    GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) STORED
    AFTER `user_id`;

ALTER TABLE `ai_consent_grant`
    ADD UNIQUE KEY `uk_ai_consent_user_scope_version`
    (`user_id_coalesce`, `scope`, `version`, `granted_at`);
