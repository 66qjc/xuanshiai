# 墨相师对话建构（Moxiang Conversational Profile）设计文档

> 版本：v1.0 ｜ 日期：2026-08-30 ｜ 状态：待用户评审
> 依据：用户需求（用墨相师替代固定题库问答，自然对话构建画像，保留约 60% 门槛与话题界限）
> 关联：`docs/superpowers/plans/` 各良配阶段计划；`良配思维导图总结/良配AI体验完善方案.md`（F4/F5 阶段 2 成果为本设计地基）

---

## 一、背景与动机

现状两条链路是断开的：

1. **画像建构**（`ai_profile_session` kind=build）由固定题库 `_PROFILE_QUESTION_BANK`（`app/services/ai/profile.py:186`）逐题推进，10 题固定文案固定顺序，无接话、无追问、无承认，用户反馈"像填表不像聊天"。
2. **墨相师**（`/api/v1/voice/moxiang-master` WS）人设即"像朋友一样在对话中自然收集信息"（`app/services/ai/prompts/moxiang_master.py`），但实现为纯聊：对话历史内存级（WS 连接生命周期）、**不做画像字段抽取、不落库**（`app/api/routes/voice_moxiang.py` 头注）。

本设计把两条链路接通：墨相师对话成为画像建构的默认方式，聊天内容实时抽取落库；题库问答降级为故障兜底。

## 二、已确认决策

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 墨相师与题库关系 | 默认墨相师；题库仅作降级兜底，无常驻入口 |
| D2 | 门槛语义 | 硬字段必达 + entry 条目折算（见第六节） |
| D3 | 确认节奏 | 阶段性轻确认卡片，发布前总览确认 |
| D4 | 话题边界 | 提示词层 + 抽取层双层过滤 |
| D5 | 实现架构 | 扩展墨相师 WS 通道（方案 A），确认走 REST 复用 |
| D6 | 入口归宿 | 建构入口归墨相师（我的页直进）；画像页保留为档案页 |

## 三、产品口径（PRODUCT.md 变更草案，实施第一步落库）

- **建构方式**：画像建构默认通过墨相师对话完成；对话中 AI 围绕白名单主题引导，用户可随时闲聊，AI 温和拉回。
- **话题白名单**：自我认知、三观与感情观、生活方式与作息饮食、物理位置（城市/居住地）、基本情况（年龄/婚姻状态/学历/职业/身高/收入）。
- **门槛**：门槛 = 硬信息（城市、年龄、婚姻状态）全部确认，且折算总分 ≥ 60%（阈值服务端可配置）。条目折算上限 2 分。
- **确认节奏**：对话中阶段性轻确认（可折叠卡片，不阻塞对话）；发布前一次总览确认；AI 不擅自入库任何未经确认的内容。
- **单入口**：「我的」页画像建构入口直达墨相师；画像档案（成稿/条目管理/更新）经画像页访问；题库问答仅在墨相师通道故障时由用户主动选用。

## 四、会话模型与数据流

### 4.1 会话创建

- WS `session_start` 新增可选字段 `mode: "profile_build"`。携带时：fail-closed 检查（AI 门禁 + 画像授权 consent 复用现有 `ai_consent_grant` 纪律）→ 创建 `ai_profile_session`，**`session_kind='master'`**（新枚举值，幂等迁移）。
- 不携带 `mode` 的连接行为完全不变（纯聊模式向后兼容，既有测试不破坏）。
- master 会话**无题库推进**（复用 update 会话"current_question 恒 None"路径），但继承 build 的草稿、确认、发布、投影、叙事语义。
- `input_mode` 沿用现有列并随语音/文字互切更新（复用 `update_session_input_mode`）。

### 4.2 每轮对话流程

```
用户消息(文字/语音转写)
  → 落库 ai_profile_turn (role=user, source_type=master_text|master_voice)
  → 墨相师 LLM 生成回复（prompt 注入：缺失硬字段清单 + 已确认画像摘要 + 进度状态）
  → 回复落库 (role=assistant)
  → 入队 master_extract 任务（幂等键 session_id+turn_id，每轮一任务）
       ├─ structured 补丁：仅硬字段 + 其余 structured 白名单，枚举校验
       ├─ entry 补丁：分类 + 200 字自由文本
       └─ 越界内容：空补丁（非失败），warning 计数
  → draft fields (confirmation_status='pending')
  → WS 推送 progress / confirm_card（按第七节时序）
```

- master_extract 复用阶段 2 的抽取引擎（entry 抽取 handler、update-intent 的澄清语义不适用处简化：建构阶段澄清由墨相师对话本身承担）。
- 抽取内容 = 本轮 turn + 尚未抽取的历史轮（断线补抽）。

### 4.3 确认与发布

- 确认/编辑/删除**走现有 REST 端点**（幂等、审计、鉴权、测试现成）；WS 仅推送状态变更通知。
- 门槛达标 → `publish_ready` 推送 → 用户跳画像档案页总览确认 → 现有 publish 流程 → 投影/叙事。
- 发布后叙事层自动进入墨相师 prompt 上下文（`_load_narrative_context` 已支持，无新增工作）。

## 五、WS 协议扩展

仅新增后端→前端 3 个推送类型，消息协议其余不变：

