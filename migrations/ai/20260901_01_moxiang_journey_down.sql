-- Phase 1 Contract v1.1 P1-A：墨相师候选理解池 + 构建邀请 + journey_stage + profile_dimension 回滚
-- 必须先删除所有依赖列与生成列的索引/约束，再删除列与表。
-- 文档注释列出需要 DROP 的索引/约束名供运维排查，实际执行通过 DROP TABLE 让
-- 生成列与 unique key 一并消失（InnoDB 不支持单独 DROP GENERATED COLUMN）。

ALTER TABLE `ai_profile_draft_field`
    DROP KEY `idx_draft_field_dimension`,
    DROP COLUMN `profile_dimension`;

ALTER TABLE `ai_profile_revision_field`
    DROP KEY `idx_revision_field_dimension`,
    DROP COLUMN `profile_dimension`;

ALTER TABLE `ai_profile_session`
    DROP COLUMN `journey_stage`;

-- 索引清单（随 DROP TABLE 自动回收）：
--   uk_candidate_session_hash
--   uk_ai_profile_build_invite_id
--   uk_ai_profile_build_invite_no
--   uk_ai_profile_build_invite_pending
--   active_slot (GENERATED ALWAYS ... STORED)
DROP TABLE IF EXISTS `ai_profile_build_invite`;
DROP TABLE IF EXISTS `ai_profile_candidate`;