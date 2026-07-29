# 关系、聊天、通知与安全接口

## 1. 通用约定

接口前缀：`/api/v1`。

本文件所有接口都要求登录，并且通过 `get_verified_user` 校验手机号已绑定：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

`access_token` 中的内部 `sid` 是登录会话标识，仅由服务端解析。聊天接口使用的 `session_id` 是 `GET /api/v1/chat/sessions` 返回的聊天会话 ID，两者不是同一个值，前端不需要自行拼接或上传登录 `session_id`。

成功响应没有统一 `data` 包装层；`204 No Content` 接口没有响应体。除特别说明外，错误响应为：

```json
{"detail":"错误原因"}
```

### 1.1 公共状态和权限

- 关系、聊天、社区和安全接口要求账号存在有效手机号。
- 目标用户必须处于公开交友状态，不能是当前用户本人，也不能与当前用户互相拉黑。
- 聊天要求双方存在有效匹配关系（匹配状态 `1` 或 `2`）。
- 喜欢、取消喜欢、关注、取消关注，以及发现模块的认识申请创建/同意/拒绝，要求当前用户 `realname_status == 2`；未通过返回 `403` 和 `{"detail":"请先完成实名认证"}`。
- 关系列表读取、举报和拉黑/解除拉黑继续允许已登录、已绑定手机号的非实名用户使用。
- 关系写入使用数据库唯一约束和 `INSERT IGNORE`，重复喜欢/关注通常会返回当前状态，不会新增重复记录。
- 当前发送消息、发布内容、举报没有 `Idempotency-Key` 机制；网络超时后不要盲目重试写请求，应先查询结果。

## 2. 喜欢、关注和匹配

### 2.1 喜欢用户

#### `PUT /api/v1/users/{target_id}/like`

权限：已登录、已绑定手机号且实名认证通过（`realname_status == 2`）；未通过返回 `403`。成功状态：`200 OK`。

路径参数：

| 参数 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `target_id` | path | integer | 是 | `>=1`，不能是自己 | 被喜欢用户 ID |

请求体：无。

请求示例：

```http
PUT /api/v1/users/23/like
Authorization: Bearer <access_token>
```

返回参数：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `target_user_id` | integer | 是 | 不为空 | 目标用户 ID |
| `relation_type` | string | 是 | 不为空 | 固定为 `like` |
| `enabled` | boolean | 是 | 不为空 | 本次操作后是否喜欢 |
| `matched` | boolean | 是 | 不为空 | 是否仍存在有效 match（通常来自申请同意；**喜欢本身不再创建匹配**） |

成功响应：

```json
{"target_user_id":23,"relation_type":"like","enabled":true,"matched":false}
```

定版产品规则：**喜欢为私有列表操作**，互喜欢**不会**创建 `user_match`、**不会**创建 `chat_session`、**不会**发送「可以开始聊天了」类匹配通知。聊天会话仅在认识申请被同意等既有路径建立。`matched` 仅反映当前是否仍有有效匹配（例如双方此前已通过申请）。

#### `DELETE /api/v1/users/{target_id}/like`

权限：已登录、已绑定手机号且实名认证通过（`realname_status == 2`）；未通过返回 `403`。路径参数与喜欢接口相同，请求体无，成功状态 `200 OK`。响应示例：

```json
{"target_user_id":23,"relation_type":"like","enabled":false,"matched":false}
```

取消喜欢只删除当前用户的私有喜欢记录。它不会修改任何 `user_match` 状态，也不会撤销或删除由认识申请同意等流程创建的匹配与聊天。目标用户不存在、不可见或已拉黑时返回 `404` 或 `403`。

### 2.2 关注用户

#### `PUT /api/v1/users/{target_id}/follow`

权限：已登录、已绑定手机号且实名认证通过（`realname_status == 2`）；未通过返回 `403`。请求体无，成功状态 `200 OK`。返回格式与喜欢接口相同，但 `relation_type` 固定为 `follow`，`matched` 固定为 `false`：

