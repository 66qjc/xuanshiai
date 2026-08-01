# 社区动态、评论与纸飞机接口

## 1. 通用约定

接口前缀：`/api/v1`。所有接口都要求登录且已绑定手机号：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

成功响应没有统一 `data` 包装层；删除类接口使用 `204 No Content` 且没有响应体。错误响应统一为：

```json
{"detail":"错误原因"}
```

当前实现复用 FastAPI、Pydantic、SQLAlchemy AsyncSession 和 Redis 日额度工具。社区图文/短视频请先调用 `POST /api/v1/community/media/uploads` 拿到 `media_id`，发布时传 `image_media_ids` / `video_media_id`（或过渡期的已上传 storage URL）。本组接口不负责图片内容识别；文本敏感词 MVP 已接入写路径。

以下社区写操作还要求 `realname_status == 2`：发布/删除动态、动态点赞或取消点赞、收藏或取消收藏、发表/删除评论、参与话题、活动报名、发送或回复纸飞机、社区媒体上传/删除。未通过实名时返回 `403`：

```json
{"detail":"请先完成实名认证"}
```

动态流、动态详情、评论列表、话题/活动列表与详情、纸飞机读取、举报原因等浏览能力继续对已登录且绑定手机号的非实名用户开放。

### 1.1 创建接口幂等 Header（2026-07-25 变更）

以下四条创建接口新增向后兼容的可选请求头：

- `POST /api/v1/community/posts`
- `POST /api/v1/community/posts/{post_id}/comments`
- `POST /api/v1/paper-planes`
- `POST /api/v1/paper-planes/{plane_id}/replies`

| 参数 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `Idempotency-Key` | header | string | 否 | 无 | 8~128 字符；同一用户、同一接口操作内唯一 | 客户端为一次创建意图生成的稳定幂等键 |

合法示例：

```http
Idempotency-Key: post-20260725-0001
```

非法示例：`Idempotency-Key: short`，返回 `422`。旧客户端不传该 Header 时继续按原行为创建，响应模型和成功状态不变。

传入 Header 时，服务端先提交一个短事务预留，再执行创建；目标记录与幂等完成响应在同一数据库事务提交。处理规则：

- 同一个 key、同一个规范化请求载荷已完成：返回首次保存的相同响应，不重复创建，也不重复扣纸飞机额度。
- 同一个 key 正在处理：返回 `409`，客户端应稍后使用原 key 和原载荷重试。
- 同一个 key 已用于不同载荷（评论包含 `post_id`，纸飞机回复包含 `plane_id`）：返回 `409`，客户端必须为新的创建意图生成新 key。
- key 的作用域为当前用户和具体创建操作；不同用户或不同操作可以使用相同文本 key。

冲突响应示例：

```json
{"detail":"Idempotency-Key 已用于不同请求"}
```

## 2. 社区动态

### 2.1 发布动态

#### `POST /api/v1/community/posts`

权限：已登录、绑定手机号且实名认证通过。成功状态：`201 Created`。支持 1.1 节的可选 `Idempotency-Key`。

请求字段：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `content` | body | string | 条件 | `""` | 最长 2000；与媒体至少一个非空 | 动态正文；允许仅媒体无正文（media-only） |
| `image_media_ids` | body | array[integer] | 否 | `[]` | 最多 9 个；元素 `>=1`；与 `video_media_id` / 视频互斥 | 优先：先上传得到的社区图片媒体 ID |
| `video_media_id` | body | integer/null | 否 | `null` | `>=1`；与图片字段互斥 | 优先：先上传得到的社区视频媒体 ID |
| `images` | body | array[string] | 否 | `[]` | 最多 9 个；过渡期 URL | 兼容旧客户端：须为本人已上传且 `status=ready` 的 `/storage/uploads/{user_id}/community/...`；禁止外链/本地临时路径 |
| `video` | body | string/null | 否 | `null` | 最长 500；过渡期 URL | 兼容旧客户端：规则同 `images` |
| `location` | body | string/null | 否 | `null` | 最长 128 字符 | 展示位置文本 |
| `topic_id` | body | integer/null | 否 | `null` | 非空时 `>=1` | 话题 ID；可通过 `GET /api/v1/community/topics`、`GET /api/v1/community/topics/page` 查询，并用 `GET /api/v1/community/topics/{topic_id}` 或 `GET /api/v1/community/topics/{topic_id}/detail` 查看详情 |
| `visibility` | body | integer | 否 | `0` | `0` / `1` / `2` | `0` 公开，`1` 仅双向匹配用户可见，`2` 仅作者本人可见 |
| `declaration` | body | string | 否 | `""` | `""` / `"内容包含虚构演绎"` / `"内容包含广告推广"` / `"内容可能引起不适"` | 作者选择的内容声明 |

规则补充：

- `content`、图片（`image_media_ids` 或 `images`）、视频（`video_media_id` 或 `video`）三者不可同时为空。
- 图片与视频不可混用（含 id 与 URL 混用）。
- 发布成功后服务端把对应 `community_media` 从 `ready` 绑为 `bound`，响应里的 `images`/`video` 仍是可展示的 storage URL。

推荐请求示例（media id）：

```json
{
  "content":"",
  "image_media_ids":[101,102],
  "video_media_id":null,
  "location":"上海",
  "topic_id":null,
  "visibility":1,
  "declaration":""
}
```

过渡期 URL 示例：

```json
{
  "content":"今天去看了一个展览",
  "images":["/storage/uploads/1/community/a.webp"],
  "video":null,
  "location":"上海",
  "topic_id":null,
  "visibility":1,
  "declaration":"内容包含广告推广"
}
```

非法示例：

```json
{"content":"","images":[],"image_media_ids":[]}
```

```json
{"content":"x","image_media_ids":[1],"video_media_id":2}
```

成功返回 `CommunityPostResponse`：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 动态 ID |
| `user_id` | integer | 是 | 不为空 | 作者 ID |
| `nickname` | string/null | 是 | 未设置时 `null` | 作者昵称 |
| `avatar` | string/null | 是 | 未设置时 `null` | 作者头像 |
| `content` | string | 是 | 不为空 | 动态正文 |
| `images` | array[string] | 是 | 无图片为 `[]` | 图片地址 |
| `video` | string/null | 是 | 无视频为 `null` | 视频地址 |
| `location` | string/null | 是 | 未填写为 `null` | 位置文本 |
| `visibility` | integer | 是 | 不为空 | `0` 公开、`1` 仅双向匹配用户可见、`2` 仅作者本人可见 |
| `declaration` | string | 是 | 空字符串表示未声明 | 作者选择的内容声明 |
| `like_count` | integer | 是 | 无点赞为 `0` | 点赞数 |
| `comment_count` | integer | 是 | 无评论为 `0` | 评论数 |
| `is_liked` | boolean | 是 | 不为空 | 当前用户是否点赞 |
| `realname_status` | integer | 是 | 未认证时为 `0` | 作者的实名状态，取自 `user_auth.realname_status` |
| `created_at` | datetime | 是 | 不为空 | 创建时间 |

响应示例：

```json
{
  "id":101,"user_id":1,"nickname":"小明","avatar":"/storage/uploads/1/avatar.webp",
  "content":"今天去看了一个展览","images":["/storage/uploads/1/photo.webp"],"video":null,
  "location":"上海","visibility":1,"declaration":"内容包含广告推广",
  "like_count":0,"comment_count":0,"is_liked":false,"realname_status":2,
  "created_at":"2026-07-20T12:00:00"
}
```

### 2.2 查看动态流

#### `GET /api/v1/community/posts`

成功状态 `200 OK`。查询参数：

