# AI 搜索接口（M03 条件 AST、草稿确认与安全搜索结果）

接口前缀：`/api/v1/ai`。本文件是 M03 AI 搜索（统一方案 §8，执行计划 Task 10）对外的完整契约。自然语言只编译为 Task 1 冻结的 10 个 allowlist 字段，再由服务端静态映射落到现有结构化筛选；模型输出永远不能成为 SQL、列名、`ORDER BY` 或表名。

### 变更记录

- 2026-08-08：新增 7 个 `/api/v1/ai/search-*` 路径；错误统一为 `AiErrorDetail` 形状（含 `request_id`）；`SearchPolicyDenied → 422 AI_POLICY_DENIED`、`SearchInputInvalid → 400 AI_INPUT_INVALID` 固定映射，这两个异常路径不触发数据库查询。旧 `/api/v1/discovery/search`（手工筛选）保持兼容，新搜索失败时前端回退手工筛选。
- 2026-08-08（Task 12 纠偏）：错误码速查补 `400 INVALID_CANDIDATE_CURSOR` 行（与 `app/api/routes/ai_search.py` 读取结果页的 cursor 校验映射一致）。

通用请求头（所有接口）：

```http
Authorization: Bearer <access_token>   # 必需
Content-Type: application/json          # 写接口必需
Idempotency-Key: <8-128 位 ASCII>       # 所有写接口必需；重复 key + 相同请求摘要回放第一次结果
X-Request-ID: req_01J...                # 可选，1-128 位 [A-Za-z0-9._:-]，用于日志与错误关联
```

通用说明：

- 前置条件：已登录；已同意授权 `search_parse`；功能开关开启（`ai_master_enabled` + `ai_search_enabled`，生产环境还需三批准门禁）。开关关闭统一返回 `503 AI_FEATURE_DISABLED`。
- 草稿状态固定为 `parsing / awaiting_confirmation / confirmed / expired / failed`（统一方案 §8.2）。执行任务阶段使用 `validating / checking_visibility / filtering / ranking / completed / empty / partial`；通用 task status 仍使用 AI-CORE 枚举。
- 条件 `user_action` 固定为 `pending / confirmed / edited / removed`。**只有已确认的 hard 条件才会进入筛选**；未确认草稿不能创建候选查询任务（`409 RESULT_STALE`）。
- 解析额度：每用户每分钟 5 次（`ai_search_parse_rate_per_minute`，默认 5），超限返回 `429 AI_QUOTA_EXCEEDED`。
- 所有写接口幂等：同 `user_id + 接口 + Idempotency-Key + 请求摘要` 回放第一次结果；请求摘要不同返回 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 结果读取以 MySQL 为事实源；Redis 断开时从 MySQL 恢复，不阻塞主链路。
- 错误响应统一为：

