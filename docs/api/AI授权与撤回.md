# AI 授权与撤回

> **版本：** 1.0（2026-08-16，Task 3 / G2-A）
> **契约来源：** 统一实施方案 §6.3、§11.2；`app/services/ai/consents.py`

## 基本信息

| 项 | 值 |
|---|---|
| 前缀 | `/api/v1/ai` |
| 认证 | `Authorization: Bearer <access_token>` |
| Content-Type | `application/json` |
| scope 枚举 | `profile_text_extract`、`search_parse`、`compatibility_shadow` |
| policy_revision | `ai-policy-2026-08-07-v1`（当前冻结值） |

### per-scope consent_version 注册表

每个 scope 有独立的冻结 consent_version，客户端必须提交匹配的值：

| scope | consent_version | 用途 |
|---|---|---|
| `profile_text_extract` | `profile-text-v1` | M04 画像文字抽取 |
| `search_parse` | `search-parse-v1` | M03 搜索条件解析 |
| `compatibility_shadow` | `compatibility-shadow-v1` | M06 兼容度 shadow 计算 |

---

## 1. 查询当前授权

**`GET /api/v1/ai/consents`**

需要登录。返回当前用户所有活跃（未撤回）授权。

### 返回参数

| 字段 | 类型 | 必返 | 含义 |
|---|---|---|---|
| `consents` | array | 是 | 活跃授权列表 |
| `consents[].scope` | string | 是 | 授权 scope |
| `consents[].version` | string | 是 | 授权时的 consent_version |
| `consents[].policy_revision` | string | 是 | 授权时的 policy_revision |
| `consents[].granted_at` | datetime | 是 | 授予时间（UTC ISO 8601） |
| `privacy_revision` | integer | 是 | 当前 privacy revision |

### 成功响应示例

```json
{
  "consents": [
    {
      "scope": "profile_text_extract",
      "version": "profile-text-v1",
      "policy_revision": "ai-policy-2026-08-07-v1",
      "granted_at": "2026-08-16T10:00:00"
    }
  ],
  "privacy_revision": 3
}
```

无活跃授权时返回 `{"consents": [], "privacy_revision": 0}`。

---

## 2. 授予授权

**`PUT /api/v1/ai/consents/{scope}`**

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 校验规则 | 含义 |
|---|---|---|---|---|---|
| `scope` | path | string | 是 | 枚举（见上表） | 授权 scope |
| `Idempotency-Key` | header | string | 是 | 8-128 位 ASCII `[A-Za-z0-9._:-]` | 幂等键 |
| `X-Expected-Privacy-Revision` | header | integer | 是 | ≥ 0 | 当前 privacy revision |
| `consent_version` | body | string | 是 | 必须匹配 per-scope 冻结值 | 授权文案版本 |
| `policy_revision` | body | string | 是 | 必须匹配 `ai-policy-2026-08-07-v1` | 策略版本 |

### 请求示例

```http
PUT /api/v1/ai/consents/profile_text_extract HTTP/1.1
Authorization: Bearer <access_token>
Idempotency-Key: consent-grant-20260816-01
X-Expected-Privacy-Revision: 0
Content-Type: application/json

{"consent_version": "profile-text-v1", "policy_revision": "ai-policy-2026-08-07-v1"}
```

### 成功响应（200）

```json
{
  "operation_id": "abc123...",
  "scope": "profile_text_extract",
  "operation": "grant",
  "status": "active",
  "consent": {
    "scope": "profile_text_extract",
    "version": "profile-text-v1",
    "policy_revision": "ai-policy-2026-08-07-v1",
    "granted_at": "2026-08-16T10:00:00"
  },
  "cleanup_task_id": null,
  "privacy_revision": 1
}
```

### 幂等性

- 同一 `Idempotency-Key` + 相同 payload 重复请求：返回第一次的结果（`200`）。
- 同一 `Idempotency-Key` + 不同 payload：返回 `409 AI_CONSENT_IDEMPOTENCY_CONFLICT`。