| 参数 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `mode` | query | string | 否 | `latest` | `latest` / `following` / `city` / `liked_users` / `following_and_liked` / `mine` | 全站最新 / 我关注用户 / 同城 / 我喜欢用户发布的动态 / 关注与喜欢用户的并集 / 我的动态 |
| `page` | query | integer | 否 | `1` | `1~1000` | 页码 |
| `page_size` | query | integer | 否 | `20` | `1~50` | 每页数量 |
| `city` | query | string/null | 否 | `null` | 最长 64 字符 | `mode=city` 的城市展示名锚点 |
| `city_code` | query | string/null | 否 | `null` | 恰好 4 或 6 位 ASCII 数字；4 位短码规范化为末尾补 `00` 的 6 位市码 | `mode=city` 的城市码锚点，优先于纯文案匹配 |
| `filter` | query | string/null | 否 | `null` | `all` / `mbti` / `alumni` / `hometown` / `hot` / `latest` | 发现页二级筛选或热度 |
| `sort` | query | string | 否 | `latest` | `latest` / `hot` | 排序；`filter=hot` 时按热度 |

`city_code` 提供时只能是 4 或 6 位 ASCII 数字；其他值在 API 边界返回 `422`。同时提供且与 `city` 冲突时，`city_code` 优先。`mode=city` 仅按帖子发布地 `location` 过滤；请求未提供城市时才回落到同城浏览偏好 `community_city_*`，两者都没有可用锚点时返回 `422`。

`mode=mine` 仅返回当前用户作为作者的动态，不接受外部作者 ID。

请求示例：

```http
GET /api/v1/community/posts?mode=following&page=1&page_size=20
Authorization: Bearer <access_token>
```

返回字段：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `items` | array[CommunityPostResponse] | 是 | 无数据为 `[]` | 动态列表 |
| `page` | integer | 是 | 不为空 | 当前页 |
| `page_size` | integer | 是 | 不为空 | 当前页大小 |
| `total` | integer | 是 | 无数据为 `0` | 当前模式下动态总数 |

排序：先按平台置顶字段 `is_top` 倒序，再按 `created_at` 倒序，最后按 `id` 倒序保证分页稳定。成功示例：

```json
{"items":[],"page":1,"page_size":20,"total":0}
```

### 2.3 删除动态

#### `DELETE /api/v1/community/posts/{post_id}`

路径参数 `post_id>=1`，请求体无，成功状态 `204 No Content`。仅作者可以删除自己的有效动态，服务端执行软删除；重复删除或删除他人动态返回：

```json
{"detail":"动态不存在或无权删除"}
```

### 2.4 点赞和取消点赞

#### `PUT /api/v1/community/posts/{post_id}/like`

请求体无，成功状态 `200 OK`，返回更新后的完整动态对象，`is_liked=true`。

#### `DELETE /api/v1/community/posts/{post_id}/like`

请求体无，成功状态 `200 OK`，返回更新后的完整动态对象，`is_liked=false`。

两类操作使用已有社区点赞记录，重复点赞或重复取消不会产生重复记录；动态不存在返回 `404`。

## 3. 评论

### 3.1 查询评论

#### `GET /api/v1/community/posts/{post_id}/comments`

查询参数 `page` 默认 `1`、范围 `1~1000`；`page_size` 默认 `20`、范围 `1~50`。成功状态 `200 OK`，按评论创建时间正序返回数组，不返回 `total`。查询评论前，服务端使用当前用户身份执行与动态详情相同的可见性检查；动态不存在、被屏蔽或隐私不可见时返回 `404`，不会绕过动态权限直接暴露评论。

返回字段：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 评论 ID |
| `post_id` | integer | 是 | 不为空 | 动态 ID |
| `user_id` | integer | 是 | 不为空 | 评论者 ID |
| `nickname` | string/null | 是 | 未设置时 `null` | 评论者昵称 |
| `avatar` | string/null | 是 | 未设置时 `null` | 评论者头像 |
| `parent_id` | integer/null | 是 | 一级评论为 `null` | 父评论 ID |
| `content` | string | 是 | 不为空 | 评论内容 |
| `like_count` | integer | 是 | 无点赞为 `0` | 评论点赞数 |
| `created_at` | datetime | 是 | 不为空 | 创建时间 |

无评论时返回 `[]`。

### 3.2 发表评论或回复

#### `POST /api/v1/community/posts/{post_id}/comments`

成功状态 `201 Created`。支持 1.1 节的可选 `Idempotency-Key`。请求体：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `content` | body | string | 是 | 无 | 1~500 字符 | 评论内容 |
| `parent_id` | body | integer/null | 否 | `null` | 非空时 `>=1`，且必须属于同一动态 | 父评论 ID；空值表示一级评论 |

请求示例：

```json
{"content":"这个展览看起来很不错","parent_id":null}
```

回复示例：

```json
{"content":"我也很喜欢这个主题","parent_id":201}
```

返回一个 `CommunityCommentResponse`，字段与 3.1 相同。动态不存在或父评论不存在返回 `404`；正文为空、超过 500 字符或 `parent_id` 非法返回 `422`。

### 3.3 删除评论

#### `DELETE /api/v1/community/comments/{comment_id}`

请求体无，成功状态 `204 No Content`。仅评论作者可以删除自己的有效评论，服务端软删除并将动态评论数减一。重复删除或删除他人评论返回 `404`。

## 4. 纸飞机

### 4.1 发送纸飞机

#### `POST /api/v1/paper-planes`

成功状态 `201 Created`。支持 1.1 节的可选 `Idempotency-Key`。当前使用 Redis Lua `EVAL` 在一次原子操作内完成自然日计数、首次过期时间和超限回滚：普通用户每天最多 3 次，UTC 次日零点重置；Redis 不可用时返回 `503`。每条纸飞机默认有效 24 小时，数据库写入失败会退还已扣额度；如果额度退还本身不可用，服务端记录日志并保留原数据库错误，不用退款错误覆盖根因。

请求字段：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `content` | body | string | 条件 | `""` | 最长 1000；与媒体至少一个非空 | 纸飞机正文；允许仅图片无正文 |
| `image_media_ids` | body | array[integer] | 否 | `[]` | 最多 6 个；元素 `>=1` | 优先：社区媒体上传得到的图片 ID（`purpose=paper_plane`） |
| `images` | body | array[string] | 否 | `[]` | 最多 6 个；过渡期 URL | 兼容旧客户端：须为本人已上传 ready 的 community storage URL |
| `city` | body | string/null | 否 | `null` | 最长 64 字符 | 展示城市 |
| `tags` | body | array[string] | 否 | `[]` | 最多 5 个标签 | 纸飞机标签 |
| `is_anonymous` | body | boolean | 否 | `true` | 布尔值 | 是否匿名展示 |

说明：纸飞机**不支持视频**（无 `video` / `video_media_id` 字段）；上传时 `purpose=paper_plane` 且文件为视频会返回 `422`。

请求示例：

```json
{
  "content":"想认识同样喜欢旅行的人",
  "image_media_ids":[9],
  "images":[],
  "city":"杭州",
  "tags":["旅行","交友"],
  "is_anonymous":true
}
```

返回字段：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 纸飞机 ID |
| `content` | string | 是 | 不为空 | 正文 |
| `images` | array[string] | 是 | 无图片为 `[]` | 图片地址 |
| `city` | string/null | 是 | 未填写为 `null` | 城市 |
| `tags` | array[string] | 是 | 无标签为 `[]` | 标签 |
| `is_anonymous` | boolean | 是 | 不为空 | 是否匿名 |
| `reply_count` | integer | 是 | 无回复为 `0` | 回复数 |
| `created_at` | datetime | 是 | 不为空 | 创建时间 |

### 4.2 捡取纸飞机

#### `GET /api/v1/paper-planes`

查询参数：`page` 默认 `1`、范围 `1~1000`；`page_size` 默认 `20`、范围 `1~50`。成功状态 `200 OK`，按创建时间倒序返回数组。结果排除自己的纸飞机、已过期/非有效纸飞机，以及当前用户已经回复过的纸飞机。

无数据返回 `[]`。当前响应不返回发送者 `user_id`；如果产品需要查看非匿名发送者，需要新增兼容字段并同步隐私规则。

### 4.3 查看我的纸飞机

#### `GET /api/v1/paper-planes/mine`

查询参数与 4.2 相同。成功状态 `200 OK`，只返回当前用户创建且仍未删除的有效纸飞机；返回数组，无数据时为 `[]`。

### 4.4 回复纸飞机