```json
{"target_user_id":23,"relation_type":"follow","enabled":true,"matched":false}
```

#### `DELETE /api/v1/users/{target_id}/follow`

权限：已登录、已绑定手机号且实名认证通过（`realname_status == 2`）；未通过返回 `403`。请求体无，成功状态 `200 OK`。返回 `enabled=false`。关注不会触发匹配，也不会建立聊天会话。

### 2.3 关系列表

以下接口都返回同一结构 `RelationPage`，成功状态 `200 OK`：

| 方法和路径 | 关系方向 | 结果 |
| --- | --- | --- |
| `GET /api/v1/relations/likes` | 当前用户 -> 他人 | 我喜欢的人 |
| `GET /api/v1/relations/liked-by` | 不再提供 | 已废弃，固定返回 `410` 且不查询/返回发送者身份 |
| `GET /api/v1/relations/following` | 当前用户 -> 他人 | 我关注的人 |
| `GET /api/v1/relations/followers` | 他人 -> 当前用户 | 我的粉丝 |
| `GET /api/v1/relations/matches` | 当前用户的有效匹配 | 我的匹配 |

公共查询参数：

| 参数 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `page` | query | integer | 否 | `1` | `1~1000` | 页码，从 1 开始 |
| `page_size` | query | integer | 否 | `20` | `1~50` | 每页数量 |

请求示例：

```http
GET /api/v1/relations/matches?page=1&page_size=20
Authorization: Bearer <access_token>
```

返回参数：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `items` | array[SocialUser] | 是 | 无数据时为 `[]` | 用户列表 |
| `items[].user_id` | integer | 是 | 不为空 | 用户 ID |
| `items[].nickname` | string/null | 是 | 未设置时 `null` | 昵称 |
| `items[].avatar` | string/null | 是 | 未设置时 `null` | 头像地址 |
| `items[].age` | integer/null | 是 | 未设置生日时 `null` | 根据生日计算的年龄 |
| `page` | integer | 是 | 不为空 | 当前页码 |
| `page_size` | integer | 是 | 不为空 | 当前页大小 |
| `total` | integer | 是 | 无数据时为 `0` | 总记录数 |

响应示例：

```json
{
  "items":[{"user_id":23,"nickname":"小雨","avatar":"/storage/uploads/23/avatar.webp","age":28}],
  "page":1,
  "page_size":20,
  "total":1
}
```

无数据响应：

```json
{"items":[],"page":1,"page_size":20,"total":0}
```

喜欢是私有数据。唯一受支持的喜欢列表接口是 `GET /api/v1/relations/likes`，仅返回当前用户主动喜欢的人。`GET /api/v1/relations/liked-by` 已废弃，所有已认证调用固定返回：

```http
HTTP/1.1 410 Gone
```

```json
{"detail":"鍠滄鍒楄〃浠呮湰浜哄彲瑙?"}
```

该废弃接口不会查询或返回喜欢发送者身份。旧客户端必须移除“喜欢我的人”入口，并迁移到仅展示 `GET /relations/likes` 的本人私有喜欢列表。

### 2.4 认识申请的实名要求（发现模块）

以下写接口要求 `realname_status == 2`，未通过返回上述 `403`；收到/发出的申请列表读取仍对已登录非实名用户开放：

- `POST /api/v1/discovery/applications/{target_id}`
- `POST /api/v1/discovery/applications/{application_id}/accept`
- `POST /api/v1/discovery/applications/{application_id}/reject`

### 2.5 取消匹配

#### `DELETE /api/v1/relations/matches/{target_id}`

路径参数 `target_id` 为 `>=1` 的目标用户 ID。请求体无，成功状态 `204 No Content`。仅能取消当前用户参与的有效匹配；匹配不存在返回：

```json
{"detail":"匹配关系不存在"}
```

## 3. 聊天

### 3.1 获取对话列表