```json
{
  "detail": {
    "code": "SEARCH_DRAFT_NOT_FOUND",
    "message": "搜索草稿不存在",
    "request_id": "req_01JXc5...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

`retryable` 是后台任务的重试语义，客户端不得据此推断资源是否存在。所有 4xx/5xx 都带 `request_id`；普通响应不含 Provider trace、原文或密钥。

### 错误码速查

| HTTP | code | 触发 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 401 | — | 未登录/令牌失效 | false | 重新登录 |
| 404 | `SEARCH_DRAFT_NOT_FOUND` / `SEARCH_SNAPSHOT_NOT_FOUND` | 草稿/快照不存在或非本人（不泄露归属） | false | 提示已失效，回列表 |
| 400 | `AI_INPUT_INVALID` | query_text 长度非法、Idempotency-Key 非法、operator 非法、value 形状非法、PATCH 缺 expected_condition_revision | false | 修正入参后重试 |
| 400 | `INVALID_CANDIDATE_CURSOR` | 跨查询使用他人 cursor、伪造或超长 cursor（读取结果页时） | false | 丢弃 cursor，从第一页重新请求 |
| 422 | `AI_POLICY_DENIED` | 已确认条件包含 allowlist 外字段（电话/精确位置/敏感推断等） | false | 提示“不支持的条件”，引导手工筛选 |
| 403 | `AI_CONSENT_REQUIRED` | 未同意 `search_parse` 授权或已撤回 | false | 引导重新授权 |
| 429 | `AI_QUOTA_EXCEEDED` | 每分钟解析次数超限 | true | 稍后重试 |
| 409 | `DRAFT_VERSION_CONFLICT` | `expected_condition_revision` 与当前不符 | false | 刷新草稿后重试 |
| 409 | `RESULT_STALE` | 草稿未确认/已过期、草稿状态不可编辑、存在未解决区间冲突 | true | 重新确认/发起新搜索 |
| 503 | `AI_FEATURE_DISABLED` | 功能开关关闭或生产门禁未满足 | false | 提示功能不可用，回退手工筛选 |
| 503 | `AI_TEMPORARILY_UNAVAILABLE` | Provider/任务基础设施临时失败 | true | 稍后重试 |

---

## 1. 创建 AI 搜索草稿（解析任务）

**基本信息**：把自然语言查询文本落为 `parsing` 草稿并创建 `search_parse` 任务；完整 URL `POST /api/v1/ai/search-drafts`；HTTP Method `POST`；需要登录；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `query_text` | body | string | 是 | 无 | 1-1000 字符 | 自然语言查询原文（保存但只用于解析，不进入日志/证据） |
| `source` | body | string | 否 | `null` | 最多 24 字符 | 查询来源标记（如 `manual`/`voice`） |
| `locale` | body | string | 否 | `null` | 最多 16 字符 | 语言区域（如 `zh-CN`） |
| `Idempotency-Key` | header | string | 是 | 无 | 8-128 位 ASCII `[A-Za-z0-9._:-]` | 幂等键；重复请求回放第一次结果 |

### 请求体示例

合法：

```http
POST /api/v1/ai/search-drafts HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: search-parse-20260807-01
Content-Type: application/json

{"query_text":"想找26到32岁、住杭州、本科以上、周末愿意户外的人","source":"manual","locale":"zh-CN"}
```

非法示例（query_text 超过 1000 字符 + 缺少 Idempotency-Key）：

```http
POST /api/v1/ai/search-drafts HTTP/1.1
Authorization: Bearer <access_token>
Content-Type: application/json