#### `POST /api/v1/paper-planes/{plane_id}/replies`

路径参数 `plane_id>=1`，成功状态 `201 Created`。支持 1.1 节的可选 `Idempotency-Key`，且幂等载荷包含 `plane_id`。请求字段：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `content` | body | string | 是 | 无 | 1~1000 字符 | 回复正文 |
| `is_anonymous` | body | boolean | 否 | `true` | 布尔值 | 是否匿名回复 |

请求示例：

```json
{"content":"我也喜欢旅行，可以认识一下","is_anonymous":true}
```

返回字段：

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `id` | integer | 是 | 回复 ID |
| `plane_id` | integer | 是 | 纸飞机 ID |
| `user_id` | integer | 是 | 回复者 ID |
| `content` | string | 是 | 回复正文 |
| `is_anonymous` | boolean | 是 | 是否匿名 |
| `created_at` | datetime | 是 | 创建时间 |

纸飞机不存在、已过期或状态不可回复返回 `404`；不能回复自己的纸飞机，返回：

```json
{"detail":"不能回复自己的纸飞机"}
```

每条纸飞机回复数达到 5 条后状态变为已回应，不再出现在可捡列表中。

## 5. 错误响应

| HTTP | 触发条件 | 示例 detail | 前端处理 |
| --- | --- | --- | --- |
| `401` | 未登录或会话失效 | `请先登录` | 清理 Token 并登录 |
| `403` | 未绑定手机号 | `请先绑定手机号` | 跳转手机号绑定 |
| `403` | 社区写操作的账号未通过实名认证 | `请先完成实名认证` | 引导完成实名认证；浏览能力不受影响 |
| `409` | 相同幂等 key 正在处理 | `相同请求正在处理中` | 保留原 key 和原载荷，稍后重试 |
| `409` | 相同幂等 key 改变了请求载荷 | `Idempotency-Key 已用于不同请求` | 为新的创建意图生成新 key |
| `404` | 动态、评论、纸飞机或父评论不存在 | `纸飞机不存在或已过期` | 刷新当前列表 |
| `422` | 长度、类型、范围、枚举不合法 | `Field required` | 修正请求参数 |
| `429` | 当日纸飞机额度用完 | `今日纸飞机次数已用完` | 显示次日可用或会员提示 |
| `503` | Redis 未配置或暂时不可用 | `Redis服务未配置或暂时不可用` | 稍后重试，不重复提交 |

## 6. 动态详情 / 收藏 / 扩展流

### 6.1 查看动态详情

#### `GET /api/v1/community/posts/{post_id}`

路径参数 `post_id>=1`。成功 `200 OK`，返回完整 `CommunityPostResponse`。动态不存在、被屏蔽或隐私不可见返回 `404`。

### 6.2 动态流扩展参数

#### `GET /api/v1/community/posts`

在原有 `mode=latest|following` 基础上新增：

| 参数 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `mode` | query | string | 否 | `latest` | `latest` / `following` / `city` / `liked_users` / `following_and_liked` / `mine` | 最新、关注、同城、喜欢用户动态、关注∪喜欢并集、我的动态 |
| `city` | query | string/null | 否 | `null` | 最长 64；`mode=city` 展示名兼容 | 同城城市名 |
| `city_code` | query | string/null | 否 | `null` | 市一级 6 位码（如 `330100`）；短码 4 位右补 `00` | 同城主键，优先于 name |
| `filter` | query | string/null | 否 | `null` | `all` / `mbti` / `alumni` / `hometown` / `hot` / `latest` | 发现页二级筛选或热度 |
| `sort` | query | string | 否 | `latest` | `latest` / `hot` | 排序；`filter=hot` 时按热度 |

`mode=city` 时：**只按帖子发布地 `p.location`** 命中（等值或 `city%` 前缀）；**不**用作者 `residence` / `residence_city_code`。
锚点解析（仅决定筛哪个市名，不参与 OR）：请求 `city`/`city_code` → 同城浏览偏好 `community_city_*` → 仍无则 `422`。不回落到资料现居 `residence_*`。字面量 `city=未设置` → `422`。
发现页 `same_city` 仍只看资料 `residence_city_code`，与同城偏好无关。

`mode=following_and_liked`：作者在 `user_favorite` 且 `user_id=me` 且 `type IN (1, 3)`（喜欢用户 ∪ 关注），服务端去重 + COUNT 分页。关注 Tab「全部」应对接此 mode，禁止客户端双请求假并集。

`mode=mine` 仅返回当前用户作为作者的动态，不接受外部作者 ID。

`mode=mine`：仅返回当前登录用户作为作者的动态，服务端追加 `p.user_id = 当前用户` 条件，并继续应用状态、审核、删除、隐私、拉黑和分页规则。该模式不接受额外作者 ID，避免通过查询参数越权读取其他用户动态。

动态流排序的最终稳定键为 `p.id DESC`：同置顶状态、同热度、同创建时间的动态仍保持确定顺序，翻页时不会因数据库非确定排序导致重复或遗漏。

### 6.3 收藏 / 取消收藏

#### `PUT /api/v1/community/posts/{post_id}/collect`

#### `DELETE /api/v1/community/posts/{post_id}/collect`

请求体无。成功 `200 OK`。收藏复用 `community_like.type=3`（`1` 动态点赞、`2` 评论点赞、`3` 动态收藏）。

返回：

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `id` | integer | 是 | 动态 ID |
| `is_collected` | boolean | 是 | 当前用户是否已收藏 |
| `collect_count` | integer | 是 | 收藏总数 |

### 6.4 `CommunityPostResponse` 新增字段（向后兼容）

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `topic_id` | integer/null | 是 | 未绑定话题为 `null` | 话题 ID |
| `topic_name` | string/null | 是 | 无话题为 `null` | 话题名 |
| `collect_count` | integer | 是 | 无收藏为 `0` | 收藏数 |
| `is_collected` | boolean | 是 | 不为空 | 是否已收藏 |
| `is_followed` | boolean | 是 | 不为空 | 是否已关注作者 |
| `gender` | integer/null | 是 | 未设置为 `null` | 作者性别 1男 2女 |
| `age` | integer/null | 是 | 无生日为 `null` | 作者年龄 |
| `mbti` | string/null | 是 | 未填写为 `null` | 作者 MBTI |
| `school` | string/null | 是 | 未填写为 `null` | 学校 |
| `hometown` | string/null | 是 | 未填写为 `null` | 家乡 |
| `residence` | string/null | 是 | 未填写为 `null` | 现居地 |

旧客户端可忽略新增字段。

## 7. 话题

### 7.1 话题列表

#### `GET /api/v1/community/topics`

| 参数 | 位置 | 类型 | 必填 | 默认 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `sort` | query | string | 否 | `hot` | `hot` / `latest` | 热度或最新 |
| `page` | query | integer | 否 | `1` | 1~1000 | 页码 |
| `page_size` | query | integer | 否 | `50` | 1~100 | 每页数量 |

成功 `200 OK`，直接返回 `CommunityTopicResponse[]`。

### 7.2 分页话题列表

#### `GET /api/v1/community/topics/page`

参数同 7.1，另支持 `exclude_ids`（可重复 query，整数数组）。返回：

```json
{"items":[],"page":1,"page_size":20,"total":0}
```

### 7.3 话题元信息 / 详情

#### `GET /api/v1/community/topics/{topic_id}`

返回单个 `CommunityTopicResponse`。

#### `GET /api/v1/community/topics/{topic_id}/detail`

| 参数 | 位置 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- | --- |
| `sort` | query | string | `hot` | 话题下动态排序 `hot`/`latest` |
| `page` | query | integer | `1` | 动态页码 |
| `page_size` | query | integer | `20` | 动态每页数量 |

返回：

```json
{"topic":{"id":1,"name":"树洞","icon":null,"sort":0,"post_count":3,"participant_count":2,"heat":23,"joined":false,"created_at":"2026-07-20T12:00:00"},"posts":{"items":[],"page":1,"page_size":20,"total":0},"sort":"hot"}
```

