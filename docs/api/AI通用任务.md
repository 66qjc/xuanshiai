# AI 通用任务接口（查询与取消）

接口前缀：`/api/v1`。本文件是 AI 画像、搜索与匹配度后台通用任务（`ai_task` 事实源，统一方案 §6.4 / §11.3）对外的查询与取消契约。业务任务由 Task 7+ 各自接口创建；本文件的通用接口只负责「查看任务状态与安全结果引用」和「在可取消窗口内取消任务」。

### 变更记录

- 2026-08-08：新增 `GET /api/v1/ai/tasks/{task_id}` 与 `POST /api/v1/ai/tasks/{task_id}/cancel`，错误统一为 `AiErrorDetail` 形状（含 `request_id`），普通响应不携带 provider trace、原文或密钥。

通用请求头：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
X-Request-ID: req_01J...      # 可选，1-128 位 [A-Za-z0-9._:-]，用于日志与错误关联
```

通用说明：

- 两个接口都需要登录；任务详情只允许任务 owner（管理员端不纳入本期 C 端范围，独立审计）。
- 任务不存在或不属于当前用户统一返回 `404 TASK_NOT_FOUND`，不泄露任务归属。
- 错误响应统一为：

```json
{
  "detail": {
    "code": "TASK_NOT_FOUND",
    "message": "任务不存在",
    "request_id": "req_01J...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

---

## 1. 查询 AI 通用任务

**基本信息**：查询单个 AI 通用任务的状态、真实阶段、轮询提示和安全结果引用；完整 URL `GET /api/v1/ai/tasks/{task_id}`；HTTP Method `GET`；需要登录（Bearer Token）；所需权限：任务 owner（本人）；请求 `Content-Type`：无请求体；响应 `Content-Type`：`application/json`；成功状态码 `200 OK`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外任务 ID，由任务创建接口返回 |

### 请求体示例

无请求体。

合法 URL 示例：

```text
GET /api/v1/ai/tasks/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d
Authorization: Bearer <access_token>
X-Request-ID: req_01JXc5...
```

非法示例（task_id 超长或包含空格的请求会被路径校验拒绝）：

```text
GET /api/v1/ai/tasks/%20not%20a%20valid%20task%20id%20that%20is%20way%20too%20long%20to%20be%20legal%20
Authorization: Bearer <access_token>
```

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | string | 是 | — | — | 对外任务 ID | `3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d` |
| `status` | string | 是 | — | `queued/leased/running/retry_wait/succeeded/failed/cancelled/superseded` | 任务状态机当前状态 | `queued` |
| `stage` | string | 否 | `null` 表示尚无任务类型允许的真实阶段 | 任务类型各自的阶段枚举（如 M03 搜索的 `validating/filtering/...`） | 真实执行阶段，不是状态别名 | `completed` |
| `poll_after_ms` | integer | 是 | — | `>=0` | 客户端下次轮询建议间隔毫秒；非进行态为 `0` | `1000` |
| `expires_at` | string(datetime) | 否 | `null` 表示无租约/无过期时间 | — | 租约过期时间（进行态）；UTC ISO-8601 | `2026-08-08T08:10:00Z` |
| `result_ref` | string | 否 | `null` 表示尚无结果 | — | 成功后的安全结果引用（ID 引用，不是原文内容） | `res:profile-extract-1` |
| `error_code` | string | 否 | `null` 表示无错误 | 稳定错误码（§3） | 任务失败/重试的稳定业务错误码 | `AI_INPUT_INVALID` |
| `error_message` | string | 否 | `null` 表示无错误 | — | 安全文案，不包含 provider trace、原文或密钥 | `provider 输出未通过 Schema 校验` |

### 返回示例

进行中任务（200）：

```json
{
  "task_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "status": "running",
  "stage": "extracting",
  "poll_after_ms": 1000,
  "expires_at": "2026-08-08T08:10:00Z",
  "result_ref": null,
  "error_code": null,
  "error_message": null
}
```

已完成任务（200）：

```json
{
  "task_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "status": "succeeded",
  "stage": "completed",
  "poll_after_ms": 0,
  "expires_at": null,
  "result_ref": "res:profile-extract-1",
  "error_code": null,
  "error_message": null
}
```

### 使用方法与业务规则

- 前置条件：已登录；任务存在且属于当前用户。
- 调用顺序：任何 AI 写接口返回 `202`（含 `task_id`）后，客户端通过本接口轮询恢复；不依赖本地计时器。
- 幂等与防重：`GET` 幂等，可重复调用。
- 频率/额度/次数限制：无独立额度；需遵守登录与全局限流。
- 状态流转：状态只能由后台 Worker/取消接口改变，本接口只读；`queued → leased → running → succeeded`；`running → retry_wait → leased`；可取消窗口转 `cancelled`；版本/授权失效 `running → superseded`。
- 边界场景：
  - 任务不存在或非本人：`404 TASK_NOT_FOUND`（不区分两种场景，避免泄露归属）。
  - `superseded/failed/cancelled`：`poll_after_ms=0`，`expires_at=null`，客户端应停止轮询。
  - 进行态且租约过期未回收：状态仍为 `running` 短暂可见，后台 reaper 会转为 `retry_wait`。
- 前端处理建议：`status` 为终态（`succeeded/failed/cancelled/superseded`）时停止轮询；`result_ref` 为 `null` 时不要展示不存在的业务结果。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 404 | `TASK_NOT_FOUND` | 任务不存在或非本人 | false | 回到任务来源页；不提示具体原因 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |

错误响应 JSON：

```json
{
  "detail": {
    "code": "TASK_NOT_FOUND",
    "message": "任务不存在",
    "request_id": "req_01JXc5...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

---

## 2. 取消 AI 通用任务

**基本信息**：在可取消窗口（`queued/leased/running/retry_wait`）内请求取消任务；完整 URL `POST /api/v1/ai/tasks/{task_id}/cancel`；HTTP Method `POST`；需要登录（Bearer Token）；所需权限：任务 owner（本人）；请求 `Content-Type`：`application/json`；响应 `Content-Type`：`application/json`；成功状态码 `202 Accepted`。

### 请求参数

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | path | string | 是 | 无 | 1-64 位可见字符 | 对外任务 ID，由任务创建接口返回 |

### 请求体示例

无请求体。

合法 URL 示例：

```text
POST /api/v1/ai/tasks/3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d/cancel
Authorization: Bearer <access_token>
X-Request-ID: req_01JXc5...
```

### 返回参数

| 字段 | 类型 | 必返 | 空值含义 | 枚举含义 | 业务含义 | 示例值 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | string | 是 | — | — | 被取消任务 ID | `3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d` |
| `status` | string | 是 | — | 固定为 `cancelled` | 取消后的终态 | `cancelled` |
| `cancel_requested` | boolean | 是 | — | 固定为 `true` | 取消受理标记 | `true` |

### 返回示例

成功（202）：

```json
{
  "task_id": "3f2a9c0e1b4d4a5b8c7d6e5f4a3b2c1d",
  "status": "cancelled",
  "cancel_requested": true
}
```

### 使用方法与业务规则

- 前置条件：已登录；任务存在且属于当前用户。
- 调用顺序：通常在任务创建后、终态前调用；取消成功后任务不可再恢复。
- 幂等与防重：取消是单向终态操作；重复调用同 key 任务，第二次返回 `409 TASK_NOT_CANCELLABLE`。
- 频率/额度/次数限制：无独立额度；需遵守登录与全局限流。
- 状态流转：`queued/leased/running/retry_wait → cancelled`；`succeeded/failed/cancelled/superseded` 不可取消。
- 边界场景：
  - 任务不存在或非本人：`404 TASK_NOT_FOUND`。
  - 已完成/不可取消阶段：`409 TASK_NOT_CANCELLABLE`。
  - 取消与 Worker 完成并发：按行锁与条件更新裁决，若恰好已完成则返回 `409 TASK_NOT_CANCELLABLE`，不会出现「已取消但结果已写」的中间态泄漏。

### 错误

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
| --- | --- | --- | --- | --- |
| 404 | `TASK_NOT_FOUND` | 任务不存在或非本人 | false | 回到任务来源页；不提示具体原因 |
| 409 | `TASK_NOT_CANCELLABLE` | 任务已完成或处于不可取消阶段 | false | 展示"任务已处理完成"，跳转到结果页 |
| 401 | — | 未登录或 Token 失效 | false | 引导重新登录 |

错误响应 JSON（409）：

```json
{
  "detail": {
    "code": "TASK_NOT_CANCELLABLE",
    "message": "任务已处于不可取消状态",
    "request_id": "req_01JXc5...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

---

## 3. 稳定错误码

本文件接口使用以下稳定错误码（统一方案 §11.2，执行计划 §3.2）：

| 业务码 | HTTP | retryable | 固定语义 |
| --- | --- | --- | --- |
| `TASK_NOT_FOUND` | 404 | false | 任务不存在或不属于当前用户 |
| `TASK_IDEMPOTENCY_CONFLICT` | 409 | false | 同幂等键但请求摘要不同（任务创建接口） |
| `TASK_NOT_CANCELLABLE` | 409 | false | 任务已完成或处于不可取消阶段 |
| `AI_INPUT_INVALID` | 400 | false | 类型、长度、枚举、范围或 JSON 形状非法（写回任务的错误码） |
| `AI_POLICY_DENIED` | 422 | false | 越权字段、敏感推断、认证伪造、联系方式或承诺文案（写回任务的错误码） |
| `AI_QUOTA_EXCEEDED` | 429 | true | 用户或系统额度耗尽（可重试，写入 `retry_wait`） |
| `AI_TEMPORARILY_UNAVAILABLE` | 503 | true | Provider、Worker 或通知基础设施的临时失败（可重试，写入 `retry_wait`） |
| `RESULT_STALE` | 409 | true | 输入、隐私、关系或策略版本已变化（任务转 `superseded`） |
| `AI_FEATURE_DISABLED` | 503 | false | 功能开关、合规、保留期或 Provider 批准门禁未满足（任务创建前） |
| `AI_CONSENT_REQUIRED` | 403 | false | scope 未授权或已撤回；不得创建/继续任务 |

`retryable` 是后台任务的重试语义，客户端不得据此推断资源是否存在。

---

## 4. 兼容性说明

- 本文件接口为 2026-08-08 新增路径，不修改任何旧接口。
- 新增 `result_ref`/`error_code`/`error_message` 字段对旧客户端向后兼容；`TaskPollState` 必需字段（`task_id/status/stage/poll_after_ms/expires_at`）在任务创建接口的 202 响应中已冻结，本接口保持一致。
- 若未来加入管理员审计端，任务详情需独立路由与独立审计，不并入 C 端接口。
