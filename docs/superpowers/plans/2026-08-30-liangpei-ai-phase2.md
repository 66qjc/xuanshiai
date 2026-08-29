# 良配 AI 体验完善 — 阶段2 实施计划（画像条目主线 + 搜索侧增强）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把画像从"10 个受控字段"升级为良配文档的条目式模型，并落地对话式追加更新（New 角标 + 不覆盖）、语音/文字双模式互切、"猜你喜欢"AI 化、搜索中途模糊候选，对齐《良配AI体验完善方案.md》的 WP-P1 / WP-P4 / WP-P5 / WP-S3 / WP-S2（差距 F4/F5/F6/F7/F8/F10）。

**Architecture:** 全部改动落在 xuanshiai-backend 现有三层结构（routes → services → db/ai_schema）。条目模型走**并存扩展**（决策 D2）：structured 字段链路零改动，entry 作为新 `field_kind` 并入现有 draft_field/revision_field 表；旧库补列沿用幂等 `ensure_*_columns` 模式；update 会话复用现有 turns/extract 任务机制，仅换澄清式 prompt；语音落库复用 `extract_profile_turn` 同一落库路径；搜索建议与模糊候选全部复用 `ai_task` 队列与快照表，不新建基础设施。

**Tech Stack:** FastAPI + SQLAlchemy(async) 裸 SQL（无 ORM）、MySQL、pytest + real_db 集成测试（compose.ai-test.yml 专用库）、pydantic-settings。

**规格来源:** `D:\Users\ASUS\Desktop\宣誓爱\良配思维导图总结\良配AI体验完善方案.md`（§四 WP-P1/P4/P5、§五 WP-S3/S2、§9.2 决策点）。注：S3 在方案路线图原属阶段1、S2 原属阶段4，按用户指示并入本阶段一并实施。

**代码基线（2026-08-30，阶段0+1 完成后）：**
- 分支 `feat/liangpei-ai-phase0-1`（基于 6aaee00，HEAD 2dce8de）。开工前确认该分支去向：已合并则在其目标分支新建 `feat/liangpei-ai-phase2`；未合并则直接在本分支追加。
- 测试基线：742 过 / 5 红 / 19 跳过。5 个红全部来自用户并行 WIP（voice×3、worker lease、ai lease），**不是本计划引入的**；DoD 按"不新增红"判定。
- 阶段0+1 已提供：`ai_task.progress_percent` 进度列与搜索阶段进度（validating=10/filtering=30/ranking=85/completed=100）、`AI_PROFILE_MIN_FIELDS` 可配置阈值（默认 7）、叙事 confirm/regenerate 路由、外显灰度 `AI_COMPATIBILITY_DISPLAY_MODE`。

---

## Global Constraints