---

## 3. 撤回授权

**`DELETE /api/v1/ai/consents/{scope}`**

### 请求参数

| 参数 | 位置 | 类型 | 必填 | 含义 |
|---|---|---|---|---|
| `scope` | path | string | 是 | 授权 scope |
| `Idempotency-Key` | header | string | 是 | 幂等键 |
| `X-Expected-Privacy-Revision` | header | integer | 是 | 当前 privacy revision |

### 成功响应（202）

```json
{
  "operation_id": "def456...",
  "scope": "profile_text_extract",
  "operation": "revoke",
  "status": "revoked",
  "consent": null,
  "cleanup_task_id": "task_01J...",
  "privacy_revision": 2
}
```

撤回响应返回前，该 scope 下所有草稿、快照、结果和投影同步不可读。异步 cleanup task 负责物理清理（开发/测试环境 15 分钟内）。

### 撤回传播范围

| scope | 同步不可读范围 |
|---|---|
| `profile_text_extract` | 画像草稿/会话、特征投影、搜索结果、兼容度快照 |
| `search_parse` | 搜索草稿、搜索快照、搜索结果 |
| `compatibility_shadow` | 兼容度快照 |

---

## 4. 错误码

| HTTP | 业务码 | 触发条件 | retryable | 前端处理建议 |
|---|---|---|---|---|
| 400 | `AI_INPUT_INVALID` | scope 非法、Idempotency-Key 格式不合法 | false | 定位错误字段，不重试 |
| 409 | `AI_CONSENT_VERSION_CONFLICT` | consent_version 或 policy_revision 不匹配冻结值；privacy revision 过期 | false | 重新拉取 privacy revision，使用正确版本重试 |
| 409 | `AI_CONSENT_IDEMPOTENCY_CONFLICT` | 同一 Idempotency-Key 用于不同请求 | false | 提示操作冲突，使用新 key 重试 |
| 503 | `AI_FEATURE_DISABLED` | 生产 AI 开关、合规、保留期或 Provider 未批准 | false | 展示稳定禁用状态 |

### 错误响应示例

```json
{
  "detail": {
    "code": "AI_CONSENT_VERSION_CONFLICT",
    "message": "consent_version does not match the frozen value (profile-text-v1)",
    "request_id": "req_01J...",
    "retryable": false,
    "retry_after_ms": 0
  }
}
```

---

## 5. 使用方法与业务规则

### 前置条件
- 用户已登录且手机号已验证。
- grant 前必须先 `GET /consents` 获取当前 `privacy_revision`，以此作为 `X-Expected-Privacy-Revision`。

### 调用顺序
1. `GET /consents` → 获取 `privacy_revision`
2. `PUT /consents/{scope}` → 授予（使用对应 scope 的 `consent_version`）
3. 使用 AI 功能（画像/搜索/匹配度）
4. `DELETE /consents/{scope}` → 撤回（响应返回前数据不可读）

### 幂等与防重
- grant 和 revoke 都支持 `Idempotency-Key` 幂等。
- 同 key + 同 payload 返回第一次结果；同 key + 不同 payload 返回 `409`。
- grant 同一 (user, scope) 下已有活跃授权时，先撤旧再授新（原子操作）。

### 状态流转
- grant：`无授权 → active`
- revoke：`active → revoked`（不可逆，需重新 grant）
- revoke 后排队中的任务在下一安全点转为 `cancelled`/`superseded`，不再调用 Provider。

### 边界场景
- 已撤回的 scope 再次 revoke：返回 `status=already_revoked`，privacy revision 不变。
- privacy revision 过期：返回 `409 AI_CONSENT_VERSION_CONFLICT`，前端重新拉取后重试。
- 并发 grant/revoke：`FOR UPDATE` 锁定 `user_revision_state`，只允许一个提交。