#### `GET /api/v1/chat/sessions`

返回分页对象：`items`、`page`、`page_size`、`total`、`has_more`。`items` 仅包含仍匹配、未拉黑且未隐藏的会话。

成功状态 `200 OK`，返回当前用户可访问的聊天会话数组，按最近消息时间倒序。列表只包含有效匹配且未互相拉黑的双方。

查询参数：`page` 为 `1~1000`，默认 `1`；`page_size` 为 `1~50`，默认 `20`。当前代码返回数组，没有 `total` 和 `has_more` 字段；前端可通过返回数量小于 `page_size` 判断是否可能到达末页。

返回参数：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 聊天会话 ID，后续消息接口的 `session_id` |
| `target` | object | 是 | 不为空 | 对方用户摘要 |
| `target.user_id` | integer | 是 | 不为空 | 对方用户 ID |
| `target.nickname` | string/null | 是 | 未设置时 `null` | 对方昵称 |
| `target.avatar` | string/null | 是 | 未设置时 `null` | 对方头像 |
| `target.age` | integer/null | 是 | 未设置生日时 `null` | 对方年龄 |
| `last_message` | string/null | 是 | 尚未发消息时 `null` | 最后一条消息预览；媒体消息为 `[媒体消息]` |
| `last_message_time` | datetime/null | 是 | 尚未发消息时 `null` | 最后一条消息时间，UTC 数据库时间 |
| `unread_count` | integer | 是 | 无未读时为 `0` | 当前用户未读消息数 |

响应示例：

```json
[
  {
    "id":8,
    "target":{"user_id":23,"nickname":"小雨","avatar":"/storage/uploads/23/avatar.webp","age":28},
    "last_message":"你好，很高兴认识你",
    "last_message_time":"2026-07-20T10:30:00",
    "unread_count":2
  }
]
```

没有会话时返回 `[]`。

### 3.2 获取聊天记录

#### `GET /api/v1/chat/sessions/{session_id}/messages`

路径参数 `session_id` 必须使用对话列表返回的 `id`，不能使用登录 Token 中的内部会话 `sid`。查询参数 `page` 默认 `1`、范围 `1~1000`；`page_size` 默认 `20`、范围 `1~50`。成功状态 `200 OK`，消息按时间正序返回当前页。

返回参数：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 消息 ID |
| `session_id` | integer | 是 | 不为空 | 所属聊天会话 ID |
| `from_user_id` | integer | 是 | 不为空 | 发送者 ID |
| `to_user_id` | integer | 是 | 不为空 | 接收者 ID |
| `type` | integer | 是 | `1~6` | 消息类型 |
| `content` | string/null | 是 | 媒体消息可为 `null`；撤回后为 `消息已撤回` | 文本或系统消息内容 |
| `media_url` | string/null | 是 | 非媒体消息为 `null`；撤回后为 `null` | 已上传媒体地址 |
| `is_read` | boolean | 是 | 不为空 | 是否已读 |
| `revoked` | boolean | 是 | 不为空 | 是否已撤回 |
| `created_at` | datetime | 是 | 不为空 | 创建时间 |

消息类型：`1` 文本，`2` 图片，`3` 语音，`4` 视频，`5` 小程序卡片，`6` 系统消息。空数据返回 `[]`。

请求示例：

```http
GET /api/v1/chat/sessions/8/messages?page=1&page_size=20
Authorization: Bearer <access_token>
```

### 3.3 发送聊天消息

#### `POST /api/v1/chat/sessions/{session_id}/messages`

成功状态 `201 Created`。服务端只校验消息字段，不负责聊天媒体上传；`media_url` 必须是前端已经获得的资源地址。

请求参数：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `session_id` | path | integer | 是 | 无 | `>=1`，必须属于当前用户 | 聊天会话 ID |
| `type` | body | integer | 否 | `1` | `1~6` | 消息类型 |
| `content` | body | string/null | 条件必填 | `null` | 最长 5000；`type=1` 时必填 | 文本/系统/卡片内容 |
| `media_url` | body | string/null | 条件必填 | `null` | 最长 500；`type=2/3/4` 时必填 | 图片、语音或视频地址 |

