# AI 画像接口（M04 文字会话、墨相师旅程与画像抽取）

接口前缀：`/api/v1`。本文件覆盖 M04 AI 画像 REST 会话、结构化抽取与墨相师统一旅程的业务边界。M04 是结构化个人画像与理想型画像，**不是 AI 生图**。`profile-sessions` REST 接口的输入模式固定为文字；墨相师统一旅程另通过 `/api/v1/voice/moxiang-master` WebSocket 提供 `text_message`，其音频分支受 P-04 门禁约束。MockAIProvider 仅用于开发/验收，DeepSeek/Dots 等真实 Provider 仍需通过生产审批门禁。本文件保留语音启用条件，WebSocket 消息细节见《墨相师六维实时整理 WebSocket》。

### 变更记录

- 2026-08-08：新增 6 个 `/api/v1/ai/profile-sessions*` 路径；错误统一为 `AiErrorDetail` 形状（含 `request_id`）；普通响应不携带原文、provider trace 或密钥。本期仅会话/回答/草稿抽取；字段确认、发布、历史与删除传播由后续任务提供。
- 2026-08-08：新增 6 个草稿确认/发布/历史/删除路径（§7-§12）；发布只接受 `confirmed` 字段并写不可变 `ai_profile_revision`；删除在同步响应前令草稿与派生结果不可读；补上创建会话错误表缺失的 `409 PROFILE_SESSION_STALE` 行。
- 2026-08-08（Task 12 纠偏）：§8 PATCH 错误表、§9 publish 错误表补 `409 RESULT_STALE` 行；§13 稳定错误码总表补 `RESULT_STALE`。删除不递增草稿 `expected_revision`，客户端持旧 revision 操作已删除草稿返回 `409 RESULT_STALE`（守卫先于乐观锁），而非文档此前声称的 `DRAFT_VERSION_CONFLICT`。
- 2026-08-26（Task 1）：§9 publish 的最低发布门槛统一为 `confirmed_count >= 5`；`suggested`/`rejected`/`deleted` 不计入确认数且不进入 revision；202 响应增加可选异步叙事生成任务 ID `narrative_task_id`。
- 2026-09-02（墨相师候选链补强）：`moxiang_candidate_extract` 的内部 Provider 契约由“仅 `patches`”扩展为“allowlist `fields` + 六维 `patches`”；不改变任何 HTTP/WS 请求或响应字段。明确陈述的结构化事实会先以 `suggested` 候选进入用户确认流程，非白名单和敏感字段仍被服务端丢弃。
- 2026-09-03（旅程发布门槛与提问引导）：§9 发布门槛修正为可配置 `ai_profile_min_fields`（默认 7，取代上文历史值 5）；墨相师 `master` 会话 entry 条目计入 `confirmed_count`，且 personal 发布前必须已确认 `age`+`city_code`（缺项 `400 AI_INPUT_INVALID`）。抽取 prompt 增加六维归属、置信度 rubric 与跨轮去重（`existing_digest`）；知遇每轮按整理进度与缺失硬字段感知提问。单会话自动整理邀请上限由 2 提升到 3（见 WS 文档）。
- 2026-09-03（会话历史 405 修复）：同一 `/profile-sessions/{session_id}/turns` 路径新增 `GET` 历史分页方法，保留原 `POST` 提交方法；首次读取返回最新一页并按 `turn_no` 升序输出，只允许读取本人会话。

通用请求头（所有接口）：

```http
Authorization: Bearer <access_token>   # 必需
Content-Type: application/json          # 写接口必需
Idempotency-Key: <8-128 位 ASCII>       # 所有写接口必需；重复 key + 相同请求摘要回放第一次结果
X-Request-ID: req_01J...                # 可选，1-128 位 [A-Za-z0-9._:-]，用于日志与错误关联
```

通用说明：

- 前置条件：已登录；手机号已验证；已同意授权 `profile_text_extract`（授权版本由创建会话请求中的 `consent_version` 指定）。
- 会话状态固定为 `draft / extracting / awaiting_confirmation / paused / published / failed / cancelled / stale`（统一方案 §7.2）。本期只推进 `draft → extracting → awaiting_confirmation` 与 `paused/resume`；`published/failed/cancelled/stale` 由后续任务或版本/授权变化产生。
- 会话过期时间默认 7 天（`ai_profile_session_expire_days`）；过期或资料/授权版本变化后，会话转 `stale`，客户端需重新创建会话（`409 PROFILE_SESSION_STALE`）。
- 所有写接口幂等：同 `user_id + 接口 + Idempotency-Key + 请求摘要` 回放第一次结果；请求摘要不同返回 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 错误响应统一为：

```json
{
  "detail": {
    "code": "PROFILE_SESSION_NOT_FOUND",
    "message": "画像会话不存在",
    "request_id": "req_01JXc5...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

`retryable` 是后台任务的重试语义，客户端不得据此推断资源是否存在。

---

## 1. 创建或复用 AI 画像文字会话

**基本信息**：为当前用户创建（或复用）一个 personal/ideal_partner 的 AI 画像文字会话；完整 URL `POST /api/v1/ai/profile-sessions`；HTTP Method `POST`；需要登录（Bearer Token）；前置：手机号已验证、已同意 `profile_text_extract` 授权；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `201 Created`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `subject` | body | string | 是 | 无 | 枚举：`personal` / `ideal_partner` | 画像主体：`personal` 映射到本人已批准资料字段；`ideal_partner` 只能映射到本人偏好投影，永不能写成他人事实 |
| `consent_version` | body | string | 是 | 无 | 1-32 位 | 授权文案版本（如 `profile-text-v1`）；必须存在有效的 `profile_text_extract` 授权 |
| `input_mode` | body | string | 否 | `text` | 固定 `text` | 输入模式；本期只支持文字，语音属于 P-04 |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII `[A-Za-z0-9._:-]` | 幂等键；重复请求回放第一次结果 |

### 请求体示例

合法：

```http
POST /api/v1/ai/profile-sessions HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-session-20260807-01
Content-Type: application/json

{"subject": "personal", "consent_version": "profile-text-v1"}
```

非法示例（`subject` 枚举外 + 缺少 Idempotency-Key）：

```http
POST /api/v1/ai/profile-sessions HTTP/1.1
Authorization: Bearer <access_token>
Content-Type: application/json

