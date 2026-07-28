# 管理员审核接口

## 1. 通用约定

接口前缀：`/api/v1`。所有接口要求：

1. 当前请求携带有效登录 Token。
2. 当前用户在 `user_role` 表中存在 `role_code=admin` 且 `status=1` 的有效管理员角色。

请求头：

```http
Authorization: Bearer <admin_access_token>
Content-Type: application/json
```

管理接口不接受前端传入的用户 ID 作为操作者身份，服务端从 Token 解析当前用户并查询管理员角色。成功响应没有统一 `data` 包装层。错误响应统一为：

```json
{"detail":"错误原因"}
```

当前管理模块提供媒体审核、举报列表/处理、社区内容审核队列、社区内容下架恢复，以及红娘牵线服务申请管理接口。审核任务和内容处置会写入 `business_audit_log`。

## 2.1 社区审核队列

### `GET /api/v1/admin/community/moderation-items`

需要有效管理员 Token。查询参数：`page`（默认 1）、`page_size`（1~100，默认 20）、`status`（默认 `pending`，可选 `pending/approved/rejected/replaced/deleted/hidden`）、`target_type`（可选 `post/comment/paper_plane/paper_plane_reply/paper_plane_message/media`）。返回 `items/page/page_size/total/has_more`。每个 item 包含 `id`、`target_type`、`target_id`、`user_id`、`status`、`risk_level`、`matched_words`、`raw_content`、`display_content`、`reason`、`created_at`、`expires_at`。

### `PATCH /api/v1/admin/community/moderation-items/{task_id}/review`

请求体：

```json
{"action":"approve","reason":"审核通过"}
```

`action` 可选 `approve/reject/replace/delete/hide`；`replace` 必须额外提供 `display_content`。只有 `pending` 任务允许处理，已完成任务返回 `409`。管理员处理后写入审核人、时间、前后状态、理由和业务审计日志，并向发布者发送站内通知。审核原文及命中词保存 1 年。

审核结果：`approved` 发布，`rejected` 拒绝，`replaced` 使用管理员指定展示文本，`deleted` 删除，`hidden` 下架。删除和下架不可互相覆盖。

## 2.2 管理员授权

### `POST /api/v1/admin/users/grant`

只有有效管理员可以调用。请求体为 `{"user_id":23,"permissions":["community:review","media:review"]}`。服务端为目标用户授予 `admin` 角色并覆盖其权限清单；目标用户必须存在且可用。权限记录保存在 `admin_permission`，当前接口为后续细粒度权限校验提供管理入口。

## 2. 媒体审核

### `PATCH /api/v1/admin/media/{media_id}/review`

用途：审核用户头像、背景墙、相册图片或个人视频。成功状态：`200 OK`。

路径参数：

| 参数 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `media_id` | path | integer | 是 | `>=1` | `user_media.id` |

请求体：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 枚举/规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `status` | body | integer | 是 | 无 | `1` 通过，`2` 拒绝，`3` 隐藏 | 审核后媒体状态 |
| `reason` | body | string/null | 否 | `null` | 最长 255 字符 | 拒绝或隐藏原因，也可用于通过备注 |

请求示例：

```http
PATCH /api/v1/admin/media/501/review
Authorization: Bearer <admin_access_token>
Content-Type: application/json
```

```json
{"status":2,"reason":"图片不是本人近照"}
```

成功返回：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `media_id` | integer | 是 | 不为空 | 被审核媒体 ID |
| `user_id` | integer | 是 | 不为空 | 媒体所属用户 ID |
| `status` | integer | 是 | `1/2/3` | 更新后的审核状态 |
| `reason` | string/null | 是 | 未填写原因时 `null` | 审核原因 |

通过示例：

```json
{"media_id":501,"user_id":23,"status":1,"reason":"审核通过"}
```

拒绝示例：

```json
{"media_id":501,"user_id":23,"status":2,"reason":"图片不是本人近照"}
```

媒体不存在、已软删除或 `media_id` 无效时返回：

```json
{"detail":"媒体不存在"}
```

审核完成后服务端重新计算所属用户资料完整度。媒体初始审核状态由上传接口写入 `0`（待审核）；推荐和公开资料查询会排除存在待审核、拒绝或隐藏媒体的用户。重复审核是覆盖操作，以最后一次有效请求为准，不提供幂等键和审核版本号。