文本示例：

```json
{"type":1,"content":"你好，很高兴认识你"}
```

图片示例：

```json
{"type":2,"media_url":"/storage/uploads/chat/message.webp"}
```

校验规则：文本消息不能没有 `content`；图片/语音/视频不能没有 `media_url`；卡片和系统消息至少提供 `content` 或 `media_url`。成功响应为 `ChatMessageResponse`，字段与 3.2 相同。

### 3.4 标记会话已读

#### `POST /api/v1/chat/sessions/{session_id}/read`

请求体无，成功状态 `204 No Content`。服务端只将发给当前用户的未读消息改为已读，并清零当前用户在该会话中的未读计数。会话不存在、非参与者、未匹配或已拉黑返回 `403/404`。

### 3.5 撤回消息

#### `DELETE /api/v1/chat/messages/{message_id}`

请求体无，成功状态 `204 No Content`。只能撤回自己发送且尚未撤回的消息；撤回后查询接口返回 `content="消息已撤回"`、`media_url=null`、`revoked=true`。消息不存在、不是本人发送或已撤回时返回：

```json
{"detail":"消息不存在或无法撤回"}
```

## 4. 通知

### 4.1 查询通知

#### `GET /api/v1/notifications`

查询参数 `page` 默认 `1`、范围 `1~1000`；`page_size` 默认 `20`、范围 `1~50`。成功状态 `200 OK`，按创建时间倒序。

返回参数：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `items` | array | 是 | 无数据时 `[]` | 通知列表 |
| `items[].id` | integer | 是 | 不为空 | 通知 ID |
| `items[].notification_type` | string | 是 | 不为空 | 通知类型，如 `like`、`match` |
| `items[].title` | string/null | 是 | 无标题时 `null` | 通知标题 |
| `items[].content` | string | 是 | 无正文时为空字符串 | 通知正文 |
| `items[].payload` | object/null | 是 | 无附加数据时 `null` | 结构化附加数据 |
| `items[].related_user_id` | integer/null | 是 | 无关联用户时 `null` | 关联用户 ID |
| `items[].related_id` | integer/null | 是 | 无关联业务记录时 `null` | 关联记录 ID |
| `items[].target_type` | string/null | 是 | 旧通知可能为 `null` | 导航目标类型，如 `post`、`comment`、`user`、`report`、`activity` |
| `items[].target_id` | integer/null | 是 | 无目标或目标已失效时 `null` | 导航目标 ID |
| `items[].actor_user_id` | integer/null | 是 | 治理系统通知可为 `null` | 触发事件的用户 ID；兼容别名，等同于 `related_user_id` |
| `items[].action` | string | 是 | 不为空 | 触发动作，当前等同于 `notification_type`，如 `comment`、`follow`、`activity` |
| `items[].is_read` | boolean | 是 | 不为空 | 是否已读 |
| `items[].created_at` | datetime | 是 | 不为空 | 创建时间 |
| `page` | integer | 是 | 不为空 | 当前页 |
| `page_size` | integer | 是 | 不为空 | 当前页大小 |
| `total` | integer | 是 | 无通知时 `0` | 总通知数 |
| `unread_count` | integer | 是 | 无未读时 `0` | 未读通知总数 |

成功示例：

```json
{
  "items":[{"id":10,"notification_type":"comment","title":"有人评论了你的动态","content":"很喜欢这张照片","payload":{"post_id":4},"related_user_id":23,"related_id":4,"target_type":"post","target_id":4,"actor_user_id":23,"action":"comment","is_read":false,"created_at":"2026-07-20T10:30:00"}],
  "page":1,"page_size":20,"total":1,"unread_count":1
}
```

### 4.2 标记通知已读