```json
{"type": "progress", "percent": 45, "hard_done": 1, "hard_total": 3,
 "entry_score": 1.5, "gate_met": false}
{"type": "confirm_card", "card_id": "...",
 "items": [{"draft_field_id": "...", "kind": "entry|structured",
            "category": "...", "content": "...", "display_value": "..."}]}
{"type": "publish_ready", "summary": "..."}
```

- 重连恢复：`session_ready` 附带 pending 条目摘要与当前进度（状态在库，不丢）。
- 前端→后端确认动作走 REST，不新增 WS 上行类型。

## 六、进度与门槛折算

- **硬字段**常量 `MASTER_HARD_FIELD_KEYS = {city_code, age, marriage_status}`（settings 可覆盖），必须全部确认。
- 折算：总分 = 硬字段(1 分/个) + 其余 structured 字段(1 分/个) + entry 条目(0.5 分/条，**上限 2 分**)；分母 10。
- `percent = min(100, 总分/10×100)`；门槛 = 硬字段 3/3 且 percent ≥ 阈值（新 setting `AI_MASTER_BUILD_GATE`，默认 0.60，独立于既有 build 阈值 0.7）。
- 折算上限防止刷条目绕过硬信息采集；只有 `confirmation_status='confirmed'` 的条目计分。

## 七、确认卡片时序

- 触发：抽取任务完成后，pending 条目 ≥ 2 或距上一张卡片 ≥ 3 轮 → 推 `confirm_card`。
- 卡片可折叠、**不阻塞输入**；用户可继续说话，卡片保留在消息流；单条点开可编辑/删除（REST）。
- 每次确认动作后服务端重算并推送 `progress`；`gate_met` 首次为 true 时附推 `publish_ready`。

## 八、话题边界双层过滤

1. **提示词层**：`moxiang_master.py` `_SYSTEM_HEADER` 增白名单主题清单与拉回话术指引（越界提问温和拉回，不生硬拒绝）。
2. **抽取层**：master_extract handler 的 schema 仅接受白名单 field_key/category；越界文本返回空补丁并记 warning 计数（**不落内容**，隐私纪律）。
3. 敏感内容沿用现有内容审核链路；不编造用户未提供信息的纪律不变。

## 九、降级与 fail-closed

- **建连时**：provider 健康检查失败 → 推 `error`（沿用现有协议），前端显示"墨相师通道暂不可用"并提供"快速问答"按钮（用户主动点击才出现）→ 跳画像页题库视图。
- **对话中**：provider 故障走现有 error/重试协议；turn 已持久化，重连 `session_start`（同 session）续聊并补抽。
- 题库兜底 = 现有 build 会话链路原样保留，仅失去默认入口。

## 十、前端改动（xuanshiai-vue）

- **我的页**：画像建构入口直指 `/pagesSub/profileExtra/my-portrait-master`。
- **墨相师页**（`my-portrait-master.uvue`）：
  - +进度条（percent、硬字段 n/3、门槛提示）；
  - +确认卡片组件（参考画像页条目确认交互）；
  - +`publish_ready` 后"去成稿"按钮（跳画像档案页）；
  - +档案页跳转入口（"我的墨相"）；
  - -退出按钮修复：`getCurrentPages().length <= 1` 时 fallback `uni.reLaunch` 首页；`‹` 改 ✕ 图标并加大热区。
- **画像页**（`my-portrait.uvue`）：去默认建构首屏，保留成稿三屏/条目管理/更新会话（纯档案页）；删除页内墨相师入口卡片；题库建构视图保留代码（降级路径用）。
- WS 消息处理扩展 3 个新类型；文字/语音两模式均生效（墨相师页已有双模式）。

## 十一、测试与验收

- 后端单测：master 会话创建/门禁、turn 持久化、master_extract（白名单过滤/幂等/断线补抽/空补丁语义）、折算公式边界（上限 2 分、确认才计分）、门槛判定、纯聊模式向后兼容。
- 后端集成（real_db）：会话→对话→抽取→确认→门槛→发布→叙事上下文回归墨相师。
- 回归基线：全量 835+ 测试绿，零新增红。
- 前端：HBuilderX cli mp-weixin 编译验证；画像页测试文件扩展确认卡片/进度条逻辑。
- 端到端：模拟文字 WS 对话 5-8 轮覆盖硬字段 + 条目抽取 + 确认 + 发布。

## 十二、范围外（本期不做）

- 墨相师页内嵌完整档案管理（档案页独立保留）。
- 推荐页/搜索消费 entry 的深度耦合（沿用 entry_digest 现状）。
- 真机语音链路质量调优（依赖真机验证线）。
- 统一会话引擎重构（build/update/master 三合一，远期）。

## 附：主要涉及文件

- 后端：`app/api/routes/voice_moxiang.py`（mode/协议/会话绑定）、`app/services/voice/master_orchestrator.py`（prompt 注入）、`app/services/ai/prompts/moxiang_master.py`（白名单）、`app/services/ai/profile.py`（session_kind=master、master_extract、折算）、`app/core/config.py`（阈值）、`app/db/ai_schema.py`（枚举迁移）、`app/services/ai/gateway.py`（任务注册）。
- 前端：`pages.json`、`my-portrait-master.uvue`、`my-portrait.uvue`、我的页入口。
