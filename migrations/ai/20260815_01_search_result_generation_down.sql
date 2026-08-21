-- Task8 G4-A Step2：回滚 ai_search_result 的 generation 列。
-- 数据损失范围：执行此回滚后，所有已写入的 generation 信息丢失，active generation
-- 追踪退化为默认值 1。回滚前必须确认所有 snapshot 的 active generation 已收敛到 1，
-- 否则 cursor 校验将失效，前端需重新拉第一页。在 disposable database 上验证后再执行。
-- 索引 idx_ai_search_result_snapshot_generation 一并删除。

DROP INDEX `idx_ai_search_result_snapshot_generation`
    ON `ai_search_result`;

ALTER TABLE `ai_search_result` DROP COLUMN `generation`;
