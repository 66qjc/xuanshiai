# 良配 AI 体验完善 — 阶段3 实施计划（推荐与匹配主线）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地良配文档"根据这些描述给用户推荐'我会喜欢/会喜欢我/相似的人'"与"大模型算法比对双方画像，分析互相适合的概率"两句话——新增 WP-P6 三类推荐（快照预计算）与 WP-C1 匹配度引擎"规则粗排 + LLM 精算"混合升级，含 WP-C3 排序消费第一步与 WP-C4 品牌标注（差距 F12/F11）。

**Architecture:** P6 走**快照预计算**（决策 D4）：新表 `ai_recommendation_snapshot` 按 (viewer, view_kind, generation) 存 top-20，打分不实时调 LLM——`i_like`/`likes_me` 复用 compatibility 引擎的**单向**打分函数 `directional_score`，`similar` 用分类加权 Jaccard + 数值近邻；触发时机 = 画像 publish 入队 / 推荐页缓存失效（GET miss 入队）/ 每日批量脚本，全部复用 `ai_task` 队列。C1 走**混合式**（决策 D1）：现有规则引擎保留为粗排与兜底，用户主动查看匹配度页且无可用快照时触发 `compatibility_llm` 任务，输入双方投影（含阶段2 `entry_digest`），输出双向概率 + 3 条中文理由，写回快照（`engine='llm-v1'`）；LLM 失败自动降级写规则结果并标注，读取端永远有可用快照。C1 完成后 `i_like/likes_me` 平滑切换为消费 llm 快照双向分（`engine` 字段标记来源）。

**Tech Stack:** FastAPI + SQLAlchemy(async) 裸 SQL（无 ORM）、MySQL、pytest（单测 fake 注入 + real_db 集成，compose 专用库 127.0.0.1:3307）、pydantic-settings。

**规格来源:** `D:\Users\ASUS\Desktop\宣誓爱\良配思维导图总结\良配AI体验完善方案.md`（§四 WP-P6、§六 WP-C1/C3/C4、§八 阶段3 路线图、§9.2 决策 D1/D4/D5）。C3 第一步（推荐按契合度排序）含在 P6 物化排序内；C4（brand_label）含在 C1 内。C2 外显灰度已在阶段0+1 完成，本阶段不含；C3 第二步（discovery 流接入）留待灰度观察，不在本计划。

**代码基线（2026-08-30，阶段2 Task 6 完成后）：**
- 分支 `feat/liangpei-ai-phase3`（基于 `feat/liangpei-ai-phase2` HEAD 162a037），在独立 worktree `D:\Users\ASUS\Desktop\xuanshiai-backend-phase3` 实施——**主工作树内有并行会话的阶段2 Task 7 未提交 WIP（voice 互切），绝不可在主树提交阶段3改动**。
- 阶段2 已提供：`ai_feature_projection.entry_digest`（条目摘要，Task 4 已提交）、`update-intent` 会话、`AI_COMPATIBILITY_DISPLAY_MODE` 外显灰度、`ai_task.progress_percent`。
- compatibility 引擎可复用件（`app/services/ai/compatibility.py`）：`directional_score` :457（单向打分）、`compute_compatibility` :476、`COVERAGE_THRESHOLD` :79（0.50）、`_DIMENSION_TO_REASON` :500、`load_compatibility_features` :817、`_load_current_projection_rows` :768（含投影新鲜度+consent 过滤）、`_load_revision_vector` :696、`write_shadow_snapshot` :905、`read_compatibility_snapshot` :1132、`request_compatibility_recompute` :1240（202+任务模式）、`compatibility_execute_handler` :1345（handler 门禁复刻模式）、`register_compatibility_handlers` :1417（幂等注册模式）。

---

## Global Constraints