#### `POST /api/v1/notifications/{notification_id}/read`

路径参数 `notification_id>=1`。请求体无，成功状态 `204 No Content`。只更新属于当前用户的通知；重复标记已读可安全调用。

#### `POST /api/v1/notifications/read-all`

请求体无，成功状态 `204 No Content`。将当前用户全部通知标记为已读；重复调用可安全调用。

## 5. 隐私设置

### 5.1 查询隐私设置

#### `GET /api/v1/users/me/privacy`

无请求参数，成功状态 `200 OK`。不存在设置记录时返回默认值。

### 5.2 更新隐私设置

#### `PUT /api/v1/users/me/privacy`

请求体支持部分更新；只提交需要改变的字段。空对象 `{}` 合法，表示不修改并返回当前设置。成功状态 `200 OK`。

请求字段：

| 字段 | 类型 | 必填 | 默认/空值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `hide_phone` | boolean/null | 否 | `false` | 布尔值 | 隐藏手机号 |
| `hide_school` | boolean/null | 否 | `false` | 布尔值 | 隐藏学校信息 |
| `hide_company` | boolean/null | 否 | `false` | 布尔值 | 隐藏公司信息 |
| `hide_distance` | boolean/null | 否 | `false` | 布尔值 | 隐藏距离信息 |
| `hide_online_status` | boolean/null | 否 | `false` | 布尔值 | 隐藏在线状态 |
| `only_auth_can_contact` | boolean/null | 否 | `false` | 布尔值 | 仅认证用户可联系 |
| `only_vip_can_see_detail` | boolean/null | 否 | `false` | 布尔值 | 仅 VIP 可查看详细资料 |
| `who_can_see_me` | integer/null | 否 | `1` | `1` 所有人，`2` 仅认证，`3` 仅 VIP，`4` 完全私密 | 谁可以在公开流中看到我 |
| `match_status` | integer/null | 否 | `1` | `1` 公开展示，`2` 委托红娘，`3` 完全私密，`4` 暂停服务，`5` 已脱单 | 当前交友展示状态 |
| `anonymous_browse_enabled` | boolean/null | 否 | `false` | VIP 无痕浏览能力 | 浏览他人时不写入对方访客记录 |
| `show_profile` | boolean/null | 否 | `true` | 布尔值 | 是否允许他人查看自己的公开资料；关闭后从首页和他人主页隐藏 |
| `show_likes` | boolean/null | 否 | `true` | 布尔值 | 是否允许展示自己的喜欢列表；当前版本没有公开他人喜欢列表接口 |
| `show_posts` | boolean/null | 否 | `true` | 布尔值 | 是否允许自己的动态出现在动态流 |
| `notify_like` | boolean/null | 否 | `true` | 布尔值 | 喜欢通知开关 |
| `notify_comment` | boolean/null | 否 | `true` | 布尔值 | 评论通知开关 |
| `notify_follow` | boolean/null | 否 | `true` | 布尔值 | 关注通知开关 |
| `notify_message` | boolean/null | 否 | `true` | 布尔值 | 新消息通知开关 |
| `notify_match` | boolean/null | 否 | `true` | 布尔值 | 匹配通知开关 |
| `notify_apply` | boolean/null | 否 | `true` | 布尔值 | 认识申请通知开关 |
| `notify_system` | boolean/null | 否 | `true` | 布尔值 | 系统通知开关 |
| `notify_activity` | boolean/null | 否 | `true` | 布尔值 | 动态活动通知开关 |

请求示例：

```json
{
  "only_vip_can_see_detail":true,
  "who_can_see_me":1,
  "anonymous_browse_enabled":false,
  "notify_like":true
}
```

响应字段与请求字段相同，并额外返回 `user_id`。所有布尔字段始终返回 `true/false`，不会返回 `null`。

## 6. 黑名单

### `GET /api/v1/security/blocks`

