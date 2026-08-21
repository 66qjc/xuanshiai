# AI 产品与安全决策

- policy_revision: `ai-policy-2026-08-20-v2`（取代 v1；v1 的 text-only + Mock + M06 shadow-only 边界已由 ADR `ai-full-feature-baseline` D1-D4 解冻）
- who_can_see_me=2 表示查看者 realname_status=2；查看者认证状态缺失或无法判定时拒绝访问（fail-closed）。
- 真实 LLM provider（DeepSeek）已解冻（D1）；语音 ASR/TTS 已解冻（D2），生产启用需先满足全部前置门禁。
- AI 输出经用户确认后才能发布；认证事实只能来自认证系统。
- M03 只编译当前结构化筛选 allowlist，不生成 SQL。
- 字段 allowlist：`age`、`city_code`、`marriage_status`、`education_level`、`height_cm`、`income_band`、`occupation_group`、`interest_tags`、`lifestyle_tags`、`relationship_goal`。
- consent scope 允许：`profile_text_extract`、`search_parse`、`compatibility_shadow`；语音启用后新增 `voice_transcribe`，M06 外显后新增 `compatibility_display`。
- M06 匹配度：shadow 阶段 `display_eligible=false`、算法名 `compatibility-rule-v1`；shadow 验证通过 + 安全审查通过 + 灰度切换后解冻外显（D3）。外显前 legacy `match_score`/`legacy-rule-v1` 保持不变。
- 生产启用是功能门禁：必须同时具备 `ai_policy_approved`、`ai_provider_approved` 和有效的 `ai_retention_policy_version`，生产 provider 非 mock，并取得合规批准与 Provider 批准。
- 合规、Provider 或保留策略未批准，或任一生产启用门禁未满足时返回 HTTP 503 AI_FEATURE_DISABLED（retryable=false）。
- 普通日志不得写入手机号、身份证、精确位置、原始 IP、原始 prompt、原始 Provider 响应、隐藏资料或凭据。