- **服务层不 commit**：所有 `app/services/ai/*.py` 业务函数保持"不 commit，由调用方控制事务"契约；worker handler 的业务写入由 `_process` 的 finalize 路径统一提交（savepoint + 恰好一次外层提交）；路由成功路径显式 `await db.commit()`。
- **生产 fail-closed 门禁不变**：provider=mock 或审批不全时 503（`app/services/ai/flags.py`）；新增 `AiFeature.RECOMMEND` 走同一门禁；C1 复用 `AiFeature.COMPATIBILITY_SHADOW` 门禁不新增开关。
- **旧库迁移只用幂等补列**（`SHOW COLUMNS` → `ALTER TABLE ADD COLUMN`）；新表直接进 `AI_TABLES`（`CREATE TABLE IF NOT EXISTS`，bootstrap 自动建表）；ensure helper 必须在 `database_setup_marriage.py`（:2673 调用区）接线。
- **coverage 门槛沿用**：`COVERAGE_THRESHOLD=0.50`；无投影 / coverage 不足的用户**不出现在任何推荐列表**，llm 精算对 coverage 不足的 pair 不调用（成本守门）。
- **授权快照纪律沿用**：投影读取必须过 `profile_text_extract` scope 校验（复用 `_load_current_projection_rows` 的过滤语义）；匹配度 llm 触发必须双方 `compatibility_shadow` 授权有效（复用 recompute 门禁）；推荐/快照不携带对方投影原文出参，仅分数、理由码、`target_user_id`。
- **LLM faithfulness**：compatibility_llm prompt 一律只准基于所给双方投影资料判断，禁止编造；输出做 pydantic schema 校验，漂移转为可重试 ProviderError（仿 narrative 模式）。
- **D4/D5 锁定**：推荐打分不实时调 LLM（快照预计算）；匹配度排序本期只进三类推荐，**主 discovery 名片流不动**。
- **分数量纲统一 0..100**：`directional_score`/快照 `compatibility_index`/推荐 `score` 均为 0..100（`coverage` 为 0..1）；llm 输出的 0-100 概率原值写入。
- 每个任务完成必须全量跑 `python -m pytest tests/ -q` 不新增红后才能 commit（基线红见下）；集成测试 `python -m pytest tests/integration/ai -q`（`AI_TEST_DATABASE_URL`，默认 127.0.0.1:3307，容器 `xuanshiai-ai-test-mysql-1`）。
- 注释风格：中文，说明"为什么/约束"；commit message 用 `feat:`/`fix:`/`docs:` 前缀。
- **工作区红线**：主工作树（宣誓爱/xuanshiai-backend）内并行会话的未提交 WIP 不得进入任何提交；阶段3 全部提交只在 worktree 分支 `feat/liangpei-ai-phase3` 上进行，每次 commit 前 `git status` 核对。
- 决策点沿用已锁定结论：D1 混合式、D4 快照预计算、D5 先只进三类推荐。

**已知基线红：** 开工时以 worktree 首次全量跑出的红集合为准（阶段2 基线 5 红均为并行 WIP：voice×3、worker lease、ai lease）；DoD 按"红集合不扩大"判定。

**工作目录：** 除 Task 1（PRODUCT.md 在工作区根）外均在 `D:\Users\ASUS\Desktop\xuanshiai-backend-phase3`。

---

### Task 1: PRODUCT.md 产品范围更新（治理前置）

**Files:**
- Modify: `D:\Users\ASUS\Desktop\宣誓爱\PRODUCT.md`（工作区根；已有"AI 体验增强"系列章节）

**Steps:**
- [x] 在 AI 体验增强章节后追加"### AI 体验增强·阶段3（三类推荐与匹配度精算，2026-08）"，含 4 小节，每节写清产品定义与边界：
  1. **三类推荐**："我会喜欢/会喜欢我/相似的人"三个推荐位；打分基于双方已发布画像投影预计算（不实时调用大模型）；用户无有效画像投影或维度覆盖不足不出现在列表；推荐卡片只下发分数、理由码与对方 ID，基本资料由前端复用既有候选名片接口渲染，不通过推荐接口暴露对方画像原文。
  2. **匹配度"规则粗排 + AI 精算"**：用户主动查看某人匹配度页且无可用快照（不存在/过期，TTL 7 天）时后台触发 AI 精算任务，输出双向概率（0-100）与 3 条中文可解释理由；规则引擎免费即时分保留为缓存与兜底；AI 精算失败自动降级规则结果并标注，匹配度页永远有可用结果。
  3. **品牌标注**：匹配度与推荐结果随出参下发 `brand_label`（默认"来自良配Ai算法"），对齐"AI 算法结果"的明示语义。
  4. **排序边界**：契合度分数本期只作为三类推荐的排序主键；主 discovery 名片流排序不变（灰度验证点击率后再评估，决策 D5）。
- [x] 自查：不与既有章节冲突（尤其外显灰度、发布阈值行保持不动）。

**验收：** 纯文档，无代码；边界与 Global Constraints 一致。
**Commit:** 无（PRODUCT.md 在工作区根，不入两仓库 git）。

---

### Task 2: WP-P6a 推荐快照表（F12 地基）

**Goal:** 新表 `ai_recommendation_snapshot` 物理落地；bootstrap 自动建表；旧库零影响。

**Files:**
- Modify: `app/db/ai_schema.py`（`AI_TABLES` dict 增 `ai_recommendation_snapshot`，仿 :389 `ai_compatibility_snapshot` 风格）
- Test: `tests/integration/ai/test_ai_recommend_schema_real_db.py`（新建，仿 `test_ai_entry_schema_real_db.py`）