## 3. 举报处理

### `GET /api/v1/admin/reports`

用途：分页查看举报队列。成功状态 `200 OK`。

查询参数：

| 参数 | 类型 | 必填 | 默认 | 含义 |
| --- | --- | --- | --- | --- |
| `page` | integer | 否 | 1 | 页码 |
| `page_size` | integer | 否 | 20 | 1~100 |
| `status` | integer/null | 否 | null | `0` 待处理 / `1` 已处理 / `2` 驳回 |
| `target_type` | string/null | 否 | null | `user` / `post` / `comment` / `paper_plane` |

返回：`items[]`（含 `reporter_user_id`、`target_user_id`、`target_type`、`target_id`、`type`、`description`、`status`、`result`、时间字段）、`page`、`page_size`、`total`、`has_more`。

### `PATCH /api/v1/admin/reports/{report_id}/review`

用途：处理用户/内容举报记录。成功状态：`200 OK`。

路径参数：

| 参数 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `report_id` | path | integer | 是 | `>=1` | `user_report.id` |

请求体：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 枚举/规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `status` | body | integer | 是 | 无 | `1` 已处理，`2` 驳回 | 举报处理结果状态 |
| `result` | body | string | 是 | 无 | 1~255 字符 | 审核结论或处理措施 |
| `action` | body | string | 否 | `none` | `none` / `hide_content` / `restore_content` / `dismiss` | 显式副作用；默认只改工单 |

- `none`：仅更新 `status`/`result`（兼容旧客户端）
- `hide_content` / `restore_content`：必须搭配 `status=1`，且仅当 `target_type` 为内容时下架/恢复；用户举报或矛盾状态返回 `422`
- `dismiss`：必须搭配 `status=2`，不动内容；矛盾状态返回 `422`
- **不会**自动封禁账号；完成审核后会向举报人发送系统通知，举报成立时也会通知被举报人

请求示例：

```json
{"status":1,"result":"确认违规并下架","action":"hide_content"}
```

成功返回：`report_id`、`status`、`result`、`action`、`content_moderated`。

### 社区内容下架 / 恢复

统一请求体：`{"status":1|2,"reason":"..."}`，其中 `1` 恢复可见，`2` 下架隐藏。

| 方法 | 路径 | 后端效果 |
| --- | --- | --- |
| `PATCH` | `/api/v1/admin/community/posts/{post_id}/moderation` | 动态 `moderation_status`：下架=2，恢复=1；不改用户删除 `deleted_at` |
| `PATCH` | `/api/v1/admin/community/comments/{comment_id}/moderation` | 评论 `moderation_status`：下架=2，恢复=1；不改用户删除 `deleted_at` |
| `PATCH` | `/api/v1/admin/community/paper-planes/{plane_id}/moderation` | 纸飞机 `moderation_status`：下架=2，恢复=1（不动 lifecycle `status`） |

成功返回：`target_type`、`target_id`、`status`、`reason`。不存在 → `404`。

红娘申请审核通知不属于本举报接口，见 `docs/api/identity.md`。

## 4. 权限和错误响应

| HTTP | 触发条件 | 示例 detail | 前端处理 |
| --- | --- | --- | --- |
| `401` | 未携带 Token、Token 无效或会话过期 | `请先登录` | 清理 Token 并重新登录 |
| `403` | 当前用户不是有效管理员 | `需要管理员权限` | 禁止显示管理页面或提示无权限 |
| `404` | 媒体/举报记录不存在或已软删除 | `媒体不存在` | 刷新待审核列表 |
| `422` | ID、状态、文本长度或类型不合法 | `Input should be 1, 2 or 3` | 修正请求参数 |
| `500` | 数据库或服务端异常 | 不向客户端暴露内部 SQL | 记录 request id 后重试或报警 |