`posts` 是遵循动态可见性规则的分页对象：`items` 为当前页动态，`page` 和 `page_size` 回显请求值，`total` 为可见动态总数。`joined` 表示当前用户是否在该话题下发布过动态（无独立参与表）。

### 7.4 参与话题

#### `POST /api/v1/community/topics/{topic_id}/join`

请求体无。成功：

```json
{"success":true,"joined":true,"topic_id":1}
```

幂等；话题不存在 `404`。

`CommunityTopicResponse` 字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | integer | 话题 ID |
| `name` | string | 话题名 |
| `icon` | string/null | 图标 |
| `sort` | integer | 运营排序 |
| `post_count` | integer | 动态数 |
| `participant_count` | integer | 发帖用户去重数 |
| `heat` | integer | `participant_count*10 + post_count` |
| `joined` | boolean | 当前用户是否参与 |
| `created_at` | datetime/null | 创建时间 |
| `background_url` | string/null | 兼容字段；当前版本未持久化背景配置时为 `null` |
| `description` | string/null | 兼容字段；当前版本未持久化话题简介时为 `null` |
| `view_count` | integer | 话题浏览量；当前版本未持久化统计时为 `0` |
| `status` | integer/string/null | 兼容字段；当前版本沿用 `is_active` 查询且未单独返回状态时为 `null` |

## 8. 线下活动

### 8.1 活动列表

#### `GET /api/v1/community/activities`

| 参数 | 位置 | 类型 | 默认 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `filter` | query | string | `all` | `all` / `recruiting` / `mine` | 全部 / 招募中 / 我已报名 |
| `page` | query | integer | `1` | 1~1000 | 页码 |
| `page_size` | query | integer | `20` | 1~50 | 每页 |

返回 `ActivityPage`：`items/page/page_size/total`。

### 8.2 我的活动

#### `GET /api/v1/community/activities/mine`

`filter`：`all` / `pending` / `joined` / `ended`。

### 8.3 活动详情

#### `GET /api/v1/community/activities/{activity_id}`

成功返回 `ActivityResponse`。`address` 仅在报名成功（`my_status=1`）时返回，否则 `null`。

### 8.4 报名活动

#### `POST /api/v1/community/activities/{activity_id}/signup`

成功 `201 Created`。请求体可选：

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `real_name` | string/null | 否 | 最长 64 | 真实姓名 |
| `phone` | string/null | 否 | 最长 20 | 联系电话 |
| `remark` | string/null | 否 | 最长 255 | 备注 |

示例：

```json
{"real_name":"张三","phone":"13800000000","remark":"可周末参加"}
```

成功：

```json
{"success":true,"activity_id":1,"my_status":0,"my_status_text":"pending","message":"报名已提交，审核通过后告知集合信息"}
```

活动不可报名/截止/满员 `422`；不存在 `404`；已报名幂等返回当前状态。

`ActivityResponse` 关键字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | integer | 活动 ID |
| `title` | string | 标题 |
| `cover` | string/null | 封面 |
| `type` | string/null | 活动类型 |
| `city` | string/null | 城市 |
| `address` | string/null | 地址（仅报名成功可见） |
| `start_time` / `end_time` | datetime | 起止时间 |
| `signup_deadline` | datetime/null | 报名截止 |
| `max_people` / `current_people` | integer | 人数上限 / 已报名 |
| `price` | number | 报名费 |
| `status` | integer | 1招募中 2已满 3进行中 4已结束 5已取消 |
| `status_text` | string | recruiting/full/ongoing/ended/cancelled |
| `description` | string/null | 详情 |
| `my_status` | integer/null | 0待审 1成功 2取消 3拒绝；未报名为 `null` |
| `my_status_text` | string | pending/joined/cancelled/rejected/none |
| `created_at` | datetime | 创建时间 |

## 9. Banner / 额度 / 城市 / 举报原因

### 9.1 Banner

#### `GET /api/v1/community/banners`

| 参数 | 位置 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- | --- |
| `position` | query | string | `community` | Banner 位置，对应 `config_banner.position` |

返回数组，字段：`id/title/image_url/link_type/link_value/sort/position`。无数据 `[]`。

### 9.2 日额度

#### `GET /api/v1/community/quotas`

返回：

```json
{
  "apply_daily":{"total":3,"used":1,"remain":2,"points_available":false,"points_cost":20},
  "paper_plane_daily":{"total":3,"used":0,"remain":3,"points_available":false,"points_cost":10}
}
```

`apply_daily` 读取发现申请 Redis 键 `discovery:apply:{user_id}:{date}`；会员使用 `apply_daily_vip_limit`。纸飞机读取 `paper-plane:{user_id}:{date}`，上限 3。Redis 不可用 `503`。积分加次写路径尚未实现，因此两个额度项的 `points_available` 当前固定为 `false`；`points_cost` 仅供展示，客户端不得据此发起加次。

### 9.3 同城城市

#### `GET /api/v1/community/city`

#### `PUT /api/v1/community/city`

PUT 请求体：

```json
{"name":"南京","code":"320100"}
```

读写 **同城浏览偏好**（独立字段，不污染资料现居）：

| 列 | 含义 |
| --- | --- |
| `user_profile.community_city_name` | 浏览城市展示名 |
| `user_profile.community_city_code` | 市一级 6 位码 |
| `user_profile.community_city_updated_at` | 上次变更时间（UTC） |

- `code` 可选；提供时必须恰好为 4 或 6 位 ASCII 数字。4 位短码规范化为末尾补 `00` 的 6 位市码；仅有 name 时反查常用市表。
- `PUT` 拒空名与字面量「未设置」→ **422**。
- **同城无变化**（同 name/code）→ **200**，不刷新 `updated_at`。
- **一周限改**（默认 7 天，`community_city_cooldown_days`）：换城且距上次变更不足冷却 → **429**，文案含下次可改日期。
- **不得**写入 `residence` / `residence_*_code`。
- 成功返回例如 `{"name":"南京","code":"320100"}`。区级本期不暴露。

### 9.4 举报原因

#### `GET /api/v1/community/report-reasons`

返回固定枚举：

```json
[
  {"id":"harass","label":"骚扰或不适内容"},
  {"id":"fake","label":"虚假资料或冒充"},
  {"id":"ad","label":"广告或引流"},
  {"id":"other","label":"其他安全问题"}
]
```

### 9.5 提交社区内容举报

#### `POST /api/v1/community/reports`

- 权限：已登录且已绑定手机号（`get_verified_user`）
- Content-Type：`application/json`
- 成功状态：`201 Created`

请求字段：

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|------|
| `target_type` | body | string | 是 | 无 | 枚举 `post` / `comment` / `paper_plane` | 举报对象类型 |
| `target_id` | body | int | 是 | 无 | ≥ 1 | 对象主键 |
| `reason_id` | body | string | 是 | 无 | 长度 1–64，须来自 `GET /community/report-reasons` | 举报原因 |
| `description` | body | string | 否 | `null` | 最长 1000 | 补充说明 |
| `images` | body | string[] | 否 | `[]` | 最多 6 张，须为本人已上传的 `/storage/uploads/` 路径 | 佐证图片 |

请求示例：

```json
{
  "target_type": "post",
  "target_id": 1024,
  "reason_id": "harass",
  "description": "内容包含骚扰信息",
  "images": []
}
```

返回字段：

| 字段 | 类型 | 必返 | 说明 |
|------|------|------|------|
| `id` | int | 是 | 举报记录 ID |
| `target_type` | string | 是 | `post` / `comment` / `paper_plane` / `user` |
| `target_id` | int \| null | 是 | 被举报对象 ID |
| `target_user_id` | int | 是 | 被举报内容的作者 ID |
| `type` | string | 是 | 举报原因，等于请求的 `reason_id` |
| `status` | int | 是 | `0` 待处理 / `1` 已处理 / `2` 已驳回 |
| `created_at` | datetime | 是 | 提交时间 |

成功响应示例：

```json
{
  "id": 55,
  "target_type": "post",
  "target_id": 1024,
  "target_user_id": 88,
  "type": "harass",
  "status": 0,
  "created_at": "2026-07-26T10:00:00"
}
```

