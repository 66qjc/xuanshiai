-- 回滚 G4-A 补漏：删除 ai_search_snapshot 的 updated_at 列。
-- 数据损失范围：仅行更新时间戳，无业务字段；回滚后删除/失效路径不得再写该列。

ALTER TABLE `ai_search_snapshot` DROP COLUMN `updated_at`;