错误示例：

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json
```

```json
{"detail":"需要管理员权限"}
```

## 5. 状态、并发、审计和兼容性

- 媒体状态：`0` 待审核，`1` 通过，`2` 拒绝，`3` 隐藏。管理员请求只允许写入 `1/2/3`。
- 举报状态：`0` 待处理，`1` 已处理，`2` 已驳回。管理员请求只允许写入 `1/2`。
- 媒体审核在读取记录时使用行锁，避免同一媒体同时被两个请求读取后覆盖；当前没有审核人、审核批次和审计日志字段。
- 举报处理在读取记录时使用行锁；`status=1/2` 均为不可覆盖终态，重复处理返回 `409`。
- 本模块复用已有 `get_current_admin`、Pydantic Schema、SQLAlchemy AsyncSession 和资料/举报服务，不重复实现认证和数据库连接。
- 新增审核列表、批量操作、操作日志或自动处罚前，必须先定义分页、权限、状态流转、审计字段和幂等方案。

## 6. 变更记录

### 2026-07-20

- 补充管理员身份校验、路径参数、请求字段和响应字段契约。
- 明确媒体和举报状态枚举、空值含义、错误示例与当前副作用。
- 明确媒体接口与举报接口的现有边界；举报审核已具备结果通知与内容处置审计，但仍没有批量审核或自动封禁能力。

### 2026-07-22

### 2026-07-24

- 新增 `PATCH /api/v1/admin/users/{user_id}/certifications/{kind}/review`，用于审核学历、房产和婚姻认证；`kind` 为 `education`、`house` 或 `marriage`，状态 `2` 为通过、`3` 为失败。

- 新增红娘牵线服务申请查询、分配和处理接口，详见 `docs/api/matchmaker.md`。
- 红娘申请审核结果新增申请人站内通知，详见 `docs/api/identity.md`。

### 2026-07-28（举报终态与独立申诉复审）

#### `GET /api/v1/admin/reports/{report_id}`

路径参数 `report_id>=1`，无请求体。返回举报双方、目标、描述、`status`、`result`、`action`、`reviewed_by`、`reviewed_at` 与时间字段；不存在返回 `404`。

#### `GET /api/v1/admin/report-appeals`

Query：`page>=1`（默认 1）、`page_size=1..100`（默认 20）、`status` 可选 `0|1|2`。返回 `{items,page,page_size,total,has_more}`；每项包含申诉、原举报目标和 `original_reviewer_id`，便于前端禁用同审核人复审。

#### `PATCH /api/v1/admin/report-appeals/{appeal_id}/review`

请求体：

```json
{"status":1,"result":"复核后确认内容未违规"}
```

`status=1` 申诉通过，`status=2` 申诉驳回；`result` 1~255 字符。成功返回 `appeal_id`、`report_id`、`status`、`result`、`content_restored`。`content_restored=true` 仅表示该内容当时仍由原举报的 `hide_content` 处置下架，且本次复审成功撤销了这次下架；若内容后来被独立审核下架则返回 `false`，不会覆盖后续决定。原举报审核人复审同一申诉返回 `409`；已进入 `1/2` 终态的申诉再次处理返回 `409`；不存在返回 `404`。

#### 状态与处置边界

- `PATCH /api/v1/admin/reports/{report_id}/review` 只允许把 `status=0` 处理一次；已处理/已驳回均为终态，再次调用返回 `409`，不会覆盖原结论。
- 举报记录持久化 `action`、`reviewed_by`、`reviewed_at`。申诉记录独立保存原因、结论、复审人和时间，不回写或删除原举报结论。
- 动态、评论和纸飞机的管理员下架统一写 `moderation_status=2`，并用 `moderation_report_id` 记录举报处置来源；恢复写 `1` 并清空来源。用户删除只写 `deleted_at`。申诉通过仅撤销当前仍归因于原举报的下架，因此不会复活用户自行删除的内容，也不会覆盖后续独立审核。
- 纸飞机继续使用既有独立 `moderation_status`，不改变生命周期 `status`。
- 内容处置继续写 `business_audit_log`。举报审核和申诉复审均使用行锁，避免并发覆盖终态。
- 举报审核组合必须一致：`hide_content` / `restore_content` 只能搭配 `status=1`，`dismiss` 只能搭配 `status=2`；矛盾请求返回 `422`。

错误补充：状态冲突或审核人冲突为 `409`；前端应刷新举报/申诉详情，不得自动重试审核写请求。