**表结构（锁定）：**
```sql
CREATE TABLE IF NOT EXISTS `ai_recommendation_snapshot` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT,
    `snapshot_id` varchar(64) NOT NULL COMMENT '本次物化批次 id，同批三视图共用',
    `viewer_user_id` bigint unsigned NOT NULL,
    `view_kind` ENUM('i_like','likes_me','similar') NOT NULL,
    `target_user_id` bigint unsigned NOT NULL,
    `score` decimal(5,2) DEFAULT NULL COMMENT '0..100 视图主分',
    `coverage` decimal(5,4) DEFAULT NULL COMMENT '0..1 维度覆盖度',
    `direction_json` json DEFAULT NULL COMMENT 'i_like/likes_me：单向分与理由明细',
    `score_detail_json` json DEFAULT NULL COMMENT 'similar：分类权重明细',
    `reason_codes` json DEFAULT NULL,
    `rank_no` int unsigned NOT NULL COMMENT '1 起，score 降序（避开 MySQL 8 保留字 rank）',
    `generation` int unsigned NOT NULL DEFAULT 1,
    `engine` varchar(32) NOT NULL DEFAULT 'rule-v1' COMMENT 'rule-v1/llm-v1 打分来源',
    `algorithm_version` varchar(32) NOT NULL DEFAULT 'recommend-rule-v1',
    `source_hash` char(64) NOT NULL COMMENT 'viewer 投影 source_hash，失效锚',
    `status` varchar(24) NOT NULL DEFAULT 'ready' COMMENT 'ready/superseded',
    `calculated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expires_at` datetime DEFAULT NULL,
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_ai_recommend_snapshot` (`viewer_user_id`, `view_kind`, `target_user_id`, `generation`),
    KEY `idx_ai_recommend_read` (`viewer_user_id`, `view_kind`, `status`, `rank_no`),
    KEY `idx_ai_recommend_expires` (`expires_at`, `status`),
    CONSTRAINT `chk_ai_recommend_viewer_not_target` CHECK (`viewer_user_id` <> `target_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 三类推荐快照（D4 预计算）'
```

**Steps:**
- [x] RED：集成测试断言 `information_schema.COLUMNS` 中该表含上述关键列（view_kind ENUM/score/rank_no/generation/engine/source_hash），且重复执行建表语句不报错（幂等）。
- [x] GREEN：`AI_TABLES` 增表（`database_setup_marriage.py` 经 `tables.update(AI_TABLES)` :2623 自动获得，无需额外接线——核对即可）。
- [x] 全量回归（新表对既有用例零影响）。

**验收：** 表存在且幂等；全量测试不新增红。
**Commit:** `feat(schema): 三类推荐快照表 ai_recommendation_snapshot（D4 预计算载体）`

---

### Task 3: WP-P6b 推荐打分核心（纯函数层）

**Goal:** `i_like` / `likes_me` / `similar` 三种打分的纯函数实现与单测——不碰 DB、不调 LLM。

**Files:**
- Create: `app/services/ai/recommend.py`（纯函数区）
- Test: `tests/test_ai_recommend.py`（新建单测）

**接口（后续任务依赖，锁定）：**
```python
RECOMMEND_TASK_TYPE = "recommend_rebuild"
RECOMMEND_ALGORITHM_VERSION = "recommend-rule-v1"
RECOMMEND_ENGINE_RULE = "rule-v1"
RECOMMEND_ENGINE_LLM = "llm-v1"
VIEW_KINDS = ("i_like", "likes_me", "similar")

@dataclass(frozen=True)
class RecommendationScore:
    score: float | None      # 0..100，None=不可算（coverage 不足/无维度）
    coverage: float          # 0..1（维度权重覆盖占比）
    reason_codes: tuple[str, ...]
    score_detail: dict | None = None   # similar 的权重明细 / 单向分的维度明细

def score_i_like(viewer_preference: dict, candidate_profile: dict) -> RecommendationScore
def score_likes_me(candidate_preference: dict, viewer_profile: dict) -> RecommendationScore
def similarity_score(profile_a: dict, profile_b: dict) -> RecommendationScore
def positive_dimension_codes(preference: dict, profile: dict) -> tuple[str, ...]
```

**设计要点：**
- `score_i_like` 内部组装 `FeatureSet(profile={}, preference=viewer_preference)` 与 `FeatureSet(profile=candidate_profile, preference={})`，调用 compatibility 的 `directional_score`（:457，导入复用，**不改其行为**）；`likes_me` 反向组装；二者共享 `_directional_score_card(source_pref, target_profile)` 私有包装（score+coverage+`positive_dimension_codes` 理由码）。coverage < `COVERAGE_THRESHOLD`（0.50，从 compatibility 导入）时 score 置 None——消费端据此把候选排除出列表（方案验收："无投影用户不出现在任何列表"）。
- `positive_dimension_codes`：对每个 `COMPATIBILITY_RULES.dimensions` 中双方已知且 `dimension.score>0` 的维度，经 `_DIMENSION_TO_REASON`（:500）映射为理由码（复用既有展示语义）；缺失维度记 `DIMENSION_UNKNOWN`。
- `similar` 权重（和恒为 1.0，冻结在常量 `_SIMILAR_WEIGHTS`）：`interest_tags 0.30`（集合 Jaccard）、`relationship_goal 0.15`、`education_level 0.10`、`marriage_status 0.10`、`city_code 0.10`、`income_band 0.05`（分类等值）、`age 0.10`（|Δ|≤3 岁=1.0，线性降到 15 岁=0）、`height_cm 0.10`（|Δ|≤5cm=1.0，线性降到 30cm=0）。coverage=双方已知维度的权重占比，<0.5 置 None；理由码 `SIM_{KEY大写}`（贡献>0 的维度）。`score_detail` 带各维度 `{weight, value}` 便于调试与后续调权。
- 字段容错：投影 `fields_json` 的值可能是标量/列表/None，全部按 compatibility 的 `_as_number`/`_as_str` 语义取值（直接复用）。

**Steps:**
- [x] RED：`score_i_like`（已知偏好×满足画像 → 分数与理由码；覆盖不足 → score None）；`score_likes_me` 反向对称断言；`similarity_score`（同校同兴趣 → 高分排序在前；无交集 → 低分；两侧全空 → None）；`positive_dimension_codes` 只产正向码。
- [x] GREEN：实现纯函数区。
- [x] 全量回归。

**验收：** 三种打分单测全绿；不触碰 DB/LLM/任务系统。
**Commit:** `feat(recommend): 三类推荐打分核心（单向复用规则引擎+相似度加权Jaccard）`

---

### Task 4: WP-P6c 候选池、物化与 worker handler

**Goal:** 从投影加载候选池 → 三视图打分 → top-20 物化为新 generation；注册 `recommend_rebuild` worker 任务。

**Files:**
- Modify: `app/services/ai/recommend.py`（DB 区 + handler）
- Modify: `app/services/ai/compatibility.py`（`_load_active_consent` 增公开别名 `load_active_consent`，供跨模块授权读取；行为零变化）
- Modify: `app/workers/ai_worker.py`（`register_business_handlers` :576 增 `TASK_HANDLERS.setdefault(RECOMMEND_TASK_TYPE, recommend_rebuild_handler)`，或在 recommend.py 内仿 `register_compatibility_handlers` :1417 自注册——采用后者，与 compatibility 一致）
- Test: `tests/test_ai_recommend.py`（续）+ `tests/integration/ai/test_ai_recommend_real_db.py`（新建）

**接口（锁定）：**
```python
async def load_candidate_pool(db, viewer_id: int, limit: int) -> list[dict]
    # 行结构 {user_id, fields: dict, entry_digest: str|None, source_hash: str}
async def load_recommendation_inputs(db, viewer_id: int) -> tuple[dict, dict] | None
    # (viewer_personal_fields, viewer_preference_fields)；无有效投影返回 None
async def materialize_recommendations(db, viewer_id: int, trigger: str) -> str
    # 返回 snapshot_id；不 commit
async def recommend_rebuild_handler(db, task, worker_id) -> tuple[str, RevisionVector] | None
```

**设计要点：**
- **候选池**：`SELECT subject_user_id, fields_json, entry_digest, source_hash FROM ai_feature_projection WHERE projection_kind='personal_compatibility' AND status='active' AND subject_user_id<>:viewer AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP()) ORDER BY updated_at DESC LIMIT :limit`（limit=`settings.ai_recommendation_pool_limit`，默认 200）；python 侧过滤 `consent_snapshot_json.scope=='profile_text_extract'`（对齐 `_load_current_projection_rows` :768 的授权过滤语义）；同 user 多投影取 id 最大者。**候选投影不做逐用户 revision 向量复核**（200 次额外查询不值；推荐是增强型消费，投影过期由 publish/每日触发自然刷新）。
- **可见性**：逐候选 `candidate_visibility_service.decide(db, viewer_id, candidate_id, VisibilityScene.PROFILE)`，不可见跳过（worker 内批量执行，可接受 N 次查询）。
- **物化**：三视图分别打分 → 过滤 score None → 降序取 top `settings.ai_recommendation_top_n`（默认 20）→ `rank_no` 1 起连续（**WP-C3 第一步：契合度高者优先**）→ generation = `COALESCE(MAX(generation),0)+1`（按 viewer+view_kind）→ INSERT 全部行（同批共用 snapshot_id `rc_{uuid}`）→ UPDATE 旧 generation 置 `status='superseded'`；`expires_at = now + settings.ai_recommendation_ttl_minutes`（默认 1440）。全空（无合格候选）也写批次语义：不插行，直接返回 snapshot_id（读取端据"无 ready 行"走再生成提示）。
- **handler 门禁**：payload `{viewer_user_id, trigger}`；viewer 需有 active `profile_text_extract` 授权（`load_active_consent`）且 `load_recommendation_inputs` 非 None，否则 `fail_task(AI_INPUT_INVALID, retryable=False)`；返回 `(f"recommend-snapshot:{snapshot_id}", owner_rev)`（owner_rev 用 `compatibility._load_revision_vector` :696）。仿 `compatibility_execute_handler` :1345 结构。
- **config 新增**（`app/core/config.py` :211 附近）：`ai_recommendation_ttl_minutes: int = Field(default=1440, gt=0)`、`ai_recommendation_pool_limit: int = Field(default=200, gt=0)`、`ai_recommendation_top_n: int = Field(default=20, gt=0)`。

**Steps:**
- [x] RED（单测）：fake db 会话注入投影行 → 候选池过滤（scope/过期/去重/排除自己）成立；物化对 mock 候选产出三视图 generation=1 行序正确；二次物化 generation=2 且旧行 superseded。
- [x] GREEN：实现 DB 区 + handler + 自注册 + config。
- [x] RED（集成）：真库两个造数用户互有投影（沿用 `test_ai_compatibility_real_db.py` 的造数模式）→ 直接调 `materialize_recommendations` → rollback-then-assert：i_like/likes_me 互含对方且 rank、score 落库正确；similar 排序合理；无投影用户物化不产生 ready 行。
- [x] 全量回归。

**验收：** 物化幂等（generation 递增、旧代失效）；coverage 不足者不入表；handler 注册后 `--once` 可消费任务。
**Commit:** `feat(recommend): 候选池+三视图物化+recommend_rebuild worker任务`

---

### Task 5: WP-P6d 触发接线（publish / GET miss / 每日批量）

**Goal:** 三条触发路径全部接到 `recommend_rebuild` 任务；publish 主流程零破坏。

**Files:**
- Modify: `app/services/ai/profile.py`（`publish_profile_draft` :3230——narrative 入队（:3349 附近）之后增 recommend 入队：`enqueue_task(task_type=RECOMMEND_TASK_TYPE, idempotency_key=idempotency_key+"-recommend", request_hash=request_hash+":recommend", revisions=published_vector, consent=draft.consent_snapshot)`；**不改 TaskSubmission 出参**（fire-and-forget，状态经推荐读取端观察））
- Create: `scripts/recommend_daily_batch.py`（每日批量：遍历存在 active personal_compatibility 投影的 distinct user，逐个 `enqueue_task(key=f"recommend-daily-{user}-{UTC日期}")`；docstring 写明 crontab 建议 `0 3 * * *`）
- Test: `tests/integration/ai/test_ai_recommend_real_db.py`（续）

**设计要点：**
- publish 幂等回放路径不受影响：`enqueue_task` 同 key 回放既有任务（`tasks.py` :428 语义），replay 分支无需感知 recommend 任务。
- GET-miss 触发在 Task 6 的路由里接线（本任务只备好 key 约定：`recommend-view-{viewer}-{YYYYMMDD}`，request_hash 含同日约束——同用户同日 GET 触发至多一个重建任务）。
- 每日批量脚本独立于 FastAPI：直接用 `app.db.session` 的 session 工厂 + `enqueue_task`，末尾打印入队计数；不入队无投影用户。

**Steps:**
- [x] RED（集成）：publish 含 recommend 任务入队（rollback-then-assert ai_task 行）；同 Idempotency-Key 重放 publish 不产生第二个 recommend 任务；daily 脚本 dry 逻辑对造数用户入队且幂等（同日重跑回放）。
- [x] GREEN：实现两处接线。
- [x] 全量回归（`test_ai_profile_publish.py` 必须全绿）。

**验收：** 画像发布后自动产生推荐重建任务；每日批量幂等；publish 契约（出参/状态机）不变。
**Commit:** `feat(recommend): publish/每日批量触发推荐重建（publish契约不变）`

---

### Task 6: WP-P6e 读取端点 GET /ai/recommendations（含 C3 排序验收）

**Goal:** 推荐页读取快照；miss/过期时入队重建并返回 `regenerating`；新功能门禁接入。

**Files:**
- Create: `app/api/routes/ai_recommend.py` + `app/schemas/ai_recommend.py`
- Modify: `app/api/router.py`（:106 区 `api_router.include_router(ai_recommend.router, prefix="/ai", tags=["AI"])`）
- Modify: `app/services/ai/flags.py`（`AiFeature.RECOMMEND = "recommend"` + `is_ai_feature_enabled` 分支 `settings.ai_recommend_enabled`）
- Modify: `app/core/config.py`（`ai_recommend_enabled: bool = False`；生产 validator :339 的 any_ai_enabled 元组加入该开关）
- Modify: `app/services/ai/recommend.py`（`read_recommendations(db, viewer_id, view_kind, limit) -> list[dict]`：`WHERE viewer+view_kind+status='ready' AND expires_at>now` 按 `rank_no` 升序取 limit 行）
- Test: `tests/test_ai_recommend.py` + `tests/integration/ai/test_ai_recommend_real_db.py`（续）

**出参（锁定）：**
```python
class RecommendationCard(BaseModel):
    target_user_id: int
    score: float | None
    coverage: float | None
    rank_no: int
    engine: str                 # rule-v1 / llm-v1
    reason_codes: list[str]
    reason_texts: list[str] = []   # llm 引擎时来自 direction_json 的中文理由
class RecommendationPage(BaseModel):
    view: Literal["i_like", "likes_me", "similar"]
    items: list[RecommendationCard]
    regenerating: bool = False
```
（卡片"基本资料+认证标+标签"由前端用既有候选名片/用户资料接口按 `target_user_id` 拼装——推荐接口不下发对方画像原文，边界见 Task 1。）

**Steps:**
- [x] RED（单测）：read_recommendations 过滤 superseded/过期、按 rank_no 排序、limit 生效。
- [x] RED（集成）：造数物化后 `GET /ai/recommendations?view=i_like` 返回 200 且 items 按 rank 排列（**C3 验收：按契合度分数降序**）；无快照用户 GET 返回 `regenerating=true` 且 ai_task 出现 `recommend-view-{user}-{日期}` 任务、同日重复 GET 不再入队；flags 关闭时 503 `AI_FEATURE_DISABLED`。
- [x] GREEN：实现路由/schema/flags/config/router。
- [x] 全量回归。

**验收（方案 WP-P6+WP-C3 第一步）：** 三个 view 均可读、按分排序；miss 触发重建且同日幂等；门禁 503 生效。
**Commit:** `feat(recommend): GET /ai/recommendations 读取端点+门禁+miss再生成`

---

### Task 7: WP-C1a 快照表 engine/brand_label 列与写读路径

**Goal:** `ai_compatibility_snapshot` 获得 `engine` 与 `brand_label`；写入/读取路径向后兼容。

**Files:**
- Modify: `app/db/ai_schema.py`（:389 CREATE TABLE 增列 + 新增 `ensure_ai_compatibility_engine_columns`）
- Modify: `database_setup_marriage.py`（:2679 调用区接线）
- Modify: `app/services/ai/compatibility.py`（常量 `COMPATIBILITY_LLM_TASK_TYPE="compatibility_llm"`、`ENGINE_RULE="rule-v1"`、`ENGINE_LLM="llm-v1"`、`SCORE_SEMANTICS_LLM="llm_pairwise_probability"`、`BRAND_LABEL="来自良配Ai算法"`、`REASON_LLM_FALLBACK="LLM_FALLBACK_RULE"`（并入 `_NON_DISPLAYABLE_REASONS` :110）；`write_shadow_snapshot` :905 增参 `engine="rule-v1", brand_label=None, ttl_minutes=None`（默认行为完全不变：engine 固定 rule-v1、ttl 用 `ai_compatibility_snapshot_ttl_minutes` :211）；`_SNAPSHOT_INSERT_COLUMNS`/`_SNAPSHOT_READ_COLUMNS` 增列；`_snapshot_to_read` 透传）
- Modify: `app/schemas/ai_compatibility.py`（`CompatibilitySnapshotRead` 增 `engine: str = "rule-v1"`、`brand_label: str | None = None`）
- Test: `tests/integration/ai/test_ai_compatibility_real_db.py`（续）+ `tests/test_ai_compatibility.py`（续）

**Steps:**
- [x] RED（集成）：information_schema 断言两新列；旧库（先建旧结构再跑 ensure）补列成功且**连跑两遍不报错**；llm 参数写快照后 rollback-then-assert engine/brand_label/ttl 落库；rule 默认路径回归（engine='rule-v1'，brand_label NULL，行为与改前逐字段一致）。
- [x] GREEN：实现迁移 + 写读扩展。
- [x] 全量回归。

**验收：** 列幂等存在；规则快照字节级语义不变；llm 形参路径就绪（Task 9 使用）。
**Commit:** `feat(compatibility): 快照engine/brand_label列+写读路径扩展（rule默认零变化）`

---

### Task 8: WP-C1b compare_compatibility：schema/prompt/provider/gateway

**Goal:** LLM 双向比对的最小调用链：请求/结果 schema、faithfulness prompt、Mock 与 OpenAI 兼容实现、网关方法。

**Files:**
- Modify: `app/schemas/ai_compatibility.py`（增 3 个模型）
- Create: `app/services/ai/prompts/compatibility_compare.py`
- Modify: `app/services/ai/providers.py`（`MockAIProvider` :240 增 `compare_compatibility`（确定性 fixture + failure 注入 scope `"compare_compatibility"`）；`_OpenAICompatProvider` :626 增 `compare_compatibility`，仿 `generate_narrative` :1010：`build_compatibility_compare_prompt` → `_chat_json` :721 → 归一化（score clamp 成 0-100 整数、reasons 去空/去重/≤50 字截断）→ `CompatibilityCompareResult.model_validate`；条数≠3 等 schema 漂移转 RETRYABLE `ProviderError`）
- Modify: `app/services/ai/gateway.py`（增 `compare_compatibility(context, request) -> InvokeOutcome[CompatibilityCompareResult]`，仿 `generate_narrative` :384，走 `invoke(context, "compare_compatibility", ..., response_type=...)`）
- Test: `tests/test_deepseek_provider.py` 风格新增用例（或 `tests/test_ai_compatibility.py` 续）

**Schema（锁定）：**
```python
class CompatibilityCompareDirection(BaseModel):
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=3, max_length=3)

class CompatibilityCompareRequest(BaseModel):
    viewer_personal: str; viewer_personal_digest: str | None = None
    viewer_ideal: str;     viewer_ideal_digest: str | None = None
    target_personal: str;  target_personal_digest: str | None = None
    target_ideal: str;     target_ideal_digest: str | None = None

class CompatibilityCompareResult(BaseModel):
    viewer_to_target: CompatibilityCompareDirection
    target_to_viewer: CompatibilityCompareDirection
```

**Prompt 要点（`build_compatibility_compare_prompt(request) -> str`）：** 角色=婚恋匹配分析师；输入=双方"个人画像/理想型画像"的结构化字段摘要 + 条目摘要（digest 为空标注"无"）；硬约束——只准基于所给资料判断，资料不足给低分并在理由中说明、禁止编造新信息；每方向恰好 3 条中文理由、每条 ≤50 字、口语化可解释；score 为 0-100 整数；输出仅 JSON：`{"viewer_to_target":{"score":72,"reasons":["…","…","…"]},"target_to_viewer":{…}}`。

**Steps:**
- [x] RED（单测）：Mock provider 返回确定性 fixture（72/68 各 3 条理由）且 failure 注入 `compare_compatibility:timeout` 生效；OpenAI 兼容实现 monkeypatch `_chat_json`——合法 JSON 通过、score 122 clamp 为 100、reasons 2 条→RETRYABLE ProviderError、4 条→RETRYABLE；gateway 层 schema violation 转 `AI_INPUT_INVALID` 不可重试。
- [x] GREEN：实现 schema/prompt/双 provider/gateway。
- [x] 全量回归。

**验收：** 调用链单测全绿；不触碰 DB（纯调用层）。
**Commit:** `feat(compatibility): compare_compatibility LLM调用链（schema+prompt+双provider+gateway）`

---

### Task 9: WP-C1c compatibility_llm 任务：handler + GET 触发 + 降级

**Goal:** "用户主动查看匹配度页且无可用快照 → llm 任务（约数秒）→ 快照更新为 engine='llm-v1' 带双向分+理由+brand_label；失败降级规则结果"。

**Files:**
- Modify: `app/services/ai/compatibility.py`：
  - `load_compatibility_prompt_inputs(db, viewer_id, target_id)`（基于 `_load_current_projection_rows` :768 取 4 行的 `fields_json` + `entry_digest`，拼 `CompatibilityCompareRequest`；任何一方缺投影返回 None）
  - `compatibility_llm_execute_handler(db, task, worker_id)`（门禁复刻 rule handler :1345：可见性/版本向量/双 consent → 规则粗排 `compute_compatibility` :476，**blocked/coverage 不足直接写规则快照收尾，不调 LLM**（成本守门）→ `load_compatibility_prompt_inputs` 为 None 同前 → `AIGateway.compare_compatibility` → outcome.error：`write_shadow_snapshot(rule 结果 + reason_codes 追加 REASON_LLM_FALLBACK)`；成功：写 llm 快照——`compatibility_index=调和平均(两分)`、`direction_json={"viewer_to_target":{"score":v,"reasons":[…3 条中文]},"target_to_viewer":…}（0-100 原值）`、`engine='llm-v1'`、`brand_label=BRAND_LABEL`、`score_semantics='llm_pairwise_probability'`、`ttl_minutes=settings.ai_compatibility_llm_ttl_minutes`；返回 `(f"compatibility-snapshot:{id}", owner_rev)`）
  - `register_compatibility_handlers` :1417 增 llm handler 注册
  - `request_compatibility_llm_refresh(db, viewer_id, target_id) -> CompatibilityRecomputeAccepted | None`：可见性+双 consent 门禁（任一缺失返回 None 不触发）→ 查有无**新鲜 llm 快照**（engine='llm-v1' AND status='ready' AND expires_at>now，ORDER BY id DESC LIMIT 1）→ 有则 None → 无则 `enqueue_task(key=f"compat-llm-{viewer}-{target}-{UTC日期}", request_hash=viewer:target:日期)`（同 pair 同日至多一任务，成本上限清晰）
- Modify: `app/core/config.py`（`ai_compatibility_llm_ttl_minutes: int = Field(default=10080, gt=0)`，即 7 天）
- Modify: `app/api/routes/ai_compatibility.py`（`get_compatibility_route` :95：读取结果 `status ∈ {COVERAGE_INSUFFICIENT(空态), STALE}` 且 visibility 允许时调 `request_compatibility_llm_refresh`——返回 accepted 则 `JSONResponse(202, CompatibilitySnapshotRecomputeRead 语义 {snapshot_id, task_id, status, poll_after_ms, expires_at})`，None 则维持 200 原结果；成功路径仍显式 commit；快照命中（含 rule 秒回/llm 已缓存）语义不变）
- Test: `tests/test_ai_compatibility.py`（handler 单测，gateway 用 monkeypatch fake）+ `tests/integration/ai/test_ai_compatibility_real_db.py`（续）

**Steps:**
- [x] RED（单测）：handler 粗排 blocked → 写规则快照且无 gateway 调用；gateway 成功 → llm 快照字段全对（engine/brand_label/方向分/100/3 条理由）；gateway 失败 → 规则快照 + `LLM_FALLBACK_RULE` 码；`request_compatibility_llm_refresh`——无授权 None、有新鲜 llm 快照 None、否则入队且同日幂等。
- [x] GREEN：实现 loader/handler/refresh/路由。
- [x] RED（集成）：造数 → GET 无快照 pair → 202+任务 → 跑 handler（fake gateway）→ 二次 GET 200 且 `engine='llm-v1'`、`brand_label='来自良配Ai算法'`、directions 与理由在案；llm 失败路径二次 GET 得规则结果且 reason_codes 含降级码；coverage 门槛与授权门禁回归不变（`test_ai_compatibility.py` shadow 纪律用例零变化）。
- [x] 全量回归。

**验收（方案 WP-C1 验收原文）：** 查看触发 llm 任务→快照更新为 engine='llm-v1' 且带理由与 brand_label；TTL 内二次查看不触发新任务；llm 失败降级写规则结果且 reason_code 标注；coverage/授权门槛回归不变。
**Commit:** `feat(compatibility): compatibility_llm 精算任务+GET触发202+失败降级`

---

### Task 10: 平滑切换 — i_like/likes_me 消费 llm 双向分（P6×C1 收口）

**Goal:** 物化推荐时，pair 已有新鲜 llm 快照则直接消费其双向分（engine='llm-v1'），否则回退规则单向打分——"平滑切换"落成读取优先级。

**Files:**
- Modify: `app/services/ai/recommend.py`（`_load_fresh_llm_directions(db, viewer_id, candidate_ids) -> dict[int, dict]`：`SELECT target_user_id, direction_json FROM ai_compatibility_snapshot WHERE viewer_user_id=:v AND target_user_id IN (...) AND engine='llm-v1' AND status='ready' AND expires_at>now`，同 target 取 id 最大；`materialize_recommendations` 内 i_like/likes_me 打分改为：llm 命中 → `score=direction_json.{viewer_to_target|target_to_viewer}.score`、`engine=llm-v1`、`reason_texts=该方向 reasons`；未命中 → 规则 `score_i_like/score_likes_me`、`engine=rule-v1`；similar 不受影响（纯规则））
- Test: `tests/integration/ai/test_ai_recommend_real_db.py`（续）

**Steps:**
- [x] RED（集成）：造数 pair 写入新鲜 llm 快照 → 物化后该 pair 的 i_like/likes_me 行 `engine='llm-v1'` 且 score=双向分、reason_texts 非空；删除/过期 llm 快照 → 重物化回退 rule-v1；llm 快照不影响 similar 行。
- [x] GREEN：实现。
- [x] 全量回归。

**验收：** 消费优先级与降级路径全绿；方案"平滑切换为消费 ai_compatibility_snapshot 的双向分"落地。
**Commit:** `feat(recommend): i_like/likes_me 消费 llm 双向分（engine标记来源）`

---

### Task 11: 全量回归、手工冒烟与 DoD 收口

**Steps:**
- [x] 全量：`python -m pytest tests/ -q`（对照基线红集合，不扩大）；集成：`python -m pytest tests/integration/ai -q`。
- [x] 手工冒烟（本地起后端 + worker）：
  1. 造数两个用户（含投影）→ publish → 轮询 recommend_rebuild 任务 succeeded；
  2. `GET /ai/recommendations?view=i_like` 返回按分排序卡片；`view=similar` 同理；
  3. `GET /compatibility/{target}` 无快照 → 202+任务 → 稍后重取 → engine='llm-v1' + brand_label + 双向分与 3 条理由（fake/mock provider 下验证链路；真实 provider 跑通另计 prompt 调优）；
  4. 人为令 llm gateway 失败（failure 注入）→ 重取得到规则结果 + `LLM_FALLBACK_RULE`。
- [x] 本文件 checkbox 全部勾选；部署提示入档。

---

## 任务依赖与执行顺序

```
T1 PRODUCT.md ──→ 全部
T2 表 ──→ T3 打分核心 ──→ T4 物化+handler ──→ T5 触发接线 ──→ T6 读取端点
                                     └──→ T10 平滑切换（依赖 T4+T9）
T7 engine列 ──→ T8 LLM调用链 ──→ T9 llm任务+GET触发 ──→ T10
T11 收口：全串行最后
```

- 双主线：P6 线（T2→T6）与 C1 线（T7→T9）相互独立可并行，仅 T10 汇合。
- 规模估算（方案口径）：P6 6~9 人日（T2~T6）、C1 5~7 人日（T7~T10，prompt 调优另计）≈ 11~16 人日。

---

## 阶段完成定义（DoD）

1. `python -m pytest tests/ -q`：新增用例全绿、**红集合不扩大**（基线红见 Global Constraints）。
2. 手工冒烟四条（Task 11）通过；`GET /ai/recommendations` 三视图按分排序；`GET /compatibility/{target}` 未命中→202→llm 快照或降级规则快照，读取端永远有可用结果。
3. `PRODUCT.md` 阶段3章节入档；本文件各任务 checkbox 勾选完毕。
4. 部署提示：生产流量前跑 `database_setup_marriage.py` bootstrap（新表 CREATE IF NOT EXISTS + ensure helper 均幂等可重复执行）；`ai_recommend_enabled` 默认关，按灰度节奏打开；每日批量 crontab 建议行见 `scripts/recommend_daily_batch.py` docstring；真实 LLM 精算的 prompt 调优与点击率灰度（C3 第二步、discovery 接入）不在本阶段范围。