{"subject": "company", "consent_version": "profile-text-v1"}
```

响应：`422 校验错误` 或 `400 AI_INPUT_INVALID`（Idempotency-Key 缺失/非法）。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | string | 是 | — | — | 对外会话 ID（hex） | `3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d` |
| `subject` | string | 是 | — | `personal/ideal_partner` | 画像主体 | `personal` |
| `status` | string | 是 | — | 会话状态机（见通用说明） | 当前会话状态；新会话为 `draft` | `draft` |
| `input_mode` | string | 是 | — | 固定 `text` | 输入模式 | `text` |
| `progress` | object | 是 | — | — | 画像构建进度（详见下方展开） | 见下 |
| `current_question` | object | 否 | `null` 表示当前无待追问问题 | — | 由缺失字段字典计算出的下一问 | 见下 |
| `draft_id` | string | 否 | `null` 表示当前会话无活动草稿（如新建尚未抽取） | — | 当前会话的活动草稿 ID（加法字段，Task6 Step2）；前端可直接据此跳转草稿编辑器，无需额外查询 | `dr_1a2b3c4d` |
| `profile_revision` | integer | 是 | — | `>=0` | 创建时快照的本人资料 revision | `1` |
| `preference_revision` | integer | 是 | — | `>=0` | 创建时快照的本人偏好 revision | `0` |
| `expires_at` | string(datetime) | 否 | `null` 表示未设置 | — | 会话过期时间，UTC ISO-8601 | `2026-08-14T08:00:00Z` |
| `created_at` | string(datetime) | 是 | — | — | 创建时间，UTC ISO-8601 | `2026-08-07T08:00:00Z` |

`progress` 对象展开：

| 字段 | 类型 | 必返 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- |
| `basis` | string | 是 | 进度口径，固定 `confirmed_field_coverage`（已确认字段覆盖度，不是完整度） | `confirmed_field_coverage` |
| `value` | number | 是 | 已确认字段数 / allowlist 字段数（0..1）；真实覆盖度，不用计时器伪造 | `0.0` |

`current_question` 对象展开：

| 字段 | 类型 | 必返 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- |
| `id` | string | 是 | 问题 ID，来自服务端问题字典 | `interest_lifestyle_v1` |
| `text` | string | 是 | 问题文案（不诱导敏感信息） | `最近让你投入的事情是什么？` |
| `field_key` | string | 是 | 该问题对应的目标抽取字段（属于 allowlist，加法字段，Task6 Step2）；前端据此稳定映射到 typed field 编辑器，不依赖问题文案或顺序 | `interest_tags` |

### 返回示例

成功（201）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "status": "draft",
  "input_mode": "text",
  "progress": {"basis": "confirmed_field_coverage", "value": 0.0},
  "current_question": {"id": "interest_lifestyle_v1", "text": "最近让你投入的事情是什么？", "field_key": "interest_tags"},
  "draft_id": null,
  "profile_revision": 1,
  "preference_revision": 0,
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：登录、手机号已验证、存在 `profile_text_extract` 授权（`ai_consent_grant` 未撤回）。授权缺失返回 `403 AI_CONSENT_REQUIRED`，不创建任务。
- 调用顺序：画像流程的第一步；同 `user_id + subject` 只保留一个活动会话，重复创建返回已存在会话（幂等复用）。
- 幂等与防重：`Idempotency-Key` 必填；重复请求回放第一次结果。同 user+subject 的活动会话已存在时直接返回该会话，不受 key 影响。
- 频率/额度/次数限制：无独立额度；遵守登录与全局限流。Provider 额度不足返回 `429 AI_QUOTA_EXCEEDED`。
- 状态流转：新会话 `draft`；提交回答后进入 `extracting`。
- 边界场景：授权撤回后创建返回 `403 AI_CONSENT_REQUIRED`；功能开关/合规/保留期未批准返回 `503 AI_FEATURE_DISABLED`。
- 前端处理建议：保存 `session_id` 与 `expires_at`；过期后引导重新创建会话。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 403 | `AI_CONSENT_REQUIRED` | 未授权或已撤回 `profile_text_extract` | false | 展示用途并引导重新授权 |
| 400 | `AI_INPUT_INVALID` | `subject` 枚举非法、`consent_version` 非法或 Idempotency-Key 缺失/非法 | false | 修正参数后重试，不重试授权缺失场景 |
| 409 | `PROFILE_SESSION_STALE` | 已存在但仍 `active` 的复用会话已过期，或会话依赖的资料/授权版本已变化 | false | 重新拉取并创建新会话 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 但请求摘要不同 | false | 更换 key 或复用原响应 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关、合规、保留期或 Provider 批准门禁未满足 | false | 展示稳定禁用状态，提供普通资料编辑 |

---

## 2. 查询本人的 AI 画像会话

**基本信息**：按会话 ID 查询当前用户的会话与回答摘要；完整 URL `GET /api/v1/ai/profile-sessions/{session_id}`；HTTP Method `GET`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外会话 ID |

### 请求体示例

无请求体。

合法：

```text
GET /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d
Authorization: Bearer <access_token>
```

### 返回参数

与「创建会话」的返回参数一致（`ProfileSessionRead`），额外说明：

| 字段 | 类型 | 必返 | 业务含义 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 会话 ID |
| `status` | string | 是 | 当前会话状态（会话可刷新恢复，不依赖客户端本地状态） |
| `progress` / `current_question` | object | 是/否 | 与创建会话一致；`current_question` 由缺失字段字典实时计算 |
| `profile_revision` / `preference_revision` | integer | 是 | 当前资料/偏好 revision 快照 |

### 返回示例

成功（200）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "status": "awaiting_confirmation",
  "input_mode": "text",
  "progress": {"basis": "confirmed_field_coverage", "value": 0.1},
  "current_question": {"id": "city_residence_v1", "text": "你现在生活在哪座城市？", "field_key": "city_code"},
  "draft_id": "dr_1a2b3c4d5e6f7a8b9c0d",
  "profile_revision": 1,
  "preference_revision": 0,
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：已登录；会话存在且属于当前用户。
- 调用顺序：任何异步写接口（turns/delete）返回 `202` 后，可通过本接口刷新会话状态；不依赖本地计时器。
- 幂等与防重：`GET` 幂等，可重复调用。
- 频率/额度/次数限制：无独立额度。
- 状态流转：只读；状态由后台 Worker / 写接口推进。
- 边界场景：不存在或非本人统一 `404 PROFILE_SESSION_NOT_FOUND`（不泄露归属）；会话 `stale` 时返回当前状态，提交回答将返回 `409 PROFILE_SESSION_STALE`。
- 前端处理建议：返回 `current_question` 时展示下一问；`status=stale` 时引导重新创建会话。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在或非本人 | false | 视为不可恢复，引导重新创建；不提示具体原因 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 3. 提交一条文字回答并创建抽取任务

**基本信息**：保存原始回答（原文先落库，抽取失败不删原文），并创建 `profile_extract` 任务；完整 URL `POST /api/v1/ai/profile-sessions/{session_id}/turns`；HTTP Method `POST`；需要登录（Bearer Token）；权限：仅本人且会话未结束；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外会话 ID |
| `client_turn_id` | body | string | 是 | 无 | 8-128 位 | 客户端生成的轮次 ID；同会话内唯一，重复提交回放原 turn 且不创建第二个任务 |
| `answer_text` | body | string | 是 | 无 | 去除首尾空白后 1-2000 字 | 原始回答文本；原文不入普通日志 |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键；同 key 同请求摘要回放第一次任务 |

### 请求体示例

合法：

```http
POST /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/turns HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-turn-20260807-01
Content-Type: application/json

{"client_turn_id": "turn-001", "answer_text": "周末喜欢看展"}
```

非法示例（`answer_text` 全空白）：

```http
POST /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/turns HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-turn-20260807-01
Content-Type: application/json