无参数，成功状态 `200 OK`，返回 `SocialUser[]`。用户字段为 `user_id`、`nickname`、`avatar`、`age`；无黑名单时返回 `[]`。

### `PUT /api/v1/security/blocks/{target_id}`

路径参数 `target_id>=1`，成功状态 `204 No Content`。请求体可省略；也可传：

```json
{"reason":"骚扰"}
```

`reason` 类型为 `string/null`，最长 255 字符。拉黑会取消双方有效匹配、关闭双方待处理认识申请，并阻止主页、关系、申请和聊天操作。重复拉黑使用 `INSERT IGNORE`，可安全重复调用。

### `DELETE /api/v1/security/blocks/{target_id}`

请求体无，成功状态 `204 No Content`。只解除当前用户发起的拉黑；目标账号不存在或已停用返回 `404`。

## 7. 举报

### `POST /api/v1/security/reports/{target_id}`

路径参数 `target_id>=1`，成功状态 `201 Created`。请求体：

| 字段 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `type` | string | 是 | 无 | 1~64 字符 | 举报类型，当前由客户端传入 |
| `description` | string/null | 否 | `null` | 最长 1000 字符 | 举报说明 |
| `images` | array[string] | 否 | `[]` | 最多 6 个地址 | 举报证据图片地址 |

请求示例：

```json
{
  "type":"骚扰",
  "description":"持续发送不当消息",
  "images":["/storage/uploads/report/example.png"]
}
```

返回参数：

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `id` | integer | 是 | 举报记录 ID |
| `target_user_id` | integer | 是 | 被举报用户 ID |
| `target_type` | string | 是 | 固定 `user`（用户举报轨） |
| `target_id` | integer/null | 是 | 与 `target_user_id` 相同 |
| `type` | string | 是 | 举报类型 |
| `status` | integer | 是 | `0` 待处理，`1` 已处理，`2` 已驳回 |
| `created_at` | datetime | 是 | 创建时间 |

成功响应：

```json
{"id":31,"target_user_id":23,"target_type":"user","target_id":23,"type":"骚扰","status":0,"created_at":"2026-07-20T11:00:00"}
```

当前接口只保存**用户**举报记录（`target_type=user`），不会自动封禁目标用户。社区帖子/评论/纸飞机请使用 `POST /api/v1/community/reports`（见 `docs/api/community.md`）。后台处理见 `docs/api/admin.md`。

## 8. 错误响应

| HTTP | 触发条件 | 示例 detail | 前端处理 |
| --- | --- | --- | --- |
| `401` | 未登录、Token 无效或会话失效 | `请先登录` | 清理 Token 并重新登录 |
| `403` | 未绑定手机号、未匹配、已拉黑或无权限 | `当前没有聊天权限` | 展示绑定/匹配/权限提示 |
| `403` | 喜欢/关注或认识申请写操作未通过实名认证 | `请先完成实名认证` | 引导实名认证；举报、拉黑和读取仍可用 |
| `404` | 用户、会话、消息、关系或举报目标不存在 | `聊天会话不存在` | 刷新列表并移除失效项 |
| `409` | 业务状态冲突 | `关系状态冲突` | 重新查询当前状态 |
| `410` | 调用已废弃的 `GET /relations/liked-by` | `鍠滄鍒楄〃浠呮湰浜哄彲瑙?` | 移除该入口；仅使用 `GET /relations/likes` |
| `422` | 类型、长度、范围或枚举不合法 | `文本消息内容不能为空` | 修正请求参数 |

## 9. 状态、兼容性与现有能力复用

- 关系状态：喜欢/关注通过 `user_favorite.type` 区分，匹配状态 `1/2` 表示有效，`3` 表示取消。
- 聊天会话由**申请同意**等匹配路径创建，**不由**互相喜欢创建；会话 ID 稳定对应 `chat_session.id`。
- 本组接口复用 FastAPI 的 `HTTPBearer`、Pydantic Schema、SQLAlchemy AsyncSession 和已有关系/通知/Redis 能力，不新增重复的认证、分页或缓存实现。
- 当前分页列表响应格式已上线，后续如要增加 `has_more` 或改成统一 `{items,...}` 包装，属于响应契约变更，必须先增加兼容版本或同步前端迁移。

