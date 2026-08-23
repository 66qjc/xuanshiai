# AI 军师接口

更新时间：2026-08-23

接口前缀：`/api/v1/ai/advisor`。当前为文字情感军师 MVP 后端框架，军师类型固定为 `relationship`。所有接口需要登录；创建、查询和生成建议需要有效会员。AI 只提供建议，不主动发送消息，不作医疗、法律、金融或心理诊断。

## 通用约定

**Headers**：

| Header | 必填 | 说明 | 示例 |
|---|---:|---|---|
| `Authorization` | 是 | Bearer 登录令牌 | `Bearer <token>` |
| `Content-Type` | 有请求体时是 | 固定 `application/json` | `application/json` |
| `Idempotency-Key` | 否 | 当前框架预留，正式幂等存储待产品口径冻结后补齐 | `advisor-20260823-001` |

**通用错误**：

| HTTP 状态码 | 触发条件 | 响应示例 | 前端处理 |
|---|---|---|---|
| `401` | 未登录或令牌失效 | `{"detail":"未认证"}` | 跳转登录 |
| `403` | 非会员、资源不属于当前用户或聊天会话无权访问 | `{"detail":"AI features require an active membership"}` | 展示会员或权限提示 |
| `404` | 军师会话或消息不存在 | `{"detail":"AI advisor session not found"}` | 刷新列表并关闭当前页面 |
| `422` | 参数非法、敏感词或高风险内容被拦截 | `{"detail":"High-risk content cannot be used to generate relationship advice"}` | 提示用户修改内容；不得自动重试 |
| `429` | 当日额度耗尽 | `{"detail":"Daily AI advisor quota exhausted"}` | 展示次日重置或会员权益提示 |
| `503` | AI、Redis或数据库暂时不可用 | `{"detail":"AI advisor service is temporarily unavailable"}` | 保留输入，允许稍后重试 |

---

#### 创建情感军师会话

**基本信息**：创建当前用户的文字情感军师会话。完整 URL 为 `/api/v1/ai/advisor/sessions`，HTTP Method 为 `POST`，需要登录和有效会员，Content-Type 与响应均为 `application/json`，成功状态码为 `201`。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
|---|---|---|---:|---|---|---|---|---|
| `title` | body | string/null | 否 | `null` | 最长 80 字符 | 会话标题；空值使用默认标题 | `聊天回复建议` | 超过 80 字符 |
| `advisor_type` | body | string | 否 | `relationship` | 当前仅允许 `relationship` | 军师类型 | `relationship` | `psychology` |
| `chat_session_id` | body | integer/null | 否 | `null` | 正整数；必须属于当前用户 | 绑定现有聊天会话 | `123` | `0` |

**请求体示例**：

```http
POST /api/v1/ai/advisor/sessions
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "title": "聊天回复建议",
  "advisor_type": "relationship",
  "chat_session_id": 123
}
```

非法示例：`{"advisor_type":"psychology"}`，当前版本返回 `422`。

**返回参数**：

| 字段 | 类型 | 必返 | 空值含义 | 枚举 | 业务含义 | 示例 |
|---|---|---:|---|---|---|---|
| `id` | integer | 是 | 不为空 | 无 | 军师会话 ID | `1` |
| `advisor_type` | string | 是 | 不为空 | `relationship` | 军师类型 | `relationship` |
| `chat_session_id` | integer/null | 是 | 未绑定聊天 | 无 | 关联聊天会话 | `123` |
| `title` | string | 是 | 不为空 | 无 | 会话标题 | `聊天回复建议` |
| `message_count` | integer | 是 | 不为空 | 无 | 当前建议消息数 | `0` |
| `created_at` | datetime | 是 | 不为空 | ISO 8601 | 创建时间 | `2026-08-23T12:00:00` |
| `updated_at` | datetime | 是 | 不为空 | ISO 8601 | 更新时间 | `2026-08-23T12:00:00` |