- **服务层不 commit**：所有 `app/services/ai/*.py` 业务函数保持"不 commit，由调用方控制事务"（docstring 已声明的契约），提交点只在路由成功路径显式 `await db.commit()`。
- **生产 fail-closed 门禁不变**：provider=mock 或审批不全时 503（`app/services/ai/flags.py`），本计划不放松任何门禁。
- **旧库迁移只用幂等补列**（`SHOW COLUMNS` → `ALTER TABLE ADD COLUMN`），不改已有列、不删数据；新 helper 必须在 `database_setup_marriage.py`（约 :2553 的 ensure 调用区）接线。
- **publish 门槛与进度语义不变（本计划锁定）**：`min_confirmed_fields_to_publish()`（profile.py:1824）与 `progress_value()`（profile.py:679）仍按 structured allowlist 字段的 confirmed 计数，**entry 条目不计入门槛与进度**——条目是丰富度增强，不改变建构门槛边界（不破坏 D3 决策与既有测试）。
- **entry 内容 ≤200 字**：服务层校验（超限 `AI_INPUT_INVALID`）+ DB `VARCHAR(200)` 双保险。
- **追加不覆盖**：除非用户显式删除（复用 `delete_ai_profile_field`，profile.py:3173），旧条目永远保留。
- **会话唯一活动槽位纪律不变**：`uk_ai_profile_session_active (user_id, subject, active_slot)` 仍保证每用户每 subject 只有一个活动会话；update-intent 在已有活动会话时返回 `AI_INPUT_INVALID` 提示先完成/放弃，**不静默关闭用户会话**。
- **脱敏纪律**：S2 的 partial 集仅含 hard（确定性）条件全部命中的用户，绝不含仅 soft 条件命中者；`is_fuzzy` 行沿用现有快照出参脱敏。投影 `entry_digest` 为摘要文本且遵守 `visibility_class` 纪律（ideal_partner_preference 仍 self_only）。
- **LLM faithfulness**：entry 抽取、澄清式追问、搜索建议的 prompt 一律只准基于用户已有资料归纳，禁止编造新偏好；出参做 allowlist/分类校验。
- **本阶段不新增灰度开关**：`is_new` 为纯读端派生；S3 自带空投影降级路径（回显标签）即天然开关。
- 每个任务完成必须全量跑 `python -m pytest tests/ -x -q` 通过后才能 commit；集成测试：`python -m pytest tests/integration/ai -q`（`AI_TEST_DATABASE_URL`，默认 127.0.0.1:3307）。
- 注释风格：中文，说明"为什么/约束"；commit message 用 `feat:`/`fix:`/`docs:`/`chore:` 前缀。
- **工作区红线**：`ai_playground/` 未跟踪文件与 `app/api/routes/` 下 router/main.py 中用户并行改动（如 ai_playground 注册）**不得进入任何提交**；每次 commit 前用 `git status` + `git diff --stat` 核对暂存清单。
- 决策点沿用已锁定结论：D2 并存扩展、D3 阈值可配置默认 7、D6 外显灰度纪律。

**工作目录：** 除 Task 1 外均在 `D:\Users\ASUS\Desktop\宣誓爱\xuanshiai-backend`（独立 git 仓库）。

---

### Task 1: PRODUCT.md 产品范围更新（治理前置）

**Files:**
- Modify: `D:\Users\ASUS\Desktop\宣誓爱\PRODUCT.md`（工作区根，约 461 行；已有"AI 体验增强（良配对齐，2026-08）"章节）

**Steps:**
- [x] 在该章节后追加"### AI 体验增强·阶段2（条目式画像与搜索体验，2026-08）"，含 5 小节，每节写清产品定义与边界：
  1. **条目式画像**：分类（基本情况/工作状态/外形特征/性格特征/价值观/兴趣爱好/作息习惯/饮食习惯/生活规划）× 方向（关于我/关于对方）× 自由文本 ≤200 字；与既有 10 个结构化字段并存；主业务 `user_profile` 仍不直写，前端读 AI 投影/叙事接口。
  2. **对话式画像更新**：随时陈述新期望→AI 追问澄清→确认后**追加**为 New 条目；旧条目不覆盖（除非用户显式删除）；New 角标规则（最新发布版本首现的条目置顶标新）。
  3. **语音/文字双模式**：同一会话内可切换，进度与已确认字段无缝延续；语音抽取结果与文字模式同库同状态机。
  4. **AI 猜你喜欢**：基于用户已有画像归纳搜索词（不编造），每用户每日限频，空画像降级为标签回显。
  5. **搜索中途模糊候选**：进度约 30% 起先返回模糊命中集（仅硬性条件命中，前端模糊样式展示），完成后切换完整结果；写明脱敏边界。
- [ ] 自查：不新增与既有章节冲突的范围表述（尤其"发布阈值约 67%"行保持不动）。

**验收：** 纯文档，无代码；小节边界与 Global Constraints 一致。

---

### Task 2: WP-P1a 条目模型表结构与迁移（F4 地基一）

