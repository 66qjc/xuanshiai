-- 补充 ai_profile_draft.session_id 与 ai_profile_summary(user_id,subject,created_at) 索引
-- 这两个热读路径此前全表扫(GET session / turn 提交 / 叙事页)
ALTER TABLE `ai_profile_draft`
  ADD KEY `idx_ai_profile_draft_session` (`session_id`, `status`);

ALTER TABLE `ai_profile_summary`
  ADD KEY `idx_ai_profile_summary_user_subject` (`user_id`, `subject`, `created_at`);