**返回示例**：

```json
{
  "id": 1,
  "advisor_type": "relationship",
  "chat_session_id": 123,
  "title": "聊天回复建议",
  "message_count": 0,
  "created_at": "2026-08-23T12:00:00",
  "updated_at": "2026-08-23T12:00:00"
}
```

**使用方法与业务规则**：绑定聊天时后端校验当前用户是聊天参与者。AI 不会因为绑定会话而自动读取历史；生成建议时仍需提交 `include_history=true`。创建操作当前未完成持久化幂等，前端提交期间应禁用重复点击。

**错误**：除通用错误外，聊天会话不属于当前用户返回 `403`；参数校验失败返回 `422`。

---

#### 查询情感军师会话

**基本信息**：分页查询当前用户未删除的军师会话。完整 URL 为 `/api/v1/ai/advisor/sessions`，HTTP Method 为 `GET`，需要登录和有效会员，无请求体，成功状态码为 `200`。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
|---|---|---|---:|---|---|---|---|---|
| `page` | query | integer | 否 | `1` | 1～1000 | 页码 | `1` | `0` |
| `page_size` | query | integer | 否 | `20` | 1～50 | 每页数量 | `20` | `100` |

**请求体示例**：无请求体。

```http
GET /api/v1/ai/advisor/sessions?page=1&page_size=20
Authorization: Bearer <token>
```

非法示例：`GET /api/v1/ai/advisor/sessions?page=0`，返回 `422`。

**返回参数**：

| 字段 | 类型 | 必返 | 空值含义 | 业务含义 | 示例 |
|---|---|---:|---|---|---|
| `items` | array | 是 | 无数据为空数组 | 会话数组；内部字段同创建接口返回 | `[]` |
| `page` | integer | 是 | 不为空 | 当前页 | `1` |
| `page_size` | integer | 是 | 不为空 | 每页数量 | `20` |
| `total` | integer | 是 | `0` 表示无数据 | 总数 | `1` |
| `has_more` | boolean | 是 | 不为空 | 是否还有下一页 | `false` |