错误契约：

| 状态码 | 触发条件 | 前端处理建议 |
|--------|----------|--------------|
| `404` | 目标动态/评论/纸飞机不存在 | 提示内容已删除并刷新列表 |
| `409` | 该用户对同一目标已有待处理举报 | 提示「已举报，请等待处理结果」 |
| `422` | `reason_id` 不在枚举内、举报自己的内容、图片非本人上传 | 按 `detail` 提示 |

> 举报「用户」仍走社交接口 `POST /api/v1/security/reports/{target_id}`；本接口只处理社区内容。

## 11. 社区媒体上传

社区动态与纸飞机的图文/短视频统一走本节接口（与通用语音上传 `POST /api/v1/media/uploads` 不同）。推荐流程：先上传拿 `media_id` → 再在发布接口传入 id → 服务端绑定。

### 11.1 上传媒体

#### `POST /api/v1/community/media/uploads`

- Content-Type：`multipart/form-data`（不要用 `application/json`）
- 权限：已登录、绑定手机号且 `realname_status == 2`（`get_realname_verified_user`）
- 成功状态：`201 Created`

请求字段：

| 字段 | 位置 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| `file` | form | file | 是 | 无 | 见下方类型与大小 | 媒体文件本体 |
| `purpose` | form | string | 是 | 无 | `post` 或 `paper_plane` | 用途；发布时 resolve 必须与用途一致 |

类型与限制：

| 类型 | 判定 | 大小上限 | 其他 |
| --- | --- | --- | --- |
| 图片 | 非视频路径；真实解码 JPEG/PNG/WEBP | 5MB；像素 ≤ 2500 万 | 服务端转存 WebP + 缩略图 |
| 视频 | `Content-Type` 以 `video/` 开头或文件名以 `.mp4` 结尾 | 50MB | 仅 MP4；时长 1~30 秒；依赖本机 `ffprobe` |

成功响应 `CommunityMediaResponse`：

| 字段 | 类型 | 必返 | 空值含义 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | integer | 是 | 不为空 | 媒体 ID（发布时使用） |
| `purpose` | string | 是 | 不为空 | `post` / `paper_plane` |
| `media_type` | string | 是 | 不为空 | `image` / `video` |
| `url` | string | 是 | 不为空 | 可访问 storage 路径 |
| `thumbnail_url` | string/null | 是 | 视频为 `null` | 缩略图路径 |
| `file_size` | integer/null | 是 | 可能为 0 | 字节数 |
| `duration_seconds` | integer/null | 是 | 图片为 `null` | 视频时长（秒，向上取整） |
| `status` | string | 是 | 不为空 | 初始恒为 `ready` |

请求示例（multipart 概念）：

```http
POST /api/v1/community/media/uploads HTTP/1.1
Authorization: Bearer <access_token>
Content-Type: multipart/form-data; boundary=----bound

------bound
Content-Disposition: form-data; name="purpose"

post
------bound
Content-Disposition: form-data; name="file"; filename="a.jpg"
Content-Type: image/jpeg

<binary>
------bound--
```

成功响应示例：

```json
{
  "id": 42,
  "purpose": "post",
  "media_type": "image",
  "url": "/storage/uploads/7/community/a.webp",
  "thumbnail_url": "/storage/uploads/7/community/a-thumb.webp",
  "file_size": 1280,
  "duration_seconds": null,
  "status": "ready"
}
```

错误码：

| 状态 | 场景 | 示例 `detail` | 客户端建议 |
| --- | --- | --- | --- |
| `401` | 未登录 / token 无效 | （鉴权中间件） | 重新登录 |
| `403` | 未实名通过 | 请先完成实名认证 | 引导实名 |
| `413` | 图片/视频过大或像素超限 | `文件大小不能超过5MB` / `视频大小不能超过50MB` | 压缩后重试 |
| `415` | 伪造成像/非支持格式/非 MP4 视频 | `图片内容无法识别` / `仅支持MP4视频` | 换真实 JPG/PNG/WEBP 或 MP4 |
| `422` | 空文件、非法 purpose、纸飞机传视频、时长超限等 | `purpose 仅支持 post 或 paper_plane` / `纸飞机不支持视频` / `视频时长不能超过30秒` | 修正参数 |
| `503` | 视频路径但本机无 `ffprobe` | `视频处理服务未配置，请安装ffprobe` | 运维安装 ffprobe |

### 11.2 删除未绑定媒体

#### `DELETE /api/v1/community/media/{media_id}`

- 权限：已登录 + 实名；仅所有者
- 成功：`204 No Content`（无响应体）
- `404`：媒体不存在或不属于当前用户 / 已删除
- `409`：`status=bound` 已绑定动态或纸飞机，不可删（`媒体已绑定内容，无法删除`）

路径参数：

| 参数 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `media_id` | path | integer | 是 | `>=1` | 媒体 ID |

### 11.3 生命周期与清理

| `status` | 含义 |
| --- | --- |
| `ready` | 已上传、未绑定；可删除；可在发布时绑定 |
| `bound` | 已绑定到动态或纸飞机；不可主动删除 |
| `deleted` | 软删除（用户删除或过期清理） |

流程：

1. 上传成功 → `ready`，写入 `expire_at = now + 24h`（`UNBOUND_TTL_HOURS=24`）。
2. `POST /community/posts` 或 `POST /paper-planes` 使用对应 id（或过渡 URL）成功 → `ready` → `bound`，并写入 `community_media_attachment`。
3. 未绑定且超过 `expire_at` 的 `ready` 记录，由服务端函数 `cleanup_expired_unbound_media(db, limit=100)` 批量标为 `deleted` 并尽量删除磁盘文件。
4. 清理入口：当前为**可手动/定时调用的服务函数**（非对外 HTTP 接口）；运维或后台任务按需调用即可。删除动态时，已绑定媒体会随帖一并标为 `deleted`。

### 11.4 与发布接口的关系（变更摘要）

- 动态：新增 `image_media_ids`、`video_media_id`；`content` 允许 media-only（空字符串 + 媒体）。
- 纸飞机：新增 `image_media_ids`（最多 6）；无视频字段。
- 旧 `images` / `video`（动态）与纸飞机 `images`：过渡期仍接受，但必须是本人 `community_media` 的 ready storage URL；外链、`wxfile://`、本地盘符等一律 `422`。
- 响应模型仍返回 URL 列表（`images`/`video`），前端展示不必改；新客户端应以上传 id 为准。


## 12. 纸飞机匿名会话

回复纸飞机（§4.4）成功后会返回 `conversation_id`，后续匿名对话走本节接口。会话双方分别是纸飞机作者（`owner_id`）与回复者（`replier_id`），对彼此仅展示 `peer_label`，不暴露昵称与头像。

### 12.1 会话列表

#### `GET /api/v1/paper-plane-conversations`

- 权限：已登录且已绑定手机号

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|------|------|------|------|------|------|
| `page` | query | int | 否 | 1 | 1–1000 |
| `page_size` | query | int | 否 | 20 | 1–50 |

返回 `PaperPlaneConversationResponse` 数组（无 `total` 字段）：

| 字段 | 类型 | 必返 | 说明 |
|------|------|------|------|
| `id` | int | 是 | 会话 ID |
| `plane_id` | int | 是 | 关联纸飞机 ID |
| `owner_id` | int | 是 | 纸飞机作者 ID |
| `replier_id` | int | 是 | 回复者 ID |
| `status` | int | 是 | `1` 进行中 / `2` 已结束 |
| `last_message` | string \| null | 是 | 最后一条消息预览（截断 200 字符） |
| `last_message_at` | datetime \| null | 是 | 最后消息时间 |
| `unread_count` | int | 是 | 当前调用者的未读数 |
| `plane_content` | string \| null | 是 | 纸飞机正文摘要 |
| `peer_label` | string | 是 | 对端展示名：作者视角为「匿名回复者」，回复者视角为「纸飞机主人」 |
| `created_at` | datetime | 是 | 会话创建时间 |

无数据时返回 `[]`。

### 12.2 会话消息