{"client_turn_id": "turn-001", "answer_text": "   "}
```

响应：`400 AI_INPUT_INVALID`（长度/内容非法，不重试）。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `turn_id` | string | 是 | — | — | 服务端 turn ID | `5b8c7d6e...` |
| `session_id` | string | 是 | — | — | 所属会话 ID | `3f2a9c0e...` |
| `client_turn_id` | string | 是 | — | — | 客户端轮次 ID | `turn-001` |
| `turn_no` | integer | 是 | — | `>=1` | 会话内轮次序号 | `1` |
| `role` | string | 是 | — | 固定 `user` | 回答方 | `user` |
| `status` | string | 是 | — | 固定 `saved` | turn 落库状态 | `saved` |
| `replayed` | boolean | 是 | — | `false` 新建 / `true` 重复 `client_turn_id` 回放 | 是否回放已存在 turn（回放时不创建第二个任务） | `false` |
| `task_id` | string | 否 | `null` 表示回放场景未创建新任务 | — | `profile_extract` 任务 ID | `7a2b1c3d...` |
| `task_status` | string | 否 | `null` 表示回放场景 | `queued` 等任务状态机值 | 抽取任务状态 | `queued` |
| `stage` | string | 否 | `null` 表示尚无阶段 | — | 真实执行阶段 | `extracting` |
| `poll_after_ms` | integer | 是 | — | `>=0`；进行态 `1000`，回放 `0` | 下次轮询建议间隔毫秒 | `1000` |
| `expires_at` | string(datetime) | 否 | `null` 表示回放场景无租约 | — | 任务租约过期时间，UTC ISO-8601 | `2026-08-07T08:10:00Z` |

### 返回示例

成功（202，新任务）：

```json
{
  "turn_id": "5b8c7d6e4f3a2b1c9d0e8f7a6b5c4d3e",
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "client_turn_id": "turn-001",
  "turn_no": 1,
  "role": "user",
  "status": "saved",
  "replayed": false,
  "task_id": "7a2b1c3d4e5f6a7b8c9d0e1f2a3b4c5d",
  "task_status": "queued",
  "stage": "extracting",
  "poll_after_ms": 1000,
  "expires_at": "2026-08-07T08:10:00Z"
}
```

重复 `client_turn_id`（202，回放，`replayed=true`、`task_id=null`）：

```json
{
  "turn_id": "5b8c7d6e4f3a2b1c9d0e8f7a6b5c4d3e",
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "client_turn_id": "turn-001",
  "turn_no": 1,
  "role": "user",
  "status": "saved",
  "replayed": true,
  "task_id": null,
  "task_status": null,
  "stage": null,
  "poll_after_ms": 0,
  "expires_at": null
}
```

### 使用方法与业务规则

- 前置条件：会话属于本人且未结束（`draft/extracting/awaiting_confirmation/paused`）；`profile_text_extract` 授权仍有效。
- 调用顺序：创建会话后提交回答；每次提交推进会话 `draft → extracting`（或 `awaiting_confirmation → extracting`）；抽取完成后由 Worker 推进到 `awaiting_confirmation`，客户端可查询任务状态（`GET /api/v1/ai/tasks/{task_id}`）或刷新会话。
- 幂等与防重：`client_turn_id` 同会话内唯一，重复提交回放原 turn 且不再创建第二个 task（`count_tasks(turn_id)==1`）；`Idempotency-Key` 保证同 key 请求摘要回放同一任务，不同摘要返回 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 频率/额度/次数限制：无独立次数限制；Provider 额度不足返回 `429 AI_QUOTA_EXCEEDED`；遵守登录与全局限流。
- 状态流转：原文先落库（`saved`），再创建 `profile_extract` 任务（`queued → leased → running → succeeded`）；抽取失败（schema-invalid/timeout）只改变任务状态（`failed`/`retry_wait`），**不产生已发布字段，也不删除原文**。
- 边界场景：会话不存在/非本人/已结束统一 `404`；资料或授权版本变化/过期 `409 PROFILE_SESSION_STALE`；授权撤回 `403 AI_CONSENT_REQUIRED`；`answer_text` 超长或空白 `400 AI_INPUT_INVALID`。
- 前端处理建议：为每个新回答生成新的 `client_turn_id`（8-128 位），重试同一回答时复用同一个 `client_turn_id` 与 Idempotency-Key；`202` 后通过任务接口轮询恢复。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | `answer_text` 空白/超长、`client_turn_id` 长度非法 | false | 修正参数后重试，不重试版本类错误 |
| 403 | `AI_CONSENT_REQUIRED` | 授权未授权或已撤回 | false | 展示用途并引导重新授权 |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在、非本人或已结束 | false | 重新创建会话 |
| 409 | `PROFILE_SESSION_STALE` | 会话依赖的资料/授权版本变化或过期 | false | 重新拉取并创建新会话 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 不同请求摘要 | false | 更换 key 或复用原响应 |
| 429 | `AI_QUOTA_EXCEEDED` | 用户或 Provider 额度耗尽 | true | 展示冷却时间或手工路径 |
| 503 | `AI_TEMPORARILY_UNAVAILABLE` | Provider/任务基础设施临时失败 | true | 重试同一 task，不重复提交 turn |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

### 3.1 分页读取墨相师会话历史

**基本信息**：恢复墨相师页面中的已持久化用户消息与助手回复；完整 URL `GET /api/v1/ai/profile-sessions/{session_id}/turns`；HTTP Method `GET`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。这是对同路径既有 `POST` 方法的加法兼容，不改变提交回答契约。

#### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位，`^[a-z0-9_]+$` | 要恢复的会话 ID | `3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d` | `../../other` |
| `before_turn_no` | query | integer 或 null | 否 | `null` | `>=1`，exclusive | 读取该轮次之前的更早记录；首次请求省略 | `39` | `0` |
| `limit` | query | integer | 否 | `50` | `1..100` | 单页最多返回的 turn 数 | `50` | `101` |

#### 请求体示例

无请求体。

合法请求：

```http
GET /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/turns?limit=50 HTTP/1.1
Authorization: Bearer <access_token>
```

更早一页：

```http
GET /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/turns?before_turn_no=39&limit=50 HTTP/1.1
Authorization: Bearer <access_token>
```

非法请求（游标为 0）：

```http
GET /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/turns?before_turn_no=0 HTTP/1.1
Authorization: Bearer <access_token>
```

响应：`422 Unprocessable Entity`，不访问会话数据。

#### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | string | 是 | — | — | 当前会话 ID | `3f2a9c0e...` |
| `subject` | string | 是 | — | `personal/ideal_partner` | 会话画像主体 | `personal` |
| `turns` | array | 是 | 空数组表示会话尚无消息或该页已读完 | — | 当前页历史，始终按 `turn_no` 升序 | 见下 |
| `turns[].turn_id` | string | 是 | — | — | 服务端 turn ID，用于前端重连去重 | `t-39` |
| `turns[].turn_no` | integer | 是 | — | `>=1` | 会话内严格递增轮次 | `39` |
| `turns[].role` | string | 是 | — | `user/assistant` | 消息角色 | `user` |
| `turns[].answer_text` | string | 是 | — | 1-2000 字 | 持久化消息正文；前端适配为 `content` | `我在关系里比较慢热` |
| `turns[].client_turn_id` | string | 是 | — | — | 客户端或助手持久化轮次 ID | `client-39` |
| `turns[].created_at` | string(datetime) 或 null | 否 | 历史兼容行没有时间时为 `null` | UTC ISO-8601 | 消息创建时间 | `2026-09-03T10:00:00` |
| `next_before_turn_no` | integer 或 null | 是 | `null` 表示没有更早记录 | `>=1` | 下一页应传入的 exclusive 游标，即本页最小 `turn_no` | `39` |

#### 返回示例

成功（200）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "turns": [
    {
      "turn_id": "t-39",
      "turn_no": 39,
      "role": "user",
      "answer_text": "我在关系里比较慢热",
      "client_turn_id": "client-39",
      "created_at": "2026-09-03T10:00:00"
    }
  ],
  "next_before_turn_no": 39
}
```