**返回示例**：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "has_more": false
}
```

**使用方法与业务规则**：只返回当前用户且 `status=1` 的会话，按更新时间倒序排列；软删除会话不会返回。

**错误**：通用 `401`、`403`、`422`、`503`。

---

#### 获取情感军师建议

**基本信息**：根据沟通场景、最新消息和可选聊天历史生成结构化建议。完整 URL 为 `/api/v1/ai/advisor/sessions/{session_id}/advice`，HTTP Method 为 `POST`，需要登录和有效会员，Content-Type 为 `application/json`，成功状态码为 `201`。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
|---|---|---|---:|---|---|---|---|---|
| `session_id` | path | integer | 是 | 无 | 正整数且属于当前用户 | 军师会话 ID | `1` | `0` |
| `scenario` | body | string | 是 | 无 | `opening/reply/topic_extension/rescue/care/compliment/values/intimacy/closing/analyze` | 沟通场景 | `reply` | `breakup` |
| `goal` | body | string/null | 否 | `null` | 最长 300 字符 | 用户希望达到的沟通目标 | `自然延续话题` | 超长文本 |
| `incoming_message` | body | string | 是 | 无 | 1～2000 字符；经过敏感词和高风险检测 | 对方最新消息或待分析内容 | `哈哈，是吗` | 空字符串 |
| `tone` | body | string | 否 | `natural` | `natural/warm/humorous/mature` | 建议语气 | `natural` | `aggressive` |
| `chat_session_id` | body | integer/null | 否 | `null` | 正整数且属于当前用户 | 本次使用的聊天会话 | `123` | `0` |
| `include_history` | body | boolean | 否 | `false` | 为 `true` 时必须能确定 `chat_session_id` | 是否读取最近文本聊天 | `true` | 无会话却设为 `true` |
| `max_suggestions` | body | integer | 否 | `3` | 1～3 | 最大建议数量 | `3` | `4` |

**请求体示例**：

```http
POST /api/v1/ai/advisor/sessions/1/advice
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "scenario": "reply",
  "goal": "自然延续话题",
  "incoming_message": "哈哈，是吗",
  "tone": "natural",
  "chat_session_id": 123,
  "include_history": true,
  "max_suggestions": 3
}
```

非法示例：`{"scenario":"reply","incoming_message":"","max_suggestions":4}`，返回 `422`。

**返回参数**：

| 字段 | 类型 | 必返 | 空值含义 | 枚举 | 业务含义 | 示例 |
|---|---|---:|---|---|---|---|
| `id` | integer | 是 | 不为空 | 无 | 建议消息 ID | `10` |
| `session_id` | integer | 是 | 不为空 | 无 | 军师会话 ID | `1` |
| `scenario` | string | 是 | 不为空 | 同请求场景枚举 | 实际场景 | `reply` |
| `analysis` | string | 是 | 不为空 | 无 | 简短沟通分析 | `可以先承接再延展` |
| `suggestions` | array | 是 | 至少一条安全降级建议 | 无 | 候选话术数组 | 见示例 |
| `suggestions[].content` | string | 是 | 不为空 | 无 | 可复制话术，最多 500 字符 | `听起来挺有意思的...` |
| `suggestions[].style` | string | 是 | 不为空 | `natural/warm/humorous/mature` | 话术风格 | `natural` |
| `suggestions[].reason` | string | 是 | 不为空 | 无 | 使用理由，最多 300 字符 | `先承接再提问` |
| `risk_level` | string | 是 | 不为空 | `none/low/medium/high` | 输出风险等级 | `none` |
| `risk_notice` | string/null | 是 | 无风险时为 `null` | 无 | 风险提示 | `null` |
| `next_step` | string/null | 是 | 没有建议时可为空 | 无 | 下一步沟通建议 | `观察对方是否愿意展开` |
| `disclaimer` | string | 是 | 不为空 | 无 | AI 使用声明 | `以上建议仅供参考...` |
| `created_at` | datetime | 是 | 不为空 | ISO 8601 | 建议创建时间 | `2026-08-23T12:05:00` |

**返回示例**：

```json
{
  "id": 10,
  "session_id": 1,
  "scenario": "reply",
  "analysis": "对方回复较短但没有明显拒绝，可以先承接再轻松延展。",
  "suggestions": [
    {
      "content": "听起来还挺有意思的，可以再和我说说吗？",
      "style": "natural",
      "reason": "先承接当前内容，只使用一个开放式问题。"
    }
  ],
  "risk_level": "none",
  "risk_notice": null,
  "next_step": "如果对方仍持续简短回复，建议降低频率并给予空间。",
  "disclaimer": "以上建议仅供参考，请根据真实感受沟通，不要连续打扰对方。",
  "created_at": "2026-08-23T12:05:00"
}
```

**使用方法与业务规则**：后端只读取指定聊天会话的文本消息，最多读取配置项 `AI_ADVISOR_MAX_CONTEXT_MESSAGES` 条，并排除撤回消息。请求通过本地敏感词检测后扣减每日额度；Provider、格式校验、输出审核或数据库保存失败时退还额度。AI 不调用聊天发送接口。重新生成属于新调用并再次消耗额度。当前持久化幂等框架待产品口径冻结后补齐。

**错误**：`404` 会话不存在；`403` 聊天会话越权；`422` 缺少历史会话、敏感词、高风险输入或高风险输出；`429` 额度耗尽；`503` AI、Redis或数据库失败。

---

#### 删除情感军师会话

**基本信息**：软删除当前用户的军师会话。完整 URL 为 `/api/v1/ai/advisor/sessions/{session_id}`，HTTP Method 为 `DELETE`，需要登录，成功状态码为 `204`。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
|---|---|---|---:|---|---|---|---|---|
| `session_id` | path | integer | 是 | 无 | 正整数且属于当前用户 | 待删除会话 | `1` | `0` |

**请求体示例**：无请求体。

```http
DELETE /api/v1/ai/advisor/sessions/1
Authorization: Bearer <token>
```

非法示例：`DELETE /api/v1/ai/advisor/sessions/0`，返回 `422`。

**返回参数**：无返回体。

**返回示例**：HTTP `204 No Content`。

**使用方法与业务规则**：会话设置为不可用并写入删除时间，关联消息标记为 `deleted`；当前框架不立即物理删除审计数据，正式保留期和定时清理策略待产品与隐私口径冻结。

**错误**：`404` 会话不存在或已删除；`401` 未登录；`503` 数据库失败。

---

#### 反馈情感军师建议

**基本信息**：记录当前用户对本人建议的反馈。完整 URL 为 `/api/v1/ai/advisor/messages/{message_id}/feedback`，HTTP Method 为 `POST`，需要登录，Content-Type 为 `application/json`，成功状态码为 `200`。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 校验规则 | 业务含义 | 合法示例 | 非法示例 |
|---|---|---|---:|---|---|---|---|---|
| `message_id` | path | integer | 是 | 无 | 正整数且消息属于当前用户 | 建议消息 ID | `10` | `0` |
| `feedback_type` | body | string | 是 | 无 | `copied/used/not_useful/reported` | 复制、采用、无用或举报 | `copied` | `liked` |

**请求体示例**：

```http
POST /api/v1/ai/advisor/messages/10/feedback
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{"feedback_type":"copied"}
```

非法示例：`{"feedback_type":"liked"}`，返回 `422`。

**返回参数**：

| 字段 | 类型 | 必返 | 空值含义 | 枚举 | 业务含义 | 示例 |
|---|---|---:|---|---|---|---|
| `message_id` | integer | 是 | 不为空 | 无 | 建议消息 ID | `10` |
| `feedback_type` | string | 是 | 不为空 | `copied/used/not_useful/reported` | 反馈类型 | `copied` |
| `recorded` | boolean | 是 | 不为空 | 无 | `false` 表示重复反馈未新增 | `true` |

**返回示例**：

```json
{"message_id":10,"feedback_type":"copied","recorded":true}
```

**使用方法与业务规则**：只能反馈当前用户自己的成功建议。同一用户对同一消息当前只保存一条反馈；重复提交返回 `recorded=false`。产品口径冻结后可决定是否允许修改反馈。

**错误**：`404` 消息不存在或不属于当前用户；`422` 枚举非法；`503` 数据库保存失败。

## 配置

```env
AI_ENABLED=false
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=YOUR_AI_API_KEY
AI_MODEL=deepseek-chat
AI_DAILY_ADVISOR_LIMIT=20
AI_ADVISOR_MAX_CONTEXT_MESSAGES=80
AI_ADVISOR_PROMPT_VERSION=relationship-v1
AI_ADVISOR_KNOWLEDGE_VERSION=seed-v1
```

`AI_ENABLED=false` 时开发/测试使用确定性 Mock。真实 Key 不得提交到 Git。

## 当前框架限制

- 产品口径尚未冻结，会员额度、保留期和人工转接策略使用临时默认值。
- 已建立知识库表和内置回退话术，但尚未批量导入 `情感话术.docx` 的 80～150 条种子数据。
- 已预留请求 ID、Prompt 版本、知识版本、额度消费和退款字段；独立的持久化幂等表与完整调用日志聚合后续补齐。
- 第三方敏感词审核继续复用现有适配器配置；未配置时以本地词库为主。

## 变更记录

- 2026-08-23：新增 AI 情感军师后端框架、五个接口、数据库表、Mock、风险校验、额度退款和基础测试。