## 10. 变更记录

### 2026-07-25

- **喜欢与关注实名门禁：** 变更前，公共权限章节已有实名要求，但喜欢和关注主端点未逐条写明。变更后，like/unlike/follow/unfollow 四条写接口均明确要求已登录、已绑定手机号且 `realname_status == 2`，未通过返回 `403`。影响：读取关系、举报和拉黑/解除拉黑仍只需手机号已绑定；未实名客户端应先完成实名认证再发起关系写入。
- **喜欢不再自动匹配：** `PUT /users/{id}/like` 仅维护 `user_favorite`；互喜欢不创建 match/chat_session、不发匹配通知。聊天入口以申请同意等路径为准（定版：喜欢私有 + 先申请再聊）。
- **取消喜欢不影响申请匹配：** `DELETE /users/{id}/like` 仅删除私有喜欢记录，永不修改申请创建的 match 或 chat。
- **喜欢列表收口：** 仅 `GET /relations/likes` 受支持；`GET /relations/liked-by` 废弃并固定返回不含身份的 `410`。
- **实名权限：** 喜欢/关注和认识申请创建/同意/拒绝要求 `realname_status == 2`；举报、拉黑与读取保持可用。
- 本地 HTTP 冒烟：互喜欢后 `chat_session`/`active_match` 仍为 0；`POST /discovery/applications/{id}/accept` 后才出现会话（对照验证）。

### 2026-07-20

- 将所有接口补充为请求位置、类型、必填性、规则、返回字段和完整示例。
- 明确登录 Token 中的内部会话 `sid` 与聊天 `session_id` 的区别。
- 明确匹配、聊天权限、黑名单、隐私枚举、通知和举报状态。
- 明确当前未提供消息幂等键、媒体上传和自动审核的边界。

### 2026-07-28（中央通知事件）

- `GET /api/v1/notifications` 的 `items[]` 新增向后兼容字段：`target_type: string|null` 与 `target_id: integer|null`。新事件使用这两个字段导航；`related_user_id`、`related_id` 继续保留给旧客户端。
- `items[]` 同时新增 `actor_user_id` 与 `action`：分别是 `related_user_id` 的标准别名和事件动作；旧客户端可继续使用原字段。
- 通知成功示例：`{"id":12,"notification_type":"comment","title":"有人评论了你的动态","content":"很喜欢这张照片","payload":{"comment_id":31},"related_user_id":8,"related_id":4,"target_type":"post","target_id":4,"is_read":false,"created_at":"2026-07-28T10:00:00"}`。
- `GET/PUT /api/v1/users/me/privacy` 新增 `notify_follow: boolean` 与 `notify_message: boolean`，默认均为 `true`；请求可只提交其中任一个字段。
- 互动与普通系统通知统一检查接收人的事件偏好；`reply` 复用 `notify_comment`，未知系统事件复用 `notify_system`。`report_result`（举报处理结果与内容治理通知）和 `appeal_result`（申诉复审结果）属于必达治理站内信，始终写入，不受 `notify_system` 关闭影响。
- 操作者与接收人相同不写通知；双方任一方向存在 `user_block` 时不写互动通知。关注、回复评论和点赞评论在动作层同样执行拉黑检查，返回 `403`。
- 关注只在首次写入关系时产生 `follow` 通知；重复 `PUT`、取消关注不产生通知。动态/评论点赞同样只在首次点赞时通知。
- 兼容性：旧通知行的目标字段可为 `null`；旧客户端继续读取 `related_id`。新客户端不得假定所有通知都可导航。
- 通知标题和正文预览由中央写入器分别限制为 128 和 255 字符；长评论仍可正常发布，通知仅保存截断预览。