**Goal:** draft/revision 字段表获得 entry 能力的物理载体；structured 默认值保证存量数据零影响。

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_profile_draft_field` :173 与 `ai_profile_revision_field` :215 的 CREATE TABLE 增列；新增 `ensure_ai_profile_entry_columns(cursor)`，仿 `ensure_ai_task_columns` :518 模式）
- Modify: `database_setup_marriage.py`（ensure 调用区接线）
- Modify: `tests/integration/ai/conftest.py` 或新增迁移测试文件

**表变更（两张表同构）：**
```sql
field_kind        ENUM('structured','entry') NOT NULL DEFAULT 'structured'
category          VARCHAR(32) DEFAULT NULL      -- entry 专用，受分类枚举约束（服务层校验）
content           VARCHAR(200) DEFAULT NULL     -- entry 专用正文；structured 仍走 value_json
replaces_field_key VARCHAR(64) DEFAULT NULL     -- entry 改写时指向被替换条目的 field_key（Task 6 使用，这里一并补列避免二次迁移）
```

**Steps:**
- [x] RED：集成测试断言 `information_schema.COLUMNS` 中两张表各含 4 新列，且存量行 `field_kind='structured'` 默认成立；旧库场景（先建旧结构再跑 ensure）补列后断言通过、**连跑两遍不报错**（幂等）。
- [x] GREEN：CREATE TABLE 更新 + ensure helper + `database_setup_marriage.py` 接线。
- [x] 全量回归：`python -m pytest tests/ -x -q`（默认值保证现有用例不感知新列）。

**验收：** 新列存在且幂等；全量测试不新增红。
**Commit:** `feat(schema): 画像条目模型列（field_kind/category/content/replaces_field_key）幂等迁移`

---

### Task 3: WP-P1b entry 抽取/确认/编辑链路（F4 地基二）

**Goal:** 会话能答出条目、确认条目、编辑条目（200 字上限）；structured 链路一行不动。

**Files:**
- Modify: `app/services/ai/profile.py`（抽取 handler 注册区：新增 entry 抽取路径；`confirm_profile_draft` :2173 / `_update_draft_field_status` :2302 支持整条 confirm/edit/delete；新增 entry 专用校验）
- Modify: `app/schemas/`（entry 出入参模型 + 分类枚举）
- Modify: `app/api/routes/ai_profile.py`（若现有字段确认/编辑端点需要区分 field_kind 则扩展之，尽量复用）
- Modify: `tests/`（单测 fake + 集成）

**设计要点：**
- 分类枚举冻结为 9 个 slug：`basics/occupation/appearance/personality/values/interests/routine/diet/life_plan`（对应方案中文分类），`frozenset` 常量放 schemas，越界 `AI_INPUT_INVALID`。
- entry 的 `field_key` 生成规则：`entry_{category}_{8位hex}`（≤64 字符，满足 `uk_ai_profile_draft_field (draft_id, field_key)` 唯一）；`value_json` 保持 NULL，正文在 `content`。
- entry 确认语义 = 整条 confirm / edit（改 content，重算 content_hash）/ delete；复用 `confirmation_status` 状态机，不新造状态。
- 抽取 prompt 约束：只从用户原话归纳，faithfulness 约束写进 prompt；fake provider 单测注入固定 entry 结果。
- **entry 不计入 publish 门槛与进度**（见 Global Constraints；在 `confirmed_fields` / `progress_value` 消费处加断言测试防回归）。

**Steps:**
- [x] RED（单测）：fake 抽取产出 entry → 出现于草稿候选；确认后 `confirmation_status='confirmed'`；编辑 201 字返回 `AI_INPUT_INVALID`；非法分类返回 `AI_INPUT_INVALID`；`progress_value` 不受 entry confirmed 影响（门槛断言）。
- [x] GREEN：实现抽取/确认/编辑路径。
- [x] RED（集成）：真库写入 entry 字段行 → confirm → rollback 后重读持久化成立（沿用阶段0+1 的 rollback-then-assert 模式）。
- [x] 全量回归（重点 `tests/test_ai_profile*.py` structured 路径零变化）。

**验收（方案 WP-P1 前半）：** 创建会话→答出 entry（如"价值观：欣赏阳光开朗、品行端正的人"）→确认→草稿含该条目；单条可编辑且 200 字上限生效；旧结构化字段回归全绿。
**Commit:** `feat(profile): entry 条目抽取、确认与编辑链路（200字上限+分类枚举）`

---

### Task 4: WP-P1c 叙事/投影/读取端消费 entry（F4 收口）

**Goal:** 发布后的条目进入叙事层与投影，读取端按分类返回；搜索/匹配度侧拿到可消费的 `entry_digest`。

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_feature_projection` :350 增 `entry_digest TEXT NULL`；并入现有 `ensure_ai_projection_columns` :485）
- Modify: `app/services/ai/profile.py`（`profile_projection_handler` :3317 写入 entry_digest；`generate_profile_narrative_handler` :3513 的 prompt 输入带条目摘要；revision_field 快照写入含 entry 列）
- Modify: `app/api/routes/ai_profile.py` + schemas（字段读取接口按 `category` 分组返回 entry，含 `field_kind` 标记）
- Modify: `database_setup_marriage.py`（ensure 已接线则无需再动，核对）