{"query_text":"<1001 个字符>"}
```

响应：`400 AI_INPUT_INVALID`。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `draft_id` | string | 是 | — | — | 对外草稿 ID（hex） | `sd_01JXc5...` |
| `status` | string | 是 | — | `parsing` | 草稿进入解析中 | `parsing` |
| `task_id` | string | 是 | — | — | `search_parse` 任务 ID，通过 `GET /api/v1/ai/tasks/{task_id}` 轮询 | `at_01JXc5...` |
| `condition_schema_version` | string | 是 | — | 固定 `search-condition-v1` | 条件 AST schema 版本 | `search-condition-v1` |
| `expires_at` | string(datetime) | 否 | `null` 未设置 | — | 草稿过期时间（默认 24h），过期仍可读摘要但不能确认 | `2026-08-08T08:00:00Z` |

### 返回示例

```json
{
  "draft_id": "sd_01JXc5...",
  "status": "parsing",
  "task_id": "at_01JXc5...",
  "condition_schema_version": "search-condition-v1",
  "expires_at": "2026-08-08T08:00:00Z"
}
```

### 使用方法与业务规则

- 前置条件：已登录、已同意 `search_parse` 授权、开关开启。
- 调用顺序：创建草稿 → 轮询任务（解析完成后草稿转 `awaiting_confirmation`）→ `GET` 草稿查看条件 → `PATCH` 确认/修改/删除 → `POST confirm`。
- 幂等：同 key 同 query_text 回放第一次结果；query_text 不同返回 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 限流：每分钟 5 次/用户，超限 `429 AI_QUOTA_EXCEEDED`。
- 边界：解析失败保留原文，草稿转 `failed`；客户端可回退手工筛选（旧 `/discovery/search`）。

---

## 2. 查询 AI 搜索草稿

**基本信息**：读取草稿、AST 条件、未知项与冲突；完整 URL `GET /api/v1/ai/search-drafts/{draft_id}`；HTTP Method `GET`；需要登录；成功状态码 `200 OK`。仅本人可读；不存在/非本人统一 `404 SEARCH_DRAFT_NOT_FOUND`。过期草稿仍可读摘要但不能确认。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 1-64 位 hex | 草稿 ID |

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `draft_id` | string | 是 | — | 草稿 ID | `sd_01JXc5...` |
| `status` | string | 是 | — | 草稿状态（见通用说明） | `awaiting_confirmation` |
| `condition_revision` | integer | 是 | `0` 表示未编辑 | 条件乐观锁版本，PATCH/confirm 需携带 | `2` |
| `condition_schema_version` | string | 是 | — | 固定 `search-condition-v1` | `search-condition-v1` |
| `conditions` | array | 是 | 空数组表示解析无结果 | AST 条件列表（见下） | 见下 |
| `unknown` | array | 是 | 空数组表示无未知项 | 未注册/off-allowlist 原文（要求用户澄清） | `["pure_free"]` |
| `conflicts` | array | 是 | 空数组表示无冲突 | 区间倒置冲突描述（age/height/income） | `["age 区间倒置：下限大于上限"]` |
| `expires_at` | string(datetime) | 否 | `null` 未设置 | 草稿过期时间 | `2026-08-08T08:00:00Z` |

`conditions[]` 元素展开：

| 字段 | 类型 | 必返 | 枚举 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `field_key` | string | 是 | 10 字段 allowlist 或未知原文 | 条件字段 | `age` |
| `operator` | string | 是 | `between/gte/lte/eq/in/contains` | 条件操作符 | `between` |
| `value` | any | 是 | 与字段/operator 相关 | 条件取值（如 `{"min":26,"max":32}`） | `{"min":26,"max":32}` |
| `kind` | string | 是 | `hard/soft/rank` | hard 只进硬筛选；soft 只做证据/排序 | `hard` |
| `confidence` | number | 是 | 0..1 | 模型置信度 | `0.99` |
| `source_span` | string | 否 | — | 条件来源原文片段 | `26到32岁` |
| `user_action` | string | 是 | `pending/confirmed/edited/removed` | 用户对条件的动作 | `pending` |

### 返回示例

```json
{
  "draft_id": "sd_01JXc5...",
  "status": "awaiting_confirmation",
  "condition_revision": 0,
  "condition_schema_version": "search-condition-v1",
  "conditions": [
    {"field_key":"age","operator":"between","value":{"min":26,"max":32},"kind":"hard","confidence":0.99,"source_span":"26到32岁","user_action":"pending"},
    {"field_key":"interest_tags","operator":"contains","value":"户外","kind":"soft","confidence":0.78,"source_span":"周末愿意户外","user_action":"pending"}
  ],
  "unknown": [],
  "conflicts": [],
  "expires_at": "2026-08-08T08:00:00Z"
}
```

无数据示例：`{"draft_id":"sd_...","status":"awaiting_confirmation","condition_revision":0,"conditions":[],"unknown":[],"conflicts":[],"expires_at":null}`

---

## 3. 修改 AI 搜索草稿条件

**基本信息**：显式 `confirm/edit/remove` 条件；完整 URL `PATCH /api/v1/ai/search-drafts/{draft_id}`；HTTP Method `PATCH`；需要登录；成功状态码 `200 OK`。返回新 `condition_revision`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 1-64 位 hex | 草稿 ID |
| `expected_condition_revision` | query | integer | 是 | `>=0` | 乐观锁；不匹配返回 `409 DRAFT_VERSION_CONFLICT` |
| `Idempotency-Key` | header | string | 是 | 8-128 位 ASCII | 幂等键 |
| body | body | array | 是 | 至少 1 项 | 逐条件动作列表 |

body 每项（`SearchConditionPatchRequest`）：

| 字段 | 类型 | 必填 | 枚举 | 业务含义 |
| --- | --- | --- | --- | --- |
| `condition_no` | integer | 是 | `>=0` | 条件序号（来自 GET 草稿的 `conditions` 顺序，从 0 起） |
| `action` | string | 是 | `confirm/edit/remove` | `confirm` 确认条件；`edit` 修改 value（置 `edited`，需再次 confirm）；`remove` 删除条件（不可恢复，重解析不会恢复） |
| `value` | any | 否 | 与字段相关 | `edit` 时必填；`confirm/remove` 忽略 |

### 请求体示例

合法（确认 age 并删除 interest_tags）：

```http
PATCH /api/v1/ai/search-drafts/sd_01JXc5...?expected_condition_revision=0 HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: search-patch-20260807-01
Content-Type: application/json