#### `GET /api/v1/paper-plane-conversations/{conversation_id}/messages`

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 |
|------|------|------|------|------|------|
| `conversation_id` | path | int | 是 | 无 | ≥ 1 |
| `page` | query | int | 否 | 1 | 1–1000 |
| `page_size` | query | int | 否 | 50 | 1–100 |

按 `created_at DESC, id DESC` 倒序返回 `PaperPlaneMessageResponse` 数组：

| 字段 | 类型 | 必返 | 说明 |
|------|------|------|------|
| `id` | int | 是 | 消息 ID |
| `conversation_id` | int | 是 | 所属会话 |
| `from_user_id` | int | 是 | 发送者 ID |
| `mine` | bool | 是 | 是否为当前请求用户发送的消息 |
| `content` | string | 是 | 文本内容，语音消息为空串 |
| `type` | int | 是 | `1` 文本 / `3` 语音 |
| `media_url` | string \| null | 是 | 语音文件地址，仅 `/storage/uploads/` 内部路径 |
| `voice_duration_sec` | int \| null | 是 | 语音时长（秒） |
| `created_at` | datetime | 是 | 发送时间 |

错误：`404` 会话不存在；`403` 非会话参与方。

### 12.3 发送会话消息

#### `POST /api/v1/paper-plane-conversations/{conversation_id}/messages`

- 成功状态：`201 Created`
- 支持 `Idempotency-Key` 请求头（见 §1.1）

| 参数 | 位置 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|------|
| `conversation_id` | path | int | 是 | 无 | ≥ 1 | 会话 ID |
| `content` | body | string | 条件必填 | `""` | 最长 1000 | `type=1` 时不能为空 |
| `type` | body | int | 否 | `1` | 枚举 `1` / `3` | 消息类型 |
| `media_url` | body | string \| null | 条件必填 | `null` | 最长 500，**必须以 `/storage/uploads/` 开头** | `type=3` 时必填 |
| `voice_duration_sec` | body | int \| null | 条件必填 | `null` | 1–60 | `type=3` 时必填 |

请求示例（文本）：

```json
{ "content": "你好呀", "type": 1 }
```

返回体同 §12.2 的单条 `PaperPlaneMessageResponse`。

错误契约：

| 状态码 | 触发条件 |
|--------|----------|
| `403` | 非会话参与方 |
| `404` | 会话不存在 |
| `422` | 会话已结束（`status=2`）、文本为空、语音缺 `media_url`/时长、`media_url` 非内部路径、命中敏感词 |

### 12.4 标记已读

#### `POST /api/v1/paper-plane-conversations/{conversation_id}/read`

无请求体。将调用方视角的未读数清零，返回更新后的 `PaperPlaneConversationResponse`（字段同 §12.1）。

### 12.5 结束会话

#### `POST /api/v1/paper-plane-conversations/{conversation_id}/end`

无请求体。权限需 `realname_status == 2`。将会话置为 `status=2`，之后双方均不可再发消息（发送将返回 `422 对话已结束`）。返回更新后的 `PaperPlaneConversationResponse`。

## 13. 与社交 / 发现模块的协作

社区前端还需对接已有社交与发现接口（不在本文件重复定义完整契约，见 `docs/api/social.md`、`docs/api/discovery.md`）：

| 能力 | 方法 | 路径 | 前端对接状态（2026-07-25） |
| --- | --- | --- | --- |
| 关注用户 | PUT | `/api/v1/users/{target_id}/follow` | 已接 `followUserFromCommunity` |
| 取消关注 | DELETE | `/api/v1/users/{target_id}/follow` | 已接 `unfollowUserFromCommunity` |
| 喜欢用户 | PUT/DELETE | `/api/v1/users/{target_id}/like` | 已接 `likeUser`（影响 `mode=liked_users`） |
| 我的喜欢列表 | GET | `/api/v1/relations/likes` | `likeUser` 用于判断当前状态 |
| 申请认识 | POST | `/api/v1/discovery/applications/{target_id}` | 已接 `applyToMeet` |
| 拉黑 | PUT | `/api/v1/security/blocks/{target_id}` | 已接 `blockUser` |
| 举报 | POST | `/api/v1/security/reports/{target_id}` | 已接 `reportContent`（target=用户 id） |
| 通知列表 | GET | `/api/v1/notifications` | 已接 |
| 通知已读 | POST | `/api/v1/notifications/{id}/read` | 已接 |
| 全部已读 | POST | `/api/v1/notifications/read-all` | 已接 |

说明：关注 Tab「全部」对接 `mode=following_and_liked`（`type IN (1,3)` 真分页）。额度读接口 `GET /community/quotas` 与 discovery 申请扣次共用 UTC 日键；`points_available` 在积分加次写路径落地前为 false。

## 14. 当前边界与变更记录

### 2026-07-26（社区模块安全与一致性修复）

**修改接口 / 行为变更：**

- `POST /api/v1/paper-planes`：`voice_url` 变更前仅校验长度 500，可传入任意外部 URL；变更后必须以 `/storage/uploads/` 开头，否则 `422`。**Breaking**：此前若有客户端直传外链，需改为先调 `POST /api/v1/media/uploads` 拿内部路径。
- `POST /api/v1/paper-plane-conversations/{id}/messages`：`media_url` 同上新增内部路径校验；同时该接口**新增敏感词过滤**，`type=1` 的文本命中词库将返回 `422`（此前该接口是社区唯一无内容过滤的写入口）。
- `POST /api/v1/community/reports`：新增重复举报拦截，同一用户对同一目标存在待处理举报时返回 `409`（此前可无限重复提交）。
- 未实名用户访问社区写接口时的 `403` 提示由乱码字符串修正为「请先完成实名认证」，前端若按文案匹配需同步更新。

**内部修复（不改契约）：**

- `community_post.comment_count` 由增量式 `+1 / -1` 改为按 `status=1` 重算，与 `like_count` 策略统一，消除并发与回滚下的计数漂移。
- 敏感词缓存判定由 `_cached_words` 改为独立的 `_cache_loaded` 标志，修复词库为空时每次调用都穿透查库的问题。
- `community_post` 新增复合索引 `idx_status_top_created(status, is_top, created_at)` 与 `idx_status_like_created(status, like_count, created_at)`，覆盖动态流最新/热门两条排序路径；已有库由 `_ensure_community_post_feed_indexes` 幂等补齐。

**文档补齐：** 本次补充 §9.5 提交举报、§10 纸飞机匿名会话（列表/消息/发送/已读/结束）共 5 个此前实现已存在但文档缺失的接口。

**测试：** `tests/test_community_features.py` 的纸飞机退款用例此前因 `assert_text_allowed` 提前消耗 ScriptedSession 结果而误判通过路径，已修正预置序列；`tests/test_community_media.py` 的路由注册用例此前断言 `app.routes`（只含顶层路由）恒为假，已改为断言 `community.router.routes`。全量 129 passed。

### 2026-07-25: Community contract reconciliation

- **城市码校验：** 变更前，`PUT /community/city` 的 body `code` 和动态流 `city_code` query 仅受长度约束，错误长度、非 ASCII 数字或混合字符可能进入服务层。变更后，两个 API 边界都只接受 4 或 6 位 ASCII 数字；4 位短码统一补 `00` 后作为 6 位市级码使用，且同时传 `city` 时城市码优先。影响：合法 4/6 位客户端保持兼容，发送畸形城市码的客户端改为收到 `422` 并应修正请求。
- **动态流与话题详情：** 变更前，主章节只列出两个 feed mode，且话题详情示例把 `posts` 写为数组。变更后，主章节列出全部五个公开 mode，`posts` 明确为带 `items/page/page_size/total` 的分页对象。影响：客户端应按分页对象读取话题动态，不能把 `posts` 当数组。
- **额度积分状态：** 变更前，主章节示例误称 `points_available=true`。变更后，该字段准确反映当前未接入积分加次写路径的 `false`。影响：客户端不得据 `points_cost` 单独开放加次入口。

### 2026-07-25: Community create idempotency and atomic quotas