**设计要点：**
- `entry_digest` = 该用户某投影维度下全部已发布条目的紧凑摘要（每条一行"分类：内容截断"），**不含** visibility 禁止内容；personal 维度供搜索/匹配度消费，ideal_partner 维度保持 self_only。
- 叙事 prompt 输入在结构化字段之外附条目摘要，输出叙事自然融合条目（faithfulness 约束同 Global）。
- 读取端出参：`{field_kind, category, content, confirmation_status, updated_at}`，structured 条目原样保留既有字段。

**Steps:**
- [ ] RED：发布含 entry 的草稿 → projection 行 `entry_digest` 非空且含分类前缀；narrative 任务 payload 含条目摘要；字段接口按分类分组。
- [ ] GREEN：实现三处消费。
- [ ] 集成测试：rollback-then-assert 投影行；旧 structured-only 用户投影 `entry_digest` 为 NULL 的回归用例。

**验收（方案 WP-P1 验收原文）：** 创建会话→答出 entry→确认→发布→`GET /profiles/{subject}/narrative` 与字段接口均能按分类返回条目；旧结构化字段回归测试全绿。
**Commit:** `feat(profile): 条目进入叙事与投影（entry_digest）+ 分类读取端`

---

### Task 5: WP-P4a update-intent 对话式追加会话（F5）

**Goal:** "Hi，随时可以找我更新你的画像哦"——用户陈述新期望，AI 围绕陈述追问澄清，产出可确认的 entry 级 patch。

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_profile_session` :100 增 `session_kind ENUM('build','update') NOT NULL DEFAULT 'build'`；新增 `ensure_ai_profile_session_columns`）
- Modify: `app/services/ai/profile.py`（`create_profile_session` :1003 旁新增 `create_update_session`；update 会话复用 `extract_profile_turn` :1524 但 prompt 换**澄清式**——围绕用户陈述追问边界直到可结构化为 entry patch；patch 候选结构 `{action: add|modify, category, content, replaces_field_key?}`）
- Modify: `app/api/routes/ai_profile.py`（`POST /profile-sessions/update-intent`：入参自然语言期望 + subject，返回会话与首轮追问；后续 turns/skip/confirm 复用现有端点）
- Modify: `database_setup_marriage.py` + `tests/`

**设计要点：**
- 活动槽位：`_reuse_active_session` :959 语义只适用于 build；update-intent 遇同 (user_id, subject) 活动会话 → `AI_INPUT_INVALID`（提示先完成/放弃），不静默关会话。
- update 会话**不重答全量题**：无题库推进，`current_question_id` 保持 NULL，turn 序列即澄清对话。
- patch 候选进入现有草稿确认流程前先落 turn 证据（`source_turn_ids` 可溯源）。
- Idempotency-Key 语义沿用会话创建端点既有模式。

**Steps:**
- [ ] RED（单测）：update-intent → 创建 `session_kind='update'` 会话 + 首轮澄清追问；已有活动会话时拒绝；澄清两轮后产出 add/modify patch 候选。
- [ ] GREEN：实现服务 + 路由 + 迁移。
- [ ] RED（集成）：真库建 update 会话 → turns 落库 → rollback 后重读成立。

**验收：** 对已发布画像发起 update-intent"希望对方是艺术家"→ 返回澄清追问（如"偏向音乐、绘画还是舞蹈？"）→ 答复 → 产出可确认 patch；全量回归不新增红。
**Commit:** `feat(profile): update-intent 对话式追加会话（澄清式追问+entry patch）`

---

### Task 6: WP-P4b 追加并入 + New 角标 + 不覆盖（F6）

**Goal:** patch 确认后并入草稿并发布为新 revision；读取端 New 条目置顶、旧条目保留。

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_feature_projection` 增 `first_seen_revision INT UNSIGNED DEFAULT NULL`，并入 `ensure_ai_projection_columns`）
- Modify: `app/services/ai/profile.py`（patch 确认并入 draft：add 直接新增 entry 行 / modify 新增行带 `replaces_field_key` 且**旧条目行不动**；`publish_profile_draft` :2537 写新 revision 时快照含 replaces 链；投影写入时对首次出现的 field_key 记 `first_seen_revision`）
- Modify: `app/api/routes/ai_profile.py` + schemas（字段读取端对 `首现 revision == 最新 revision` 的条目返回 `is_new: true`；排序 `is_new DESC, updated_at DESC`）
- Modify: `tests/`（重点回归 `test_ai_profile_publish.py` 全量）