无消息或已读完（200）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "turns": [],
  "next_before_turn_no": null
}
```

#### 使用方法与业务规则

- 前置条件：已登录；会话存在且属于当前用户。不存在和非本人统一返回相同 404，防止枚举他人会话。
- 调用顺序：进入或 WebSocket 恢复会话后，首次省略 `before_turn_no` 读取最近一页；上拉时把响应的 `next_before_turn_no` 原样传回，直到它为 `null`。
- 幂等与防重：`GET` 幂等且不需要 `Idempotency-Key`；前端按 `turn_id` 与本地消息去重。
- 频率/额度/次数限制：无独立额度；遵守登录与全局只读请求限流。
- 状态流转：只读，不改变会话、turn、草稿或任务状态；已结束会话历史仍可由本人恢复查看。
- 边界场景：首次页读取最新 `limit` 条但按升序返回；游标 exclusive，页间不重复；历史读取失败不得清空前端已有消息。
- 兼容性：加法接口；原 `POST /profile-sessions/{session_id}/turns` 保持不变。响应继续使用数据库字段名 `answer_text`，现有前端已兼容映射为 `content`。

#### 错误

| HTTP | 业务码 | 触发条件 | retryable | 错误响应摘要 | 前端处理建议 |
| --- | --- | --- | --- | --- | --- |
| 401 | — | 未登录或 Token 失效 | false | `{"detail":"请先登录"}` | 引导重新登录 |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在或不属于当前用户 | false | `detail.code=PROFILE_SESSION_NOT_FOUND` | 停止恢复，不区分不存在与越权 |
| 422 | — | `session_id` 格式非法、`before_turn_no < 1` 或 `limit` 超出 1..100 | false | FastAPI 参数校验错误 | 修正参数，不重试原请求 |
| 503 | `AI_TEMPORARILY_UNAVAILABLE` | 数据库或历史仓储暂时失败 | true | `detail.code=AI_TEMPORARILY_UNAVAILABLE` | 保留本地消息，稍后重试同一 GET |

---

## 4. 暂停 AI 画像会话

**基本信息**：仅允许 `draft/extracting/awaiting_confirmation` 会话暂停；完整 URL `POST /api/v1/ai/profile-sessions/{session_id}/pause`；HTTP Method `POST`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外会话 ID |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键 |

### 请求体示例

无请求体。

合法：

```http
POST /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/pause HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-pause-20260807-01
```

### 返回参数

与「查询会话」的返回参数一致（`ProfileSessionRead`）；暂停成功时 `status=paused`。

### 返回示例

成功（200）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "status": "paused",
  "input_mode": "text",
  "progress": {"basis": "confirmed_field_coverage", "value": 0.1},
  "current_question": {"id": "city_residence_v1", "text": "你现在生活在哪座城市？"},
  "profile_revision": 1,
  "preference_revision": 0,
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：会话属于本人且处于 `draft/extracting/awaiting_confirmation`。
- 调用顺序：暂停前建议先确认无进行中的抽取任务；暂停不改变已保存的 turn。
- 幂等与防重：重复暂停返回当前状态（幂等）。
- 频率/额度/次数限制：无独立额度。
- 状态流转：`draft/extracting/awaiting_confirmation → paused`；`paused` 可 `resume` 恢复。
- 边界场景：会话不存在/非本人/已结束统一 `404`；`stale` 会话返回 `409 PROFILE_SESSION_STALE`；已暂停会话重复暂停返回当前 `paused`。
- 前端处理建议：暂停后前端可退出画像流程；恢复时调用 resume 接口。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | Idempotency-Key 缺失/非法 | false | 修正后重试 |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在、非本人或状态不可暂停 | false | 重新创建会话 |
| 409 | `PROFILE_SESSION_STALE` | 会话已 stale | false | 重新创建会话 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 5. 恢复 AI 画像会话

**基本信息**：恢复已暂停的会话；非 `stale/cancelled` 均可恢复；完整 URL `POST /api/v1/ai/profile-sessions/{session_id}/resume`；HTTP Method `POST`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外会话 ID |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键 |

### 请求体示例

无请求体。

合法：

```http
POST /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/resume HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-resume-20260807-01
```

### 返回参数

与「查询会话」的返回参数一致（`ProfileSessionRead`）；恢复成功时 `status=draft` 或 `awaiting_confirmation`（取决于是否已有草稿字段）。

### 返回示例

成功（200）：

```json
{
  "session_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "subject": "personal",
  "status": "awaiting_confirmation",
  "input_mode": "text",
  "progress": {"basis": "confirmed_field_coverage", "value": 0.1},
  "current_question": {"id": "city_residence_v1", "text": "你现在生活在哪座城市？", "field_key": "city_code"},
  "draft_id": "dr_1a2b3c4d5e6f7a8b9c0d",
  "profile_revision": 1,
  "preference_revision": 0,
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：会话属于本人且未 `stale/cancelled`；`stale/cancelled` 不可恢复。
- 调用顺序：暂停后调用；恢复不改变已保存的 turn。
- 幂等与防重：重复恢复幂等（已非 paused 时返回当前状态）。
- 频率/额度/次数限制：无独立额度。
- 状态流转：`paused → draft / awaiting_confirmation`；恢复后会话继续接受新回答。
- 边界场景：过期返回 `409 PROFILE_SESSION_STALE`；已结束/非本人统一 `404`。
- 前端处理建议：恢复后根据 `current_question` 继续追问。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | Idempotency-Key 缺失/非法 | false | 修正后重试 |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在、非本人或已 cancelled | false | 重新创建会话 |
| 409 | `PROFILE_SESSION_STALE` | 会话 stale 或过期 | false | 重新创建会话 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 6. 软删除 AI 画像会话

**基本信息**：软删除会话（同步隐藏）并创建 `cleanup` 清理任务；完整 URL `DELETE /api/v1/ai/profile-sessions/{session_id}`；HTTP Method `DELETE`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外会话 ID |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键；重复删除回放同一 cleanup 任务 |

### 请求体示例

无请求体。

合法：

```text
DELETE /api/v1/ai/profile-sessions/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d
Authorization: Bearer <access_token>
Idempotency-Key: profile-delete-20260807-01
```

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | string | 是 | — | — | `cleanup` 清理任务 ID | `1a2b3c4d...` |
| `status` | string | 是 | — | 固定 `queued` | 清理任务状态 | `queued` |
| `cleanup_requested` | boolean | 是 | — | 固定 `true` | 软删除受理标记 | `true` |

### 返回示例

成功（202）：

```json
{
  "task_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
  "status": "queued",
  "cleanup_requested": true
}
```

### 使用方法与业务规则

- 前置条件：会话属于本人；会话需未删除。
- 调用顺序：任何时间可调用；删除后会话立即不可读（`active_status=0`），清理任务异步执行。
- 幂等与防重：软删除幂等；重复删除（同 Idempotency-Key）回放同一 cleanup 任务；**已发布 revision 不隐式删除**（删除传播与投影清理由后续任务提供）。
- 频率/额度/次数限制：无独立额度。
- 状态流转：`任何状态 → cancelled`（同步）＋ `cleanup` 任务（异步清理）。
- 边界场景：不存在或非本人统一 `404`；删除后原 `session_id` 的 GET/提交回答均按 `404` 处理。
- 前端处理建议：删除后展示确认完成；`task_id` 可用于跟踪清理进度。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | Idempotency-Key 缺失/非法 | false | 修正后重试 |
| 404 | `PROFILE_SESSION_NOT_FOUND` | 会话不存在、非本人或已删除 | false | 提示会话不存在 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 不同请求摘要 | false | 更换 key 或复用原响应 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 7. 查询本人的 AI 画像字段草稿

**基本信息**：按草稿 ID 查询当前用户的字段候选与来源证据（不可见字段仍展示状态，值标记 `deleted`）；完整 URL `GET /api/v1/ai/profile-drafts/{draft_id}`；HTTP Method `GET`；需要登录（Bearer Token）；权限：仅本人且授权有效；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外草稿 ID |

### 请求体示例

无请求体。

合法：

```text
GET /api/v1/ai/profile-drafts/dr_1a2b3c4d5e6f7a8b9c0d
Authorization: Bearer <access_token>
```

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `draft_id` | string | 是 | — | — | 对外草稿 ID | `dr_1a2b3c4d...` |
| `subject` | string | 是 | — | `personal/ideal_partner` | 画像主体 | `personal` |
| `status` | string | 是 | — | `draft/published/deleted` | 草稿状态；已发布草稿只读 | `draft` |
| `expected_revision` | integer | 是 | — | `>=0` | 乐观锁版本；PATCH/publish 必须携带 | `3` |
| `policy_revision` | string | 是 | — | — | 策略版本快照 | `ai-policy-2026-08-07-v1` |
| `schema_version` | string | 是 | — | 固定 `profile-extract-v1` | 抽取 Schema 版本 | `profile-extract-v1` |
| `fields` | array | 是 | 空数组表示无候选字段 | — | 字段候选列表（展开见下） | 见下 |
| `expires_at` | string(datetime) | 否 | `null` 表示未设置 | — | 草稿过期时间 | `2026-08-14T08:00:00Z` |
| `created_at` / `updated_at` | string(datetime) | 是 | — | — | 创建/更新时间 | `2026-08-07T08:00:00Z` |

`fields[]` 对象展开：

| 字段 | 类型 | 必返 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `field_key` | string | 是 | 只属于 allowlist | 字段键 | `interest_tags` |
| `subject` | string | 是 | `personal/ideal_partner` | 字段主体（发布时以此为准） | `personal` |
| `value` | any | 是 | — | 结构化字段值 | `["看展"]` |
| `display_value` | string | 否 | `null` 表示无展示文本 | 展示值 | `看展` |
| `confidence` | number | 是 | `0..1` | 抽取置信度 | `0.91` |
| `needs_confirmation` | boolean | 是 | `true` 表示非 `confirmed` | 是否仍需确认 | `false` |
| `confirmation_status` | string | 是 | `suggested/confirmed/rejected/deleted` | 确认状态；仅 `confirmed` 可发布 | `confirmed` |
| `content_hash` | string | 否 | `null` 表示无哈希 | 字段内容哈希 | `a1b2c3d4...` |

### 返回示例

成功（200）：

```json
{
  "draft_id": "dr_1a2b3c4d5e6f7a8b9c0d1e2f",
  "subject": "personal",
  "status": "draft",
  "expected_revision": 3,
  "policy_revision": "ai-policy-2026-08-07-v1",
  "schema_version": "profile-extract-v1",
  "fields": [
    {"field_key": "interest_tags", "subject": "personal", "value": ["看展"], "display_value": "看展", "confidence": 0.91, "needs_confirmation": false, "confirmation_status": "confirmed", "content_hash": "a1b2c3d4..."},
    {"field_key": "income_band", "subject": "personal", "value": "high", "display_value": "high", "confidence": 0.72, "needs_confirmation": true, "confirmation_status": "suggested", "content_hash": "e5f6a7b8..."}
  ],
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z",
  "updated_at": "2026-08-07T09:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：已登录；草稿存在且属于当前用户。
- 调用顺序：抽取完成后（会话 `awaiting_confirmation`）读取草稿，逐项确认后调用 PATCH。
- 幂等与防重：`GET` 幂等，可重复调用。
- 频率/额度/次数限制：无独立额度。
- 状态流转：只读；确认状态变化只通过 PATCH。
- 边界场景：不存在或非本人统一 `404`（不泄露归属）；已发布/已删除草稿仍可读（只读历史参考）。
- 前端处理建议：用 `expected_revision` 作为下一次 PATCH/publish 的乐观锁参数；展示 `needs_confirmation` 为真的字段。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 404 | `PROFILE_DRAFT_NOT_FOUND` | 草稿不存在或非本人 | false | 提示草稿不存在，不提示具体原因 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 8. 逐项确认/修改/拒绝/删除草稿字段

**基本信息**：对草稿字段逐项执行 `confirm` / `replace` / `reject` / `delete`；每个 action 都携带旧 revision（不匹配返回 `409 DRAFT_VERSION_CONFLICT`）；完整 URL `PATCH /api/v1/ai/profile-drafts/{draft_id}`；HTTP Method `PATCH`；需要登录（Bearer Token）；权限：仅本人且字段在白名单；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外草稿 ID |
| `expected_revision` | body | integer | 是 | 无 | `>=0` | 草稿乐观锁版本；必须等于当前 `expected_revision` |
| `actions` | body | array | 是 | 无 | 1-50 项 | 字段动作列表（展开见下） |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键 |

`actions[]` 对象展开：

| 字段 | 类型 | 必填 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `field_key` | string | 是 | 只属于 allowlist 且存在于本草稿 | 目标字段 | `interest_tags` |
| `action` | string | 是 | `confirm/replace/reject/delete` | 动作：confirm 确认、replace 替换并确认、reject 拒绝、delete 标记不可见 | `confirm` |
| `value` | any | 否（`replace` 必填） | — | 新字段值；标签类字段必须是非空字符串数组 | `["旅行","看展"]` |
| `expected_revision` | integer | 是 | `>=0` | 执行该动作时的旧版本；不匹配当前版本返回 409 | `1` |

### 请求体示例

合法：

```http
PATCH /api/v1/ai/profile-drafts/dr_1a2b3c4d5e6f7a8b9c0d HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: draft-confirm-20260807-01
Content-Type: application/json

{
  "expected_revision": 1,
  "actions": [
    {"field_key": "interest_tags", "action": "replace", "value": ["旅行", "看展"], "expected_revision": 1},
    {"field_key": "income_band", "action": "confirm", "expected_revision": 1}
  ]
}
```

非法示例（action 的 `expected_revision` 与草稿版本不符，或 `replace` 传入空标签数组）：

```http
PATCH /api/v1/ai/profile-drafts/dr_1a2b3c4d5e6f7a8b9c0d HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: draft-confirm-20260807-02
Content-Type: application/json

{
  "expected_revision": 1,
  "actions": [
    {"field_key": "interest_tags", "action": "replace", "value": [], "expected_revision": 2}
  ]
}
```

响应：`409 DRAFT_VERSION_CONFLICT` 或 `400 AI_INPUT_INVALID`。

### 返回参数

与「查询草稿」的返回参数一致（`ProfileDraftRead`）；`expected_revision` 已递增为最新值，字段确认状态为本次动作后的结果。

### 返回示例

成功（200）：

```json
{
  "draft_id": "dr_1a2b3c4d5e6f7a8b9c0d",
  "subject": "personal",
  "status": "draft",
  "expected_revision": 2,
  "policy_revision": "ai-policy-2026-08-07-v1",
  "schema_version": "profile-extract-v1",
  "fields": [
    {"field_key": "interest_tags", "subject": "personal", "value": ["旅行", "看展"], "display_value": "旅行, 看展", "confidence": 0.9, "needs_confirmation": false, "confirmation_status": "confirmed", "content_hash": "9a8b7c6d..."},
    {"field_key": "income_band", "subject": "personal", "value": "high", "display_value": "high", "confidence": 0.72, "needs_confirmation": false, "confirmation_status": "confirmed", "content_hash": "e5f6a7b8..."}
  ],
  "expires_at": "2026-08-14T08:00:00Z",
  "created_at": "2026-08-07T08:00:00Z",
  "updated_at": "2026-08-07T09:05:00Z"
}
```

### 使用方法与业务规则

- 前置条件：草稿属于本人；字段在白名单内且存在于本草稿。
- 调用顺序：读取草稿后逐项确认；每个 action 的 `expected_revision` 必须等于当前草稿版本（并发编辑保护）。
- 幂等与防重：`Idempotency-Key` 必填；同 key 同请求摘要回放第一次结果。
- 频率/额度/次数限制：无独立额度。
- 状态流转：`replace` 先做值域/长度/枚举校验（标签类字段必须是非空字符串数组）再置为 `confirmed` 并重算 content_hash，来源引用保留；`delete` 只把字段标记为 `deleted`（发布时不可见，不物理删除原文）；`reject` 标记 `rejected`；成功后草稿 `expected_revision + 1`。
- 边界场景：版本冲突返回 `409 DRAFT_VERSION_CONFLICT`（拉取最新草稿提示合并）；字段不存在或不在白名单返回 `400 AI_INPUT_INVALID`；草稿不存在或非本人统一 `404`。
- 前端处理建议：用 GET 返回的 `expected_revision` 发起 PATCH；收到 409 后重新 GET 合并，不要盲目重放。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | action/字段非法、`value` 校验失败或 Idempotency-Key 非法 | false | 修正参数后重试 |
| 404 | `PROFILE_DRAFT_NOT_FOUND` | 草稿不存在或非本人 | false | 提示草稿不存在 |
| 409 | `DRAFT_VERSION_CONFLICT` | 草稿级或任一 action 的 `expected_revision` 不匹配 | false | 拉取最新草稿，提示合并 |
| 409 | `RESULT_STALE` | 草稿已进入只读终态（`published`/`deleted`/`cancelled`）；守卫先于乐观锁，无论客户端带什么 revision 都不可再编辑 | false | 提示草稿已终态，回到历史/删除流程 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 9. 发布已确认字段

**基本信息**：只把 `confirmed` 字段写入不可变 `ai_profile_revision` 并创建投影任务；完整 URL `POST /api/v1/ai/profile-drafts/{draft_id}/publish`；HTTP Method `POST`；需要登录（Bearer Token）；权限：仅本人、至少 `ai_profile_min_fields` 个 confirmed 字段（默认 7）、主体权限正确；请求 `Content-Type`：无请求体（`expected_revision` 通过查询参数）；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

> **墨相师 master 会话门槛（2026-09-03）**：草稿所属会话 `session_kind='master'` 时，`entry` 条目与 `structured` 字段一并计入 `confirmed_count`（自然对话以六维条目为主要产物，沿用 structured-only 会卡死发布）；且「我的墨相」（personal）发布前必须已确认 `age` 与 `city_code`，缺任一项返回 `400 AI_INPUT_INVALID`。愿遇之相不受此底线约束；旧 build/update 会话仍只数 structured 字段。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外草稿 ID |
| `expected_revision` | query | integer | 是 | 无 | `>=0` | 草稿乐观锁版本；必须等于当前 `expected_revision` |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键；同 key 同 payload 回放同一任务 |

### 请求体示例

无请求体。

合法：

```http
POST /api/v1/ai/profile-drafts/dr_1a2b3c4d5e6f7a8b9c0d/publish?expected_revision=2 HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-publish-20260807-01
```

非法示例（缺少 `expected_revision` 查询参数）：

```http
POST /api/v1/ai/profile-drafts/dr_1a2b3c4d5e6f7a8b9c0d/publish HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-publish-20260807-02
```

响应：`400 AI_INPUT_INVALID`。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | string | 是 | — | — | `profile_projection` 投影任务 ID | `9f8e7d6c...` |
| `narrative_task_id` | string | 否 | `null` 仅表示历史/兼容数据中未创建或配套叙事任务缺失 | — | 异步 narrative 生成任务 ID；非空时前端通过通用任务接口轮询叙事成稿状态；同 Idempotency-Key 回放应复用首次发布的值 | `narr_7e6d5c4b...` |
| `status` | string | 是 | — | 固定 `queued` | 投影任务状态 | `queued` |
| `stage` | string | 否 | `null` | — | 任务阶段 | `null` |
| `poll_after_ms` | integer | 是 | — | `>=0`；新建 `1000`，回放 `0` | 下次轮询间隔 | `1000` |
| `expires_at` | string(datetime) | 否 | `null` | — | 任务租约过期时间 | `null` |
| `replayed` | boolean | 是 | — | `false` 新建 / `true` 同 key 回放 | 是否回放已存在任务 | `false` |
| `revision_id` | integer | 否 | `null` 表示回放 | — | 新建的不可变版本 ID | `42` |
| `revision_no` | integer | 否 | `null` 表示回放 | `>=1` | 该主体的发布版本号 | `1` |
| `subject` | string | 否 | `null` 表示回放 | `personal/ideal_partner` | 发布主体 | `personal` |
| `field_count` | integer | 否 | `null` 表示回放 | `>=` 发布门槛（默认 7；master 含 entry） | 本次发布写入的 confirmed 字段数 | `7` |

### 返回示例

成功（202，新建）：

```json
{
  "task_id": "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c",
  "narrative_task_id": "narr_7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b",
  "status": "queued",
  "stage": null,
  "poll_after_ms": 1000,
  "expires_at": null,
  "replayed": false,
  "revision_id": 42,
  "revision_no": 1,
  "subject": "personal",
  "field_count": 5
}
```

成功（202，同 Idempotency-Key 回放）：

```json
{
  "task_id": "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c",
  "narrative_task_id": "narr_7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b",
  "status": "queued",
  "stage": null,
  "poll_after_ms": 0,
  "expires_at": null,
  "replayed": true,
  "revision_id": null,
  "revision_no": null,
  "subject": null,
  "field_count": null
}
```

### 使用方法与业务规则

- 前置条件：草稿属于本人；至少 `ai_profile_min_fields` 个 `confirmed` 字段（默认 7；master 会话 entry 计入，personal 另需已确认 `age`+`city_code`，见 §9 门槛说明）；`expected_revision` 匹配。
- 调用顺序：先 PATCH 确认字段，再 publish；发布成功后草稿置为 `published`、所属会话 `published`（历史只读）。
- 幂等与防重：`Idempotency-Key` 必填；同 key 同 `draft_id + expected_revision` 回放同一投影任务，**不重复写 revision、不重复递增 revision 向量**；不同 payload 返回 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 频率/额度/次数限制：无独立额度。
- 状态流转：`confirmed` 字段 → 不可变 `ai_profile_revision`（含逐字段 content_hash/source revision）→ 只递增对应主体 revision（personal → `profile_revision`，ideal_partner → `preference_revision`，互不干扰）→ 写一条 outbox 事件 → 入队投影任务。**未确认字段永不进入发布版本与投影。**
- 边界场景：`confirmed_count` 低于门槛（默认 7）返回 `400 AI_INPUT_INVALID`（build/update 仅 `confirmed` structured 计数，`suggested`/`rejected`/`deleted` 不计入且不进入 revision；master 会话 entry 计入，personal 另需 `age`+`city_code`）；版本不匹配返回 `409 DRAFT_VERSION_CONFLICT`；主体隔离保证 ideal_partner 永不写 personal 事实。
- 前端处理建议：保存 `revision_no`/`revision_id` 用于历史展示；通过 `GET /api/v1/ai/tasks/{task_id}` 轮询投影任务。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | 缺少/非法 `expected_revision` 查询参数、`confirmed_count` 低于门槛（默认 7）、master personal 缺 `age`/`city_code`、Idempotency-Key 非法 | false | 修正参数或先确认足够字段（master 个人画像补齐年龄与城市），不重试版本类错误 |
| 404 | `PROFILE_DRAFT_NOT_FOUND` | 草稿不存在或非本人 | false | 提示草稿不存在 |
| 409 | `DRAFT_VERSION_CONFLICT` | `expected_revision` 不匹配 | false | 拉取最新草稿，提示合并 |
| 409 | `RESULT_STALE` | 草稿已进入只读终态（`published`/`deleted`/`cancelled`）；守卫先于乐观锁，已删除草稿不得用原 `expected_revision` 重新发布 | false | 提示草稿已终态；删除意图不可被静默撤销 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 不同 payload | false | 更换 key 或复用原响应 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 10. 查询本人的发布版本历史

**基本信息**：游标分页返回当前用户已发布的不可变版本（只读）；完整 URL `GET /api/v1/ai/profile-revisions`；HTTP Method `GET`；需要登录（Bearer Token）；权限：仅本人；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `cursor` | query | string | 否 | 无 | 服务端返回的 opaque base64，最长 512 | 分页游标；首屏不传 |
| `limit` | query | integer | 否 | `20` | 1-100 | 每页条数 |

### 请求体示例

无请求体。

合法：

```text
GET /api/v1/ai/profile-revisions?limit=20
Authorization: Bearer <access_token>
```

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `items` | array | 是 | 空数组表示无历史 | 版本列表（展开见下） | 见下 |
| `next_cursor` | string | 否 | `null` 表示没有更多页 | 下一页游标 | `MTI=` |
| `total` | integer | 是 | — | 本人历史版本总数（精确计数） | `1` |
| `total_is_estimate` | boolean | 是 | — | 固定 `false`（精确计数） | `false` |
| `has_more` | boolean | 是 | — | 是否还有下一页 | `false` |

`items[]` 对象展开：

| 字段 | 类型 | 必返 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `revision_id` | integer | 是 | — | 不可变版本 ID | `42` |
| `subject` | string | 是 | `personal/ideal_partner` | 发布主体 | `personal` |
| `revision_no` | integer | 是 | `>=1` | 该主体的发布版本号 | `1` |
| `policy_revision` | string | 是 | — | 发布时的策略版本 | `ai-policy-2026-08-07-v1` |
| `field_count` | integer | 是 | `0` 表示空发布（历史兼容数据） | `>=0`；2026-08-26 起新发布为 `>=5` | 该版本字段快照数；历史 immutable revision 可能低于 5，不迁移、不隐藏 | `5` |
| `published_at` | string(datetime) | 是 | — | 发布时间 | `2026-08-07T09:10:00Z` |

### 返回示例

成功（200；历史 immutable revision 可能低于 5，不迁移、不隐藏）：

```json
{
  "items": [
    {"revision_id": 42, "subject": "personal", "revision_no": 1, "policy_revision": "ai-policy-2026-08-07-v1", "field_count": 2, "published_at": "2026-08-07T09:10:00Z"}
  ],
  "next_cursor": null,
  "total": 1,
  "total_is_estimate": false,
  "has_more": false
}
```

### 使用方法与业务规则

- 前置条件：已登录。
- 调用顺序：发布后通过本接口查看历史；通过 restore 恢复旧版本为新的可编辑草稿。
- 幂等与防重：`GET` 幂等；同一快照在同一游标下返回稳定顺序（按 revision id 倒序）。
- 频率/额度/次数限制：无独立额度。
- 状态流转：只读；历史版本永不修改、永不删除（删除传播只标记投影不可读）。
- 边界场景：仅返回本人历史；无历史返回 `items: []`、`total=0`、`has_more=false`。
- 前端处理建议：使用 `next_cursor` 翻页；`has_more=false` 时停止请求。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 11. 从历史版本恢复为新的可编辑草稿

**基本信息**：从不可变版本快照创建新的可编辑草稿（字段回填 `suggested`，旧版本只读不改）；完整 URL `POST /api/v1/ai/profile-revisions/{revision_id}/restore`；HTTP Method `POST`；需要登录（Bearer Token）；权限：仅本人且旧版本未被策略删除；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `201 Created`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `revision_id` | path | integer | 是 | 无 | `>=1` | 历史版本 ID |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键 |

### 请求体示例

无请求体。

合法：

```http
POST /api/v1/ai/profile-revisions/42/restore HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: profile-restore-20260807-01
```

### 返回参数

与「查询草稿」的返回参数一致（`ProfileDraftRead`）；新草稿 `expected_revision=0`，所有字段 `confirmation_status="suggested"`。

### 返回示例

成功（201）：

```json
{
  "draft_id": "dr_5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b",
  "subject": "personal",
  "status": "draft",
  "expected_revision": 0,
  "policy_revision": "ai-policy-2026-08-07-v1",
  "schema_version": "profile-extract-v1",
  "fields": [
    {"field_key": "interest_tags", "subject": "personal", "value": ["旅行", "看展"], "display_value": "旅行, 看展", "confidence": 0.9, "needs_confirmation": true, "confirmation_status": "suggested", "content_hash": "9a8b7c6d..."}
  ],
  "expires_at": null,
  "created_at": "2026-08-07T09:20:00Z",
  "updated_at": "2026-08-07T09:20:00Z"
}
```

### 使用方法与业务规则

- 前置条件：版本属于本人且未被策略删除。
- 调用顺序：历史展示后调用；恢复出的草稿需再次逐项确认后 publish。
- 幂等与防重：`Idempotency-Key` 必填；恢复是幂等新建——同 key 回放同一次恢复结果。
- 频率/额度/次数限制：无独立额度。
- 状态流转：旧 revision 只读、不更新旧行；新草稿走正常 confirm → publish 流程；新草稿发布后成为新的 revision_no（不覆盖旧版本）。
- 边界场景：版本不存在或非本人统一 `404`（不泄露归属）；字段回填 `suggested`（需要用户再次确认，避免旧内容未经确认进入投影）。
- 前端处理建议：恢复后引导用户进入确认流程；确认全部字段后再 publish。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 404 | `PROFILE_REVISION_NOT_FOUND` | 版本不存在或非本人 | false | 提示版本不存在，不提示具体原因 |
| 400 | `AI_INPUT_INVALID` | Idempotency-Key 缺失/非法 | false | 修正后重试 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 12. 删除 AI 画像 / 删除单个字段

### 12.1 删除整个 AI 画像（按主体）

**基本信息**：删除一个主体的全部 AI 画像（草稿、会话、已发布投影引用），同步令结果不可读并创建清理任务；同意撤回与删除同时生效；完整 URL `DELETE /api/v1/ai/profiles/{subject}`；HTTP Method `DELETE`；需要登录（Bearer Token）；权限：仅本人，`subject=personal/ideal_partner`；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

#### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `subject` | path | string | 是 | 无 | 枚举 `personal/ideal_partner` | 画像主体 |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键；重复删除回放同一清理任务 |

#### 请求体示例

无请求体。

合法：

```text
DELETE /api/v1/ai/profiles/personal
Authorization: Bearer <access_token>
Idempotency-Key: profile-delete-20260807-01
```

#### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | string | 是 | — | — | `cleanup` 清理任务 ID | `2b3c4d5e...` |
| `status` | string | 是 | — | 固定 `queued` | 清理任务状态 | `queued` |
| `cleanup_requested` | boolean | 是 | — | 固定 `true` | 删除受理标记 | `true` |

#### 返回示例

成功（202）：

```json
{
  "task_id": "2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e",
  "status": "queued",
  "cleanup_requested": true
}
```

#### 使用方法与业务规则

- 前置条件：已登录；`subject` 属于本人。
- 调用顺序：任何时间可调用；**同步响应前当前草稿与派生结果已不可读**，清理任务异步执行。
- 幂等与防重：删除幂等；同 Idempotency-Key 回放同一 cleanup 任务（不重复写 outbox 事件）。
- 频率/额度/次数限制：无独立额度。
- 状态流转：同一事务内——草稿置 `deleted`、活动会话置 `cancelled`、已发布投影引用置 `invalidated`、search result 置 `stale`、compatibility snapshot 置 `blocked`、撤回 `profile_text_extract` 授权；递增 `privacy_revision` 并写 outbox 删除事件（personal → `ai_profile_deleted`，ideal_partner → `ai_preference_deleted`）；再入队 `cleanup` 任务（status=`queued`）。异步物理清理（投影/搜索结果/兼容度快照/缓存/可删除原文）由后台消费者执行；审计只保留最小不可逆引用与清理状态。
- 边界场景：重复删除回放同一 task；审计行不删除、已撤回授权不恢复。
- 前端处理建议：展示确认后调用；`task_id` 用于跟踪清理进度；删除后引导重新授权再建会话。

#### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | `subject` 非 `personal/ideal_partner`、Idempotency-Key 非法 | false | 修正参数后重试 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 不同请求摘要 | false | 更换 key 或复用原响应 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

### 12.2 删除 AI 画像单个字段

**基本信息**：删除一个主体的单个字段（同步标记不可见并递增对应主体 revision，再异步清理）；完整 URL `DELETE /api/v1/ai/profiles/{subject}/fields/{field_key}`；HTTP Method `DELETE`；需要登录（Bearer Token）；权限：仅本人、字段属于本人主体；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

#### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `subject` | path | string | 是 | 无 | 枚举 `personal/ideal_partner` | 画像主体 |
| `field_key` | path | string | 是 | 无 | 只属于 allowlist | 目标字段 |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII | 幂等键；重复删除回放同一失效任务 |

#### 请求体示例

无请求体。

合法：

```text
DELETE /api/v1/ai/profiles/personal/fields/interest_tags
Authorization: Bearer <access_token>
Idempotency-Key: profile-field-delete-20260807-01
```

#### 返回参数

与「删除整个 AI 画像」一致（`CleanupTaskAccepted`：`task_id`、`status=queued`、`cleanup_requested=true`）。

#### 返回示例

成功（202）：

```json
{
  "task_id": "3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f",
  "status": "queued",
  "cleanup_requested": true
}
```

#### 使用方法与业务规则

- 前置条件：已登录；字段属于本人主体且在白名单内。
- 调用顺序：任何时间可调用；同步响应前该字段在本主体所有草稿已标记 `deleted`（不可见）。
- 幂等与防重：重复删除（同 Idempotency-Key）回放同一清理任务。
- 频率/额度/次数限制：无独立额度。
- 状态流转：同一事务内——字段在所有草稿中标记 `deleted`；递增对应主体 revision（personal → `profile_revision`，ideal_partner → `preference_revision`）并写 outbox 事件；再入队 `cleanup` 任务。异步清理由后台消费者执行。
- 边界场景：字段不在白名单返回 `400 AI_INPUT_INVALID`；重复删除返回同一 task。
- 前端处理建议：保存 `task_id` 跟踪清理；字段删除后相关投影由消费者重建。

#### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 400 | `AI_INPUT_INVALID` | `field_key` 不在白名单、Idempotency-Key 非法 | false | 修正参数后重试 |
| 409 | `TASK_IDEMPOTENCY_CONFLICT` | 同 Idempotency-Key 不同请求摘要 | false | 更换 key 或复用原响应 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关/批准门禁未满足 | false | 展示稳定禁用状态 |

---

## 13. 稳定错误码

本文件接口使用以下稳定错误码（统一方案 §11.2，执行计划 §3.2）：

| 业务码 | HTTP | retryable | 固定语义 |
| --- | --- | --- | --- |
| `AI_FEATURE_DISABLED` | 503 | false | 功能开关、合规、保留期或 Provider 批准门禁未满足 |
| `AI_CONSENT_REQUIRED` | 403 | false | `profile_text_extract` 未授权或已撤回；不创建任务 |
| `AI_INPUT_INVALID` | 400 | false | `subject`/`answer_text`/`client_turn_id`/字段动作/值域/Idempotency-Key 非法 |
| `AI_QUOTA_EXCEEDED` | 429 | true | 用户或 Provider 额度耗尽 |
| `AI_TEMPORARILY_UNAVAILABLE` | 503 | true | Provider/任务基础设施临时失败（任务转 `retry_wait`） |
| `AI_POLICY_DENIED` | 422 | false | 越权字段、敏感推断或认证伪造（Provider 输出校验拒绝） |
| `PROFILE_SESSION_NOT_FOUND` | 404 | false | 会话不存在或非本人；不泄露归属 |
| `PROFILE_SESSION_STALE` | 409 | false | 会话依赖的资料/授权版本变化或过期，需重新创建会话 |
| `PROFILE_DRAFT_NOT_FOUND` | 404 | false | 草稿不存在或非本人；不泄露归属 |
| `PROFILE_REVISION_NOT_FOUND` | 404 | false | 历史版本不存在或非本人；不泄露归属 |
| `DRAFT_VERSION_CONFLICT` | 409 | false | `expected_revision` 与当前草稿版本不符（并发编辑） |
| `RESULT_STALE` | 409 | false | 草稿已进入只读终态（`published`/`deleted`/`cancelled`）；发布/字段修改前守卫先于乐观锁 |
| `TASK_IDEMPOTENCY_CONFLICT` | 409 | false | 同幂等键但请求摘要不同 |
| `TASK_NOT_FOUND` | 404 | false | 任务不存在或非本人（轮询任务时） |

`retryable` 是后台任务的重试语义，客户端不得据此推断资源是否存在。

---

## 14. 会话状态机与业务规则汇总

| 状态 | 含义 | 可提交回答 | 可暂停 | 可恢复 |
| --- | --- | --- | --- | --- |
| `draft` | 新建会话 | 是 | 是 | — |
| `extracting` | 有抽取任务进行中 | 是 | 是 | — |
| `awaiting_confirmation` | 已有 suggested 草稿待确认 | 是 | 是 | — |
| `paused` | 已暂停 | 是（提交会先恢复语义） | 重复暂停返回当前 | 是 |
| `published` | 已发布（后续任务） | 否 | 否 | 否 |
| `failed` | 失败 | 否 | 否 | 否 |
| `cancelled` | 已取消/软删除 | 否 | 否 | 否 |
| `stale` | 版本/授权变化或过期 | 否（409） | 否（409） | 否（409） |

字段确认状态：`suggested → confirmed / rejected / deleted`；只有 `confirmed` 能进入发布版本与投影（后续任务）。认证字段（如 `realname_status`）不在 allowlist，AI 永不写入。

抽取降级矩阵（统一方案 §6.6）：Provider 超时/429/5xx → 任务 `retry_wait/failed`，草稿保留原文，可重试或手工编辑；Schema/内容治理失败 → 只留错误码和最小审计，不展示生成文本；功能/合规/保留期未批准 → `503 AI_FEATURE_DISABLED`。

---

## 15. 兼容性与后续任务

- 本文件共 13 个路径为 2026-08-08 新增/定稿（6 个会话路径 + 6 个草稿/发布/历史/删除路径 + 1 个删除字段路径），不修改任何旧接口；均已注册到 OpenAPI `paths`。
- 响应字段均为必返/可选语义冻结；后续任务（搜索、匹配度）新增接口时保持本文件字段不破坏性变更。
- 后台 `profile_extract` 任务经 `GET /api/v1/ai/tasks/{task_id}` 轮询（见 `docs/api/AI通用任务.md`），`result_ref` 形如 `profile-draft:{draft_id}`；publish 创建的 `profile_projection` 任务与 delete 创建的 `cleanup` 任务同样经任务接口轮询。
- 删除的**异步物理清理**（清理 `ai_feature_projection`/`ai_search_result`/`ai_compatibility_snapshot`、可删除原文与缓存）由 Task 9/10/11 的后台消费者实现；本任务已保证同步不可读、写 outbox 事件并注册清理消费者占位 handler。审计只保留最小不可逆引用与清理状态；导出能力在合规批准后单独启用，不作为首期默认路径。

---

## 16. P-04 语音/ASR（协议已实现，默认关闭）

P-04 的 STT/TTS 与实时 ASR 协议已经存在，但默认关闭，且不作为本次画像链路的验收前置。`/api/v1/voice/conversation` 与 `/api/v1/voice/moxiang-master` 的音频分支只有在以下条件全部满足后才能启用：

- 产品和合规书面批准语音/转写用途、语言、地域、原始音频与 transcript 保留期、导出和删除 API。
- Provider 完成 DPA/数据出境/训练用途审查，且可以按 task/user 证明删除。
- 音频上传有格式、时长、病毒、内容治理和访问控制；失败可稳定回退文字输入，不重复扣费。
- 未获批前语音相关开关保持关闭（`AI_FEATURE_DISABLED`）；墨相师 WebSocket 的 `text_message` 文字旅程不依赖 ASR。

---

## 17. 墨相师统一旅程与下游投影（2026-09-02）

墨相师生产主路径统一为 `moxiang_journey`：

```text
WS /voice/moxiang-master
  → ai_profile_turn（用户回答先落库）
  → moxiang_candidate_extract（候选理解池）
  → 六维进度 / build_invite
  → 接受邀请后写 ai_profile_draft_field(suggested)
  → PATCH confirm
  → POST publish
  → ai_profile_revision + profile_projection
  → personal_searchable / personal_compatibility
  → AI 搜索、资料合拍参考读取
```

六个固定维度为 `personality_social`、`intimacy_pattern`、`lifestyle`、
`emotional_expression`、`relationship_boundaries`、`future_expectations`。
候选理解可以来自结构化字段或自由条目；维度只由服务端白名单映射，客户端不能提交任意维度。
每一条已持久化的用户 turn 由 `moxiang_candidate_extract` 经统一
`AIGateway.structured_extract(session_kind="master")` 生成候选；任务载荷只保存
`session_id/turn_id/client_turn_id/subject`，不保存原文。Provider 超时或校验失败时
任务按统一重试/失败规则处理，不会用未验证文本直接写候选池。

其中，master Provider 对用户明确陈述的白名单事实输出 `fields`（例如城市、兴趣、
关系目标），并对无法归入固定字段但属于六维画像的内容输出 `patches`。两类结果均先
写入 `ai_profile_candidate`，只有用户接受构建邀请并逐项确认后才可能进入发布版本；
这不是对现有 REST/WS 协议的破坏性变更。

下游准入规则：

- 只有 `confirmed` 字段能进入不可变 `ai_profile_revision`。
- 只有已发布 revision 且 `profile_text_extract` 授权仍有效，才会生成 `personal_searchable` 与 `personal_compatibility` 投影。
- `ideal_partner_preference` 仅 `self_only`，不会作为候选资料泄露。
- 搜索与匹配读取投影时重新校验授权、版本向量、可见性和过期状态；撤回或删除会使结果失效。
- 当前搜索页和首页展示不主动调用新 AI 搜索/兼容度接口；接口已提供给后续接入，旧推荐 `match_score` 继续保持 `legacy-rule-v1` 语义。

历史恢复契约：统一请求层的 `{ success, data }` 包由前端 `api/ai-moxiang.uts` 解包；后端 `answer_text` 映射为消息 `content`。错误响应不会覆盖当前本地消息流，空会话可安全恢复。