[
  {"condition_no": 0, "action": "confirm"},
  {"condition_no": 3, "action": "remove"}
]
```

非法示例（不存在的 condition_no）：

```http
PATCH /api/v1/ai/search-drafts/sd_01JXc5...?expected_condition_revision=0 HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: search-patch-20260807-02
Content-Type: application/json

[{"condition_no": 99, "action": "confirm"}]
```

响应：`400 AI_INPUT_INVALID`。

### 返回参数

同 §2 查询草稿的返回结构（`condition_revision` 已 +1）。

### 使用方法与业务规则

- 仅 `awaiting_confirmation` 草稿可编辑；`confirmed/expired/failed` 草稿不可编辑（`409 RESULT_STALE`）。
- `remove` 只标记不可见；重解析不恢复已删除条件。
- `confirm` 后草稿仍为 `awaiting_confirmation`，只有**全部**未删除 hard 条件都确认后才可 `POST confirm` 创建快照。
- 幂等：本接口以 `condition_revision + 1` 作为新版本，客户端用返回值重试。

---

## 4. 确认 AI 搜索草稿（创建快照）

**基本信息**：确认草稿并创建不可变 `ai_search_snapshot`，入队 `search_execute` 任务；完整 URL `POST /api/v1/ai/search-drafts/{draft_id}/confirm`；HTTP Method `POST`；需要登录；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `draft_id` | path | string | 是 | 1-64 位 hex | 草稿 ID |
| `expected_condition_revision` | query | integer | 是 | `>=0` | 乐观锁；不匹配返回 `409 DRAFT_VERSION_CONFLICT` |
| `Idempotency-Key` | header | string | 是 | 8-128 位 ASCII | 幂等键；同 key 同摘要回放同一快照与任务 |

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `snapshot_id` | string | 是 | — | 对外快照 ID（hex） | `ss_01JXc5...` |
| `task_id` | string | 是 | — | `search_execute` 任务 ID | `at_01JXc5...` |
| `status` | string | 是 | — | 任务状态（AI-CORE 枚举，入队时 `queued`） | `queued` |
| `stage` | string | 否 | `null` | 真实搜索阶段（见通用说明） | `null` |
| `poll_after_ms` | integer | 是 | — | 建议轮询间隔（毫秒） | `1000` |
| `expires_at` | string(datetime) | 否 | `null` 未设置 | 快照过期时间 | `2026-08-08T08:00:00Z` |
| `condition_schema_version` | string | 是 | — | 固定 `search-condition-v1` | `search-condition-v1` |
| `degraded` | boolean | 是 | — | 响应级降级标志，固定 `false` | `false` |

### 返回示例

```json
{
  "snapshot_id": "ss_01JXc5...",
  "task_id": "at_01JXc5...",
  "status": "queued",
  "stage": null,
  "poll_after_ms": 1000,
  "expires_at": "2026-08-08T08:00:00Z",
  "condition_schema_version": "search-condition-v1",
  "degraded": false
}
```

### 使用方法与业务规则

- 前置：草稿处于 `awaiting_confirmation` 且**全部**未删除 hard 条件已 `confirmed`；存在未确认 hard 条件 → `409 RESULT_STALE`；存在未解决区间冲突 → `409 RESULT_STALE`；已确认条件含 allowlist 外字段 → `422 AI_POLICY_DENIED`；已确认条件 operator 非法 → `400 AI_INPUT_INVALID`。**编译失败不创建候选任务。**
- 幂等：同 key 同摘要回放第一次快照与任务；摘要不同 `409 TASK_IDEMPOTENCY_CONFLICT`。
- 快照不可变：`snapshot_hash`/`policy_revision`/`consent_snapshot`/五维 revision vector 在同一事务写入。

---

## 5. 查询本人可编辑的搜索标签建议

**基本信息**：只读本人已确认且允许搜索的标签（`interest_tags`/`lifestyle_tags`，来源为 `personal_searchable` 特征投影）；完整 URL `GET /api/v1/ai/search-suggestions`；HTTP Method `GET`；需要登录；成功状态码 `200 OK`。不足返回空数组。

### 请求参数

无。

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `items` | array | 是 | 空数组表示无已确认标签/无投影 | 建议标签列表（去重） | `["旅行","看展"]` |
| `page` | object | 是 | — | 分页元信息（见下） | 见下 |

`page` 对象展开：`next_cursor`（string/null，固定 `null`）、`total`（integer，恒 0）、`total_is_estimate`（boolean，恒 `false`）、`has_more`（boolean，恒 `false`）。

### 返回示例

```json
{"items": ["旅行", "看展"], "page": {"next_cursor": null, "total": 0, "total_is_estimate": false, "has_more": false}}
```

无数据示例：`{"items": [], "page": {"next_cursor": null, "total": 0, "total_is_estimate": false, "has_more": false}}`

---

## 6. 查询搜索结果页

**基本信息**：读取快照的 cursor 结果页；完整 URL `GET /api/v1/ai/search-snapshots/{snapshot_id}/results`；HTTP Method `GET`；需要登录；成功状态码 `200 OK`。每次读取重新过可见性门禁（被拉黑/撤回对象被排除）；结果过期标 `stale`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `snapshot_id` | path | string | 是 | 1-64 位 hex | 快照 ID |
| `cursor` | query | string | 否 | 最多 512 字符 | 签名 cursor（来自上一页 `next_cursor`）；不传取第一页 |
| `page_size` | query | integer | 否 | 1..20，默认 20 | 每页条数 |

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `snapshot_id` | string | 是 | — | — | 快照 ID | `ss_01JXc5...` |
| `status` | string | 是 | — | `completed/stale/...` | 结果页状态；快照过期时为 `stale` | `completed` |
| `items` | array | 是 | 空数组表示无可见候选 | 结果项（见下） | 见下 |
| `next_cursor` | string | 否 | `null` 表示没有下一页 | — | 下一页签名 cursor | `eyJzb3J0...` |
| `total` | integer | 是 | `0` 表示无候选 | — | 精确候选总数（与手工 discovery 相同） | `1` |
| `total_is_estimate` | boolean | 是 | — | 固定 `false`（精确 count） | 是否估算 | `false` |
| `degraded` | boolean | 是 | — | 响应级降级标志 | 固定 `false` | `false` |

`items[]` 元素展开：

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- |
| `user_id` | integer | 是 | — | 候选用户 ID | `42` |
| `card` | object | 是 | — | 当前可见卡片字段（只含昵称/头像/年龄/城市/学历/身高/职业/收入/婚姻/兴趣标签） | `{"nickname":"小林",...}` |
| `matched_condition_count` | integer | 是 | `0` 表示只满足 hard | 满足的 hard+soft 条件数（**不得改名** `match_score`/`success_probability`） | `3` |
| `matched_conditions` | array | 是 | — | 命中的条件字段 key | `["age","city_code","education_level"]` |
| `unknown_conditions` | array | 是 | 空数组表示无缺失软字段 | 软字段缺失（`unknown`，不是默认不满足） | `["lifestyle_tags"]` |
| `reason_codes` | array | 是 | — | 证据原因码：`HARD_CONDITION_MATCH`/`SOFT_FIELD_MATCH`/`SOFT_FIELD_UNKNOWN`/`SOFT_FIELD_NO_MATCH` | `["HARD_CONDITION_MATCH","SOFT_FIELD_UNKNOWN"]` |
| `profile_revision` | integer | 是 | `0` 表示无投影 | 候选已确认资料 revision（证据来源版本） | `18` |
| `result_expires_at` | string(datetime) | 是 | — | 单条结果过期时间（默认 10 分钟） | `2026-08-08T08:10:00Z` |

### 返回示例

```json
{
  "snapshot_id": "ss_01JXc5...",
  "status": "completed",
  "items": [
    {
      "user_id": 42,
      "card": {"nickname": "小林", "avatar": "/storage/...", "age": 29, "city_code": "330100"},
      "matched_condition_count": 3,
      "matched_conditions": ["age", "city_code", "education_level"],
      "unknown_conditions": ["lifestyle_tags"],
      "reason_codes": ["HARD_CONDITION_MATCH", "SOFT_FIELD_UNKNOWN"],
      "profile_revision": 18,
      "result_expires_at": "2026-08-08T08:10:00Z"
    }
  ],
  "next_cursor": null,
  "total": 1,
  "total_is_estimate": false,
  "degraded": false
}
```

过期示例（`status="stale"`、`items=[]`、`total=0`）：

```json
{"snapshot_id":"ss_01JXc5...","status":"stale","items":[],"next_cursor":null,"total":0,"total_is_estimate":false,"degraded":false}
```

### 使用方法与业务规则

- 前置：快照存在且未删除；每次读取重新执行可见性门禁，读取期间被拉黑/撤回的候选被排除（不在 `items` 中，但 `total` 是候选查询的精确 count）。
- cursor 必须与当前快照查询绑定：跨查询使用他人 cursor → `400 INVALID_CANDIDATE_CURSOR`。
- 软字段缺失为 `unknown`，不会当作硬失败；`matched_condition_count` 语义固定。
- 结果过期：快照 `expires_at` 过期返回 `stale` 页（HTTP 200）；客户端应重新发起搜索。
- Redis 断开时从 MySQL 恢复，结果仍可读。

---

## 7. 删除 AI 搜索快照

**基本信息**：软删除快照并创建 cleanup 任务；完整 URL `DELETE /api/v1/ai/search-snapshots/{snapshot_id}`；HTTP Method `DELETE`；需要登录；成功状态码 `202 Accepted`。删除后快照立即不可读（`404 SEARCH_SNAPSHOT_NOT_FOUND`）。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- |
| `snapshot_id` | path | string | 是 | 1-64 位 hex | 快照 ID |
| `Idempotency-Key` | header | string | 是 | 8-128 位 ASCII | 幂等键；同 key 回放同一 cleanup 任务 |

### 返回参数

| 字段 | 类型 | 必返 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- |
| `task_id` | string | 是 | cleanup 任务 ID | `at_01JXc5...` |
| `status` | string | 是 | 任务状态（入队时 `queued`） | `queued` |
| `cleanup_requested` | boolean | 是 | 恒 `true` | `true` |

### 返回示例

```json
{"task_id": "at_01JXc5...", "status": "queued", "cleanup_requested": true}
```

### 使用方法与业务规则

- 删除幂等：重复删除（同 key）回放同一 cleanup 任务；快照不可读后重复删除仍返回 `202`。
- 仅本人可删；不存在/非本人统一 `404 SEARCH_SNAPSHOT_NOT_FOUND`。

---

## 8. 状态流转与兼容策略

### 8.1 草稿状态机

```text
parsing → awaiting_confirmation → confirmed（POST confirm 成功后）→ 快照不可变
parsing → failed（解析失败，保留原文，可重试）
awaiting_confirmation → expired（超过 expires_at；过期仍可读摘要但不能确认）
```

未 `confirmed` 的 draft 不能创建候选查询任务（`409 RESULT_STALE`）。PATCH 仅在 `awaiting_confirmation` 可用。

### 8.2 兼容策略

- 旧 `/api/v1/discovery/search`（手工筛选）保持兼容，不改行为。
- 新 AI 搜索失败（`503`/`422`/`400`/`429`）时前端回退手工筛选；快照/结果标记 `invalidated_at` 后物理清理由 cleanup 消费者执行。
- 字段/operator allowlist 固定（10 字段），后续扩展需走产品安全决策并同步更新 `SearchFieldAllowlist` 与本文档。

---

## 9. S-06 语义召回启动条件（Phase 4，本期不实现主链路）

本期搜索结果以 MySQL 硬门禁为主链路（可见性 + hard 参数化筛选），不做语义召回排序。Phase 4 启动语义召回 adapter 前必须全部满足以下条件，并另行评审：

1. **embedding 只用于已确认且允许检索的字段**：向量仅从 `personal_searchable`/`personal_compatibility` 投影（confirmed-only）构建，未确认字段、`ideal_partner_preference`（self_only）和认证字段永不进入向量。
2. **MySQL 硬门禁仍是主链路**：embedding 只影响候选召回阶段的排序/召回率，hard 条件与可见性过滤继续走 `CandidateQueryService` + `CandidateVisibilityService` 的参数化 SQL，模型输出不能成为 SQL/列名/`ORDER BY`/表名。
3. 向量存储、索引与 ANN 检索组件须单独评审（延迟、成本、删除传播、保留期），未批准前 `ai_search_enabled` 保持关闭。
4. 旧 `user_feature_vector.interest_vector` 不作为 embedding 使用；一期不接 ANN（统一方案 §10.4）。
5. 语义召回对 `matched_condition_count`/`reason_codes` 的证据语义不改变；结果仍必须逐条通过可见性门禁。