**is_new 判定语义（锁定）：** 条目级——该 `field_key` 在 `ai_profile_revision_field` 历史中首次出现的 revision_no 等于最新已发布 revision_no，则为 new。被 modify 替换的旧条目保留原位（不置顶、不标新）；用户显式删除走 `delete_ai_profile_field`。

**Steps:**
- [ ] RED（单测）：add patch 确认 → 草稿出现新 entry、旧条目仍在；modify patch → 新行带 `replaces_field_key`、旧行未删；publish 后读取端 `is_new` 与置顶排序正确。
- [ ] GREEN：实现并入/发布/读取端。
- [ ] RED（集成）：真库两轮发布 → 第二轮新增条目 is_new=true、首轮条目 is_new=false 且仍在；rollback-then-assert。
- [ ] 全量回归（`test_ai_profile_publish.py` 必须全绿）。

**验收（方案 WP-P4 验收原文）：** 全流程 e2e——update-intent"希望对方是艺术家"→AI 追问→答复→确认→publish→读取端出现 `is_new=true` 的条目置顶，原条目仍在。
**Commit:** `feat(profile): 对话式追加并入+New角标置顶（不覆盖语义）`

---

### Task 7: WP-P5 语音落库与双模式互切（F7）

**Goal:** 语音抽取结果落进与文字模式相同的会话/草稿状态机；同一会话内 `input_mode` 无缝切换。

**Files:**
- Modify: `app/services/voice/conversation.py`（`_extract_in_memory` :390 / `extract_all` :464 的 `structured_extract` 结果改走与文字模式相同的 `extract_profile_turn` 落库路径，WS 仅做实时回显；`fallback tag` 语义与 `_fallback_tag_field` 对齐）
- Modify: `app/api/routes/voice_ws.py`（实时会话创建/恢复时绑定 `ai_profile_session`，`input_mode='voice'`；断线重连复用同一 session_id）
- Modify: `app/services/ai/profile.py`（新增 `update_session_input_mode`：属主校验 + 活动状态校验后更新 `input_mode`——**列已存在**（ai_schema.py:106，DEFAULT 'text'），零迁移）
- Modify: `app/api/routes/ai_profile.py`（`POST /profile-sessions/{id}/mode`）
- Modify: `tests/`（语音链路补持久化测试——现测试只测内存 fake）

**设计要点：**
- consent：语音落库与文字同 scope（`profile_text_extract`），WS 建会话前校验一致。
- 切换 = 同一 session 更新 `input_mode`，进度、草稿、`progress_percent` 自然延续（同一行状态机）。
- `extract_all` 批量结果落库时按 turn 归属拆分，保证 `source_turn_ids` 溯源不断。

**Steps:**
- [ ] RED（集成）：语音模式建会话（input_mode='voice'）→ 模拟 3 轮转写抽取 → draft_field 落库 → `POST /mode` 切 text → 进度与已确认字段延续 → 重连（同 session_id）不丢。
- [ ] GREEN：实现三处接线。
- [ ] 全量回归（voice 相关用户 WIP 红除外，须逐条确认未新增红）。