An in-flight idempotency reservation has a five-minute lease measured with the
MySQL UTC clock. A retry during that lease returns `409`; after five minutes, a
retry with the same case-sensitive key and payload may take over the stale
reservation. The displaced owner cannot complete or abort the new reservation.

- 变更前：四条创建接口没有通用幂等 Header；并发重试可能重复写入。变更后：新增可选、长度 `8..128` 的 `Idempotency-Key`，完成请求重放首次响应，载荷冲突和处理中请求返回 `409`。
- 变更前：日额度依次调用 Redis `INCR`、`EXPIRE`，中途失败可能留下没有正确到期时间的计数。变更后：一次 Lua `EVAL` 原子执行计数、首次过期和超限回滚，UTC reset TTL 仅计算一次。
- 兼容性：Header 可选，URL、Method、Body、成功响应模型和实名门禁均不变；未传 Header 的旧客户端无需迁移。新客户端应为一次创建意图生成稳定 key，并只在创建意图或载荷变化时换 key。

### 2026-07-25: Community data-contract hardening

The following rules supersede every earlier statement in this document that used
`residence` or `residence_city_code` as a community-city feed fallback.

#### Post visibility and declaration

`POST /api/v1/community/posts` accepts these additional fields. Both are also
returned by `CommunityPostResponse` together with `realname_status`.

| Field | Location | Type | Required | Allowed values | Meaning |
| --- | --- | --- | --- | --- | --- |
| `visibility` | body / response | integer | no | `0`, `1`, `2`; default `0` | `0` public, `1` friends-only, `2` self-only |
| `declaration` | body / response | string | no | `""`, `"内容包含虚构演绎"`, `"内容包含广告推广"`, `"内容可能引起不适"`; default `""` | Content declaration selected by the author |
| `realname_status` | response | integer | yes | canonical `user_auth.realname_status`, default `0` | Author real-name verification status |

Example:

```json
{
  "content": "周末读书会招募",
  "location": "南京",
  "visibility": 1,
  "declaration": "内容包含广告推广"
}
```

Visibility is enforced identically for post detail, post feeds, feed totals, and
comments (comments first delegate to post detail): authors can read their own
active posts; public posts require `show_posts`; friends-only posts require two
active `user_match` rows, one in each direction; self-only posts never leave
their author. A hidden post returns `404` rather than disclosing its existence.

#### City feed anchor

For `GET /api/v1/community/posts?mode=city`, the only accepted anchors are the
request's `city` / `city_code` or `user_profile.community_city_name` /
`community_city_code`. Matches are made only against `community_post.location`.
There is no `residence` or `residence_city_code` fallback. When neither request
nor community preference supplies a usable city, the API returns `422`.

#### Topic detail pagination

`GET /api/v1/community/topics/{topic_id}/detail` returns the complete post page:

```json
{
  "topic": {"id": 1, "name": "树洞", "icon": null, "sort": 0, "post_count": 23, "participant_count": 10, "heat": 123, "joined": false},
  "posts": {"items": [], "page": 2, "page_size": 10, "total": 23},
  "sort": "latest"
}
```

`posts.items`, `posts.page`, `posts.page_size`, and `posts.total` are always
present. `page` and `page_size` reflect the request, while `total` is the count
after the same visibility rules used by the feed.

#### Activity signup contact and capacity

`POST /api/v1/community/activities/{activity_id}/signup` accepts only `remark`
as client-controlled signup data. The service locks the activity row, counts
active signups (`pending` and `joined`) inside that transaction, rejects a full
activity with `422`, then writes transaction-local `current_people`.

| Field | Request compatibility | Stored source | Meaning |
| --- | --- | --- | --- |
| `real_name` | accepted and ignored | `user_auth.real_name` | Canonical verified account name |
| `phone` | accepted and ignored | `users.phone` | Canonical account phone |
| `remark` | accepted | request body | Optional attendee note, maximum 255 characters |

Legacy clients may continue sending all three fields without a breaking change:

```json
{"real_name":"旧客户端姓名","phone":"13800000000","remark":"可周末参加"}
```

The stored name and phone are nevertheless taken from the canonical account
records, not these request values.

当前未提供：媒体内容流式审核、敏感词三态（替换*/进审）与词库管理 CRUD、纸飞机语音、纸飞机回复自动转私信。评论点赞接口已提供（见评论点赞契约）；话题参与已使用独立表 `community_topic_participant`，`join_topic` 对 `(topic_id, user_id)` 幂等写入。

### 7.x 评论点赞

#### `PUT /api/v1/community/comments/{comment_id}/like`

#### `DELETE /api/v1/community/comments/{comment_id}/like`

两接口要求登录、绑定手机号并通过实名认证，无请求体，成功返回完整 `CommunityCommentResponse`。重复点赞或重复取消均保持幂等，返回当前评论状态。

资源不可见时不通过点赞接口泄露存在性：评论不存在、已删除、已下架，或所属动态对当前用户不可见时返回 `404`；当前用户被评论作者拉黑时返回 `403`。评论点赞计数以有效点赞记录为准，客户端应使用响应中的 `like_count` 和 `is_liked` 更新本地状态。

**变更记录（2026-08-01）**：补齐评论点赞正式契约，删除旧的“当前未提供”描述；接口路径和响应保持兼容。

### 2026-07-28（社区内容审核）

本节覆盖帖子、评论、纸飞机、纸飞机回复、纸飞机会话文本消息，以及社区图片/视频媒体。

- 用户发布文本会按 `config_sensitive_word.level/action` 执行“就高不就低”决策：`reject` 直接拦截，`replace` 保存替换后的展示内容，`manual_review` 创建审核任务并隐藏内容。
- `manual_review` 内容对普通用户不可见，不进入动态流、评论列表、纸飞机列表或会话消息列表；管理员审核完成后才会进入公共读取路径。
- 高风险拦截、人工提交和人工审核结果均向发布者写入站内通知。被拒绝内容允许修改后重新提交，但必须重新完整执行敏感词检测和审核流程。
- 原文、命中词、展示文本、审核任务及管理员查看/处理记录保存 1 年；原文不进入普通用户响应。
- 审核任务终态不可重复改判；删除与下架分别记录为 `deleted` 与 `hidden`，不能使用恢复接口覆盖审核终态。
- 新增 `PUT /api/v1/community/posts/{post_id}`：作者修改动态后重新提交。该接口会重新执行敏感词检测和完整审核流程；拒绝内容不会发布，人工审核内容继续对普通用户隐藏。旧客户端不受影响。

管理员接口详见 `docs/api/admin.md` 的“社区审核队列”。

### 2026-07-26（社区媒体上传与发布绑定）

- 新增 `POST /api/v1/community/media/uploads`（multipart：`file` + `purpose`）与 `DELETE /api/v1/community/media/{media_id}`。
- 新增表 `community_media` / `community_media_attachment`；状态机 `ready` → `bound` / `deleted`。
- `POST /api/v1/community/posts` 增加 `image_media_ids`、`video_media_id`；允许 media-only；旧 `images`/`video` 过渡为本人 storage URL。
- `POST /api/v1/paper-planes` 增加 `image_media_ids`；不支持视频。
- 未绑定媒体 24h 过期，由 `cleanup_expired_unbound_media` 清理。

### 2026-07-25（实名权限与评论可见性）

- 发布/删除动态、动态点赞/收藏、评论写入/删除、参与话题、活动报名、纸飞机发送/回复新增服务端实名通过要求；未通过返回 `403`。
- 浏览接口继续允许已登录、已绑手机号的非实名用户访问。
- `GET /community/posts/{post_id}/comments` 在查询评论前复用动态详情可见性检查，避免通过评论接口旁路读取不可见动态信息。

### 2026-07-25（同城 city_code + 关注并集，历史记录已被上方 City feed anchor 取代）

- 历史实现曾将同城主键写为 `residence_city_code`。当前契约只读写 `community_city_code`，且 `mode=city` 只按帖子 `location` 命中；不再回落到作者资料现居。
- `city_code` 现在只接受 4 或 6 位 ASCII 数字；4 位短码右补 `00` 规范化为市级码。
- 新增 `mode=following_and_liked`：关注∪用户级喜欢服务端分页。
- FE 映射与静态测见 `xuanshiai-vue/docs/COMMUNITY_HTTP_CHANGELOG.md`「同城 city_code 与关注并集」。

