-- Task8 G4-A Step2：为 ai_search_result 增加 generation 列（原子 generation）
-- 每次 execute 写新 generation 行；成功后原子切换 active generation；旧 generation 行清理。
-- 加法迁移：默认 1，不破坏现有数据；新增 (snapshot_id, generation, stale) 索引。
-- 向后兼容：旧 cursor 在 generation 切换后失效，前端重新拉第一页（InvalidCandidateCursor）。

ALTER TABLE `ai_search_result`
    ADD COLUMN `generation` int unsigned NOT NULL DEFAULT '1'
        COMMENT 'Task8 Step2：原子 generation，每次 execute 写新 generation，成功后原子切换 active generation'
        AFTER `stale`;

CREATE INDEX `idx_ai_search_result_snapshot_generation`
    ON `ai_search_result` (`snapshot_id`, `generation`, `stale`);