**验收（方案 WP-P5 验收原文）：** 语音答 3 题→切文字→进度与已确认字段延续；语音会话产生的字段出现在 draft；断线重连（恢复聊天）不丢。
**Commit:** `feat(voice): 语音抽取落库画像状态机+双模式互切`

---

### Task 8: WP-S3 "猜你喜欢"AI 化（F8）

**Goal:** "点击猜你喜欢，AI 会分析你的兴趣爱好，帮你输入搜索词"——异步任务生成 3~5 条自然语言搜索词，并入现有建议端点。

**Files:**
- Modify: `app/services/ai/search.py`（新增 `search_suggest` 任务 handler + `generate_search_suggestions` 服务；输入 = 用户双投影（interest/lifestyle 标签 + ideal_partner 结构化字段 + 条目 `entry_digest`，Task 4 前实施则该项为空）；LLM JSON mode 产 3~5 条搜索词，faithfulness 约束：只准基于已有资料归纳；24h 结果缓存）
- Modify: `app/workers/ai_worker.py`（`TASK_HANDLERS` :606-614 注册 `SEARCH_SUGGEST_TASK_TYPE` → handler，仿 `search_execute_handler` 模式）
- Modify: `app/api/routes/ai_search.py`（`POST /search-suggestions/generate`：202 + 任务 ID + Idempotency-Key；`GET /search-suggestions` :296 出参增 `source: 'ai'|'tags'`，合并 AI 结果与现有标签回显 :2434）
- Modify: `app/core/config.py`（如需频控/条数配置项）
- Modify: `tests/`

**设计要点：**
- **缓存即幂等回放**：24h 内已有 succeeded 的同用户 `search_suggest` 任务 → 直接回放其结果不新建任务（复用 `_find_write_task`/`_replay_or_conflict` 模式，幂等键 `search-suggest-{user_id}-{YYYYMMDD}`）。
- **频控**：每用户 24h 窗口生成次数上限（默认 5，复用 narrative regenerate 的 COUNT 窗口模式 profile.py `_NARRATIVE_REGENERATE_DAILY_LIMIT` 写法）。
- **降级**：用户无任何投影/条目 → 不建任务，`GET` 直接返回标签回显（source='tags'）；LLM 失败 → 任务 failed，`GET` 回退 tags（前端无感）。
- 出参校验：AI 词为非空字符串数组、≤5 条、去重；越界丢弃并记审计。

**Steps:**
- [ ] RED（单测）：fake LLM 产词 → 任务 succeeded 且 payload 含 3~5 词；24h 内二次 generate 幂等回放；超频控 `AI_INPUT_INVALID`；空投影直接 tags 降级。
- [ ] GREEN：实现 handler/服务/路由/注册。
- [ ] RED（集成）：真库 generate → 轮询 succeeded → `GET /search-suggestions` 返回 source='ai' 结果；rollback-then-assert 审计行。
- [ ] 全量回归。

**验收（方案 WP-S3 验收原文）：** 有丰富投影的用户拿到 3 条以上与自身兴趣强相关的搜索词；空投影用户降级返回标签回显；每用户生成有频控。
**Commit:** `feat(search): AI 猜你喜欢（search_suggest 任务+24h缓存+频控+降级）`

---

### Task 9: WP-S2 中途模糊候选简版（F10）

**Goal:** "进度 30% 后先看到模糊符合的用户"——可见性过滤完成即物化初筛集并可读，完成后切完整结果。

**Files:**
- Modify: `app/db/ai_schema.py`（`ai_search_snapshot` :300 增 `partial_visible ENUM('none','partial','full') NOT NULL DEFAULT 'none'`；新增 `ensure_ai_search_snapshot_columns`；`ai_search_result` :323 **不加列**——partial 集用现有 `generation=0` 行表达，`is_fuzzy` 由读取端按 `generation==0` 派生）
- Modify: `app/services/ai/search.py`（execute worker 在可见性过滤完成（进度 30% 的 filtering 阶段收尾）物化初筛集：**仅 hard 确定性条件全部命中** + 上限 50 条 + `generation=0`；快照置 `partial_visible='partial'`；最终 generation 物化完成后置 `'full'`；无 hard 条件的查询不物化 partial，保持 none 直至 full）
- Modify: `app/api/routes/ai_search.py`（结果端点：`partial_visible='partial'` 时即可读 generation=0 行，条目带 `is_fuzzy: true`；`'full'` 后返回 active generation 且 `is_fuzzy=false`）
- Modify: `database_setup_marriage.py` + `tests/`

