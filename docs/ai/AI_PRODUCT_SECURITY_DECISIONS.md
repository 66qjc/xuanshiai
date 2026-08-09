# AI 产品与安全决策

- policy_revision: `ai-policy-2026-08-07-v1`
- who_can_see_me=2 表示查看者 realname_status=2；查看者认证状态缺失或无法判定时拒绝访问（fail-closed）。
- 一期只支持文字输入和 mock provider；生产 Provider、ASR、向量和学习排序均关闭。
- AI 输出经用户确认后才能发布；认证事实只能来自认证系统。
- M03 只编译当前结构化筛选 allowlist，不生成 SQL。
- 字段 allowlist：`age`、`city_code`、`marriage_status`、`education_level`、`height_cm`、`income_band`、`occupation_group`、`interest_tags`、`lifestyle_tags`、`relationship_goal`。
- consent scope 仅允许：`profile_text_extract`、`search_parse`、`compatibility_shadow`。
- match_score 保持 legacy-rule-v1；新兼容度仅内部 shadow，算法名 compatibility-rule-v1。
- 生产启用是功能门禁：必须同时具备 `ai_policy_approved`、`ai_provider_approved` 和有效的 `ai_retention_policy_version`，并取得合规批准与 Provider 批准。
- 合规、Provider 或保留策略未批准，或任一生产启用门禁未满足时返回 HTTP 503 AI_FEATURE_DISABLED（retryable=false）。
- 禁止在生产环境使用 Provider、ASR、向量或学习排序；上述能力不因本决策而获得上线授权。
- 普通日志不得写入手机号、身份证、精确位置、原始 IP、原始 prompt、原始 Provider 响应、隐藏资料或凭据。
