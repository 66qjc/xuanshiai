# M06 匹配度外显 — 安全/合规审查报告

> **审查日期：** 2026-08-21
> **审查范围：** D3 前提 2 —— 将 AI 匹配度（`compatibility-rule-v1`）从内部 shadow 解冻为对用户可见的「资料合拍参考」
> **裁决依据：** ADR `2026-08-20-ai-full-feature-baseline.md` D3、PRODUCT.md `ai-policy-2026-08-20-v2`
> **前置 1 状态：** ✅ 已完成（`run_m06_shadow_validation.py` 真实 DeepSeek 端到端 PASS，coverage=0.6，score=69.84，display_eligible=0，consent 403 正常，legacy 未污染）
> **审查结论：** CONDITIONAL PASS — 满足安全底线，附 3 项必须在生产外显前完成的整改条件

## 1. 审查对象

| 对象 | 当前状态 | 外显后状态 |
|---|---|---|
| `ai_compatibility_snapshot.display_eligible` | 恒 0（shadow，不对用户可见） | 灰度切到 1（对用户可见） |
| 对外命名 | 内部 `compatibility-rule-v1` | 固定「资料合拍参考」 |
| `match_score`/`legacy-rule-v1` | 旧推荐流使用，shadow 不触碰 | 外显期双读/排空，不突变 |
| consent scope | `compatibility_shadow` | 见 §5 审查结论 |

## 2. 安全底线评估

### 2.1 防敏感推断 ✅ PASS

维度权重已明确排除敏感信号：

| 纳入 compat 的维度 | 权重 | 排除的维度 |
|---|---|---|
| age | 20 | MBTI（性格标签不参与算分） |
| city_code | 15 | 认证状态（不引入认证等级歧视） |
| marriage_status | 10 | 活跃度（不引入活跃度歧视） |
| education_level | 10 | 会员等级（不引入付费倾斜） |
| height_cm | 10 | 置顶/推荐位（不引入运营干预） |
| income_band | 10 | |
| interest_tags | 15 | |
| relationship_goal | 10 | |

evidence 只引用字段 key（如 `age`、`city_code`），不存对方敏感原文。原因码用模板解释，不生成式解释，避免 AI 编造推断理由。

### 2.2 防误导 ✅ PASS

四道防误导机制已就绪：

1. **coverage 门禁**：双方方向 coverage ≥ 0.50 才生成可比较分数；低于阈值返回 `coverage_insufficient`，不伪造完整分
2. **缺失维度不补负面**：`DIMENSION_UNKNOWN` 标记缺失，不补负面事实（如"对方收入不达标"）
3. **disclaimer 强制展示**：固定文案「仅根据双方当前可见且已确认资料整理，供了解和破冰参考」
4. **AI 不替人承诺**（PRODUCT.md 第 46、346 行）：合拍度只做资料整理/交集提示/破冰建议，禁止"成功率 100%/命中注定/马上脱单"等表达

### 2.3 防侧写/防泄露 ✅ PASS

- 硬门禁先于规则：不可见的 pair 统一 `404 CANDIDATE_NOT_VISIBLE`，不泄露拒绝原因
- 普通日志不写手机号、身份证、精确位置、原始 prompt、原始 Provider 响应、隐藏资料、凭据
- `who_can_see_me=2` fail-closed：查看者认证状态缺失或无法判定时拒绝访问

### 2.4 consent 门禁 ✅ PASS

- 无 consent 时返回 `403 AI_CONSENT_REQUIRED`（已验证）
- consent 撤回后 shadow 变 `blocked`，不展示
- consent 快照写入 shadow，revision 变化标 `stale`

## 3. 风险评估

### 3.1 中风险：consent scope 语义变更未明确

当前 consent scope 名为 `compatibility_shadow`，语义是"允许内部 shadow 运算"。外显后，同一 scope 的语义变为"允许对用户展示匹配度分数"，这是**语义扩展**——用户在 shadow 阶段授权的 consent 是否覆盖外显展示？

**建议**：外显前新增 `compatibility_display` scope，要求用户重新授权。或更新 `compatibility_shadow` 的 consent 文案，明确告知"包含对您展示匹配度参考"。灰度切换时校验新 scope。

### 3.2 低风险：policy_revision 硬编码不一致

`compatibility.py:72` 的 `COMPATIBILITY_POLICY_REVISION = "ai-policy-2026-08-07-v1"` 硬编码为 v1，但 PRODUCT.md 已生效 v2。`AI_PRODUCT_SECURITY_DECISIONS.md` 的 policy_revision 也仍是 v1。

**建议**：外显前同步到 `ai-policy-2026-08-20-v2`。需确认更新是否影响已写入 shadow 快照的兼容性（snapshot 按 algorithm_version 查询，不按 policy_revision 过滤，应无影响）。

### 3.3 低风险：coverage=0.6 偏低

真实 DeepSeek 验证中 coverage=0.6，刚过 0.50 阈值。原因：DeepSeek 在 ideal_partner 场景下未稳定抽取 `income_band`，导致 1 个 `DIMENSION_UNKNOWN`。

**建议**：外显前优化 prompt 工程，提升 ideal_partner 投影的 income_band 抽取率。或在 disclaimer 补充"部分维度信息缺失时不计入评分"的说明。

## 4. 生产门禁

外显属于生产启用范畴，需满足全部门禁（与 D4 生产上线门禁一致）：

- `ai_policy_approved` + `ai_provider_approved` + 有效 `ai_retention_policy_version`
- 生产 provider 非 mock（当前 `deepseek`，满足）
- 合规批准 + Provider 批准
- `verify_ai_release.py --target production` 无 blocker

## 5. 审查结论

**CONDITIONAL PASS**。安全底线（防敏感推断、防误导、防侧写、consent 门禁）全部满足。生产外显前必须完成以下 3 项整改：

| # | 整改项 | 优先级 | 责任 |
|---|---|---|---|
| C1 | consent scope 语义：新增 `compatibility_display` scope 或更新 `compatibility_shadow` 文案，灰度时校验 | 高 | 产品 + 后端 |
| C2 | policy_revision 同步到 v2（代码常量 + 安全决策文档） | 中 | 后端 |
| C3 | prompt 工程优化：提升 ideal_partner 场景 income_band 抽取率，目标 coverage ≥ 0.7 | 中 | 后端 |

整改完成 + 安全审查签字 + 灰度方案就绪后，方可启动 D3 外显灰度切换。