**设计要点：**
- 复用阶段0+1 的 `_set_search_task_stage` 进度上报：filtering=30 处挂钩 partial 物化，物化本身失败**不得**中断主流程（partial 是增强，失败降级为无 partial）。
- partial 行同样写入 `matched_condition_count/reason_codes`，但仅统计 hard 条件；脱敏出参与 full 完全一致（Global 纪律）。
- 快照过期/失效语义不变：partial 行随快照一起失效。

**Steps:**
- [ ] RED（集成）：confirm 后模拟任务推进至 30% → 结果端点返回 partial 集且 `is_fuzzy=true`、条目数 ≤50、全部满足 hard 条件；任务完成后同端点返回完整集 `is_fuzzy=false`；仅 soft 命中用户不在 partial 集中；partial 物化注入失败 → 主任务仍成功。
- [ ] GREEN：实现迁移/worker/读取端。
- [ ] 全量回归（搜索既有快照语义不变）。

**验收（方案 WP-S2 验收原文）：** confirm 后立刻轮询结果端点：进度≥30% 时返回 partial 集合（is_fuzzy=true）；任务完成后同端点返回完整集合且 is_fuzzy=false；partial 集不含仅 soft 条件命中的用户。
**Commit:** `feat(search): 中途模糊候选（partial 代次物化+is_fuzzy 读取端）`

---

## 任务依赖与执行顺序

```
T1 PRODUCT.md ──→ 全部
T2 表结构 ──→ T3 entry 链路 ──→ T4 叙事/投影/读取端 ──→ T5 update-intent ──→ T6 追加/New角标
                    └──────────────→ T7 语音互切（T3 定稿落库格式后即可并行）
T8 猜你喜欢：独立（T4 后开工输入可含 entry_digest；更早开工则输入不含条目摘要，其余不变）
T9 模糊候选：完全独立，随时可并行
```

- 串行关键路径：T2→T3→T4→T5→T6（画像主线，对应方案"P1→P4"依赖）。
- 可并行线：T7（T3 完成后）、T8、T9。
- 规模估算（方案口径）：P1 5~8 人日（T2~T4）、P4 5~8 人日（T5~T6）、P5 3~4 人日（T7）、S3 2~3 人日（T8）、S2 3~4 人日（T9）≈ 18~27 人日。

---

## 阶段完成定义（DoD）

1. `python -m pytest tests/ -q`：新增用例全绿、**不新增红**（基线：742 过 / 5 红为用户并行 WIP——voice×3、worker lease、ai lease；若届时用户已修则应全绿）。
2. 手工冒烟（本地起后端）：
   - 条目式画像：会话答出条目→确认→发布→字段接口按分类返回、叙事提及条目；
   - 对话式更新：update-intent"希望对方是艺术家"→追问→确认→发布→`is_new=true` 条目置顶、原条目仍在；
   - 双模式：语音答 3 题→`POST /mode` 切文字→进度与已确认字段延续；
   - 猜你喜欢：`POST /search-suggestions/generate`→轮询→`GET` 返回 `source='ai'` 搜索词；空投影用户返回 `source='tags'`；
   - 模糊候选：搜索 confirm→进度约 30% 时结果端点已可读且 `is_fuzzy=true`→完成后 `is_fuzzy=false` 完整集。
3. `PRODUCT.md` 阶段2章节入档；本文件各任务 checkbox 勾选完毕。
4. 部署提示：生产流量前跑 `database_setup_marriage.py` bootstrap（本阶段 3 个 ensure helper 全部幂等，可重复执行）；所有新列带默认值，旧数据零影响；回滚 = 回退代码即可，无独立数据回滚需求。