### 2026-07-25（关 Mock 端侧联调）

- 动态列表 SQL：`school` 从错误的 `user_profile.school` 改为 `user_auth.school`（否则 `GET /community/posts` 500）。
- FE 关 Mock + 局域网 `API_BASE_URL`；明细见 `xuanshiai-vue/docs/COMMUNITY_HTTP_CHANGELOG.md`「关 Mock 端侧联调」。

### 2026-07-25（实际测试 / 联调冒烟）

- 本地双用户 HTTP 冒烟：quotas 200、like 取消、互喜欢无会话、apply remain−1 / 409、accept 建会话。
- 修复 `discovery._viewer_context` 漏 `LEFT JOIN user_auth` 导致申请认识 500。
- 明细见 `xuanshiai-vue/docs/COMMUNITY_HTTP_CHANGELOG.md`「实际测试」。

### 2026-07-25（对抗审查修复）

- quotas VIP 会员列对齐 `start_at`/`end_at`；Redis 日额度键统一 UTC（`daily_quota_key`）。
- 前端关注 all 不再客户端并集；详见 `xuanshiai-vue/docs/COMMUNITY_ADVERSARIAL_REVIEW.md`。

### 2026-07-25（前端对接补充，本文件无新接口）

- 社区 UniApp 侧已双路径对接本节与 §6 所列社区端口；详细 FE 变更见 `xuanshiai-vue/docs/COMMUNITY_HTTP_CHANGELOG.md`。
- 补充 discovery 申请认识、social 喜欢用户为社区旁路协作接口。
- 前端导出删帖/删评/取关/我的纸飞机，对应既有 `DELETE posts|comments`、`DELETE follow`、`GET paper-planes/mine`。

### 2026-07-25

- 新增动态详情、收藏、话题、活动、Banner、额度、城市、举报原因接口。
- 扩展动态流 `mode/city/filter/sort` 与 `CommunityPostResponse` 兼容字段。
- 收藏使用 `community_like.type=3`。
- 明确社交接口协作边界。

### 2026-07-20

- 补充所有请求参数位置、类型、必填性、默认值、范围和完整示例。
- 补充动态、评论、纸飞机和分页响应字段含义及空数据响应。
- 明确 Redis 日额度、纸飞机 24 小时有效期、5 条回复上限和当前媒体审核边界。
- 明确当前响应数组没有 `total` 的接口契约，后续改动需要兼容迁移。

- 2026-07-25：同城 `city` 回落与「未设置」422；`set_current_city` 拒无效名；feed 匹配 TRIM 前缀。

## 2026-07-28 评论线程、举报查询与申诉

本节是兼容性增量。旧 `GET /api/v1/community/posts/{post_id}/comments` 保持分页数组响应，旧客户端无需迁移；新客户端应使用以下游标接口构建线程。

### `GET /api/v1/community/posts/{post_id}/comments/page`

- 权限：已登录且已绑定手机号；不要求实名。
- 请求体：无。
- Query：`cursor` 可选、不透明字符串、最长 128；`page_size` 默认 20，范围 1~50。
- 只返回一级评论，按 `id` 正序。动态不可见时返回 `404`。

### `GET /api/v1/community/comments/{comment_id}/replies`

- 权限、Query 与根评论页相同。
- `comment_id` 必须是一级评论；传入回复 ID 返回 `422`，不存在或被管理员下架返回 `404`。
- 回复按 `id` 正序；回复回复时仍归入同一根线程。

两条接口统一返回：

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `items` | array | 是 | 评论或回复；无数据为 `[]` |
| `items[].root_id` | integer/null | 是 | 线程根评论 ID；一级评论返回自身 ID |
| `items[].target_comment_id` | integer/null | 是 | 直接回复目标；一级评论为 `null` |
| `items[].target_user_id` | integer/null | 是 | 直接回复目标作者；一级评论为 `null` |
| `items[].reply_to_user` | string/null | 是 | 直接回复目标的昵称；一级评论为 `null`，用于展示「回复 @用户」 |
| `items[].reply_count` | integer | 是 | 一级评论的有效回复数；回复通常为 `0` |
| `items[].replies` | array | 是 | 仅一级评论返回按时间升序的前 3 条回复预览；回复列表仍通过独立游标接口继续加载 |
| `items[].is_deleted` | boolean | 是 | 是否为用户删除墓碑 |
| `items[].can_delete` | boolean | 是 | 当前登录用户能否删除该有效评论 |
| `items[].content` | string | 是 | 墓碑固定为「该评论已删除」 |
| `next_cursor` | string/null | 是 | 还有下一页时返回；客户端必须原样回传 |
| `has_more` | boolean | 是 | 是否还有下一页 |

响应示例：

```json
{
  "items":[{
    "id":21,"post_id":4,"user_id":8,"nickname":"回复者","avatar":null,
    "parent_id":20,"root_id":10,"target_comment_id":20,"target_user_id":7,
    "content":"我也这样认为","like_count":0,"is_liked":false,
    "reply_count":0,"is_deleted":false,"created_at":"2026-07-28T10:00:00"
  }],
  "next_cursor":"eyJpZCI6MjF9","has_more":true
}
```

`POST /api/v1/community/posts/{post_id}/comments` 的 `parent_id` 仍向后兼容；响应新增上述线程字段。父评论必须属于同一动态、未被用户删除且未被管理员下架。评论目标作者与当前用户之间任一方向存在拉黑关系时返回 `403`。用户删除写 `deleted_at`；有有效后代时在线程页保留墓碑，不再与管理员 `moderation_status` 共用状态。

### `GET /api/v1/community/reports/mine`

Query：`page` 默认 1、范围 1~1000；`page_size` 默认 20、范围 1~50。返回 `{items,page,page_size,total,has_more}`。`items` 包含当前用户提交的举报，以及当前用户作为被举报人的已处理结论；待处理举报不会暴露给被举报人。

每项返回 `id`、`target_user_id`、`target_type`、`target_id`、`viewer_role`、`type`、`description`、`status`、`result`、`action`、`reviewed_at`、时间字段与 `can_appeal`。`viewer_role` 为 `reporter` 或 `subject`：仅举报提交者可看到自己填写的 `description`；被举报人侧固定返回 `description=null`。用户态响应不返回 `reporter_user_id` 或 `reviewed_by`，不得据此识别举报人或内部审核人。

```json
{
  "items":[{
    "id":31,"target_user_id":23,"target_type":"post","target_id":88,
    "viewer_role":"subject","type":"harassment","description":null,
    "status":1,"result":"举报成立","action":"hide_content",
    "reviewed_at":"2026-07-28T10:30:00","created_at":"2026-07-27T09:00:00",
    "updated_at":"2026-07-28T10:30:00","can_appeal":true
  }],
  "page":1,"page_size":20,"total":1,"has_more":false
}
```

### `POST /api/v1/community/reports/{report_id}/appeals`

- 权限：当前用户必须是该举报的被举报人。
- Body：`{"reason":"内容没有违规，请复核"}`；`reason` 1~1000 字符。
- 资格：举报必须为 `status=1` 已处理，且同一举报尚未提交申诉。
- 成功：`201 Created`，返回 `id`、`report_id`、`appellant_user_id`、`reason`、`status=0`、处理结果和时间字段；用户态响应不返回复审人 ID。
- 错误：非被举报人 `403`；举报非已处理终态或重复申诉 `409`；举报不存在 `404`；格式错误 `422`。

### `GET /api/v1/community/report-appeals/mine`

Query 与举报列表相同。返回当前用户提交的申诉分页，状态为 `0` 待复审、`1` 申诉通过、`2` 申诉驳回。申诉通过仅在内容当前仍由该举报下架时恢复管理员 `moderation_status`；后续独立审核决定不会被覆盖，也不会撤销用户本人删除的 `deleted_at`。
