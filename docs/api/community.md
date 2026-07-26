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

当前实现复用 FastAPI、Pydantic、SQLAlchemy AsyncSession 和 Redis 日额度工具。动态/纸飞机的图片地址必须由前端先获得，但本组接口当前只校验地址字符串长度和数组数量，不负责文件上传、图片内容识别或敏感词审核。

以下社区写操作还要求 `realname_status == 2`：发布/删除动态、动态点赞或取消点赞、收藏或取消收藏、发表/删除评论、参与话题、活动报名、发送或回复纸飞机。未通过实名时返回 `403`：

```json
{"detail":"璇峰厛瀹屾垚瀹炲悕璁よ瘉"}
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
| `content` | body | string | 是 | 无 | 1~2000 字符 | 动态正文 |
| `images` | body | array[string] | 否 | `[]` | 最多 9 个地址 | 动态图片地址 |
| `video` | body | string/null | 否 | `null` | 最长 500 字符 | 动态视频地址 |
| `location` | body | string/null | 否 | `null` | 最长 128 字符 | 展示位置文本 |
| `topic_id` | body | integer/null | 否 | `null` | 非空时 `>=1` | 话题 ID；可通过 `GET /api/v1/community/topics`、`GET /api/v1/community/topics/page` 查询，并用 `GET /api/v1/community/topics/{topic_id}` 或 `GET /api/v1/community/topics/{topic_id}/detail` 查看详情 |
| `visibility` | body | integer | 否 | `0` | `0` / `1` / `2` | `0` 公开，`1` 仅双向匹配用户可见，`2` 仅作者本人可见 |
| `declaration` | body | string | 否 | `""` | `""` / `"内容包含虚构演绎"` / `"内容包含广告推广"` / `"内容可能引起不适"` | 作者选择的内容声明 |

请求示例：

```json
{
  "content":"今天去看了一个展览",
  "images":["/storage/uploads/1/photo.webp"],
  "video":null,
  "location":"上海",
  "topic_id":null,
  "visibility":1,
  "declaration":"内容包含广告推广"
}
```

非法示例：

```json
{"content":"","images":[]}
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
| `mode` | query | string | 否 | `latest` | `latest` / `following` / `city` / `liked_users` / `following_and_liked` | 全站最新 / 我关注用户 / 同城 / 我喜欢用户发布的动态 / 关注与喜欢用户的并集 |
| `page` | query | integer | 否 | `1` | `1~1000` | 页码 |
| `page_size` | query | integer | 否 | `20` | `1~50` | 每页数量 |
| `city` | query | string/null | 否 | `null` | 最长 64 字符 | `mode=city` 的城市展示名锚点 |
| `city_code` | query | string/null | 否 | `null` | 恰好 4 或 6 位 ASCII 数字；4 位短码规范化为末尾补 `00` 的 6 位市码 | `mode=city` 的城市码锚点，优先于纯文案匹配 |
| `filter` | query | string/null | 否 | `null` | `all` / `mbti` / `alumni` / `hometown` / `hot` / `latest` | 发现页二级筛选或热度 |
| `sort` | query | string | 否 | `latest` | `latest` / `hot` | 排序；`filter=hot` 时按热度 |

`city_code` 提供时只能是 4 或 6 位 ASCII 数字；其他值在 API 边界返回 `422`。同时提供且与 `city` 冲突时，`city_code` 优先。`mode=city` 仅按帖子发布地 `location` 过滤；请求未提供城市时才回落到同城浏览偏好 `community_city_*`，两者都没有可用锚点时返回 `422`。

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

排序：先按平台置顶字段 `is_top` 倒序，再按 `created_at` 倒序。成功示例：

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
| `content` | body | string | 是 | 无 | 1~1000 字符 | 纸飞机正文 |
| `images` | body | array[string] | 否 | `[]` | 最多 6 个地址 | 附图地址 |
| `city` | body | string/null | 否 | `null` | 最长 64 字符 | 展示城市 |
| `tags` | body | array[string] | 否 | `[]` | 最多 5 个标签 | 纸飞机标签 |
| `is_anonymous` | body | boolean | 否 | `true` | 布尔值 | 是否匿名展示 |

请求示例：

```json
{
  "content":"想认识同样喜欢旅行的人",
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
| `403` | 社区写操作的账号未通过实名认证 | `璇峰厛瀹屾垚瀹炲悕璁よ瘉` | 引导完成实名认证；浏览能力不受影响 |
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
| `mode` | query | string | 否 | `latest` | `latest` / `following` / `city` / `liked_users` / `following_and_liked` | 最新、关注、同城、喜欢用户动态、关注∪喜欢并集 |
| `city` | query | string/null | 否 | `null` | 最长 64；`mode=city` 展示名兼容 | 同城城市名 |
| `city_code` | query | string/null | 否 | `null` | 市一级 6 位码（如 `330100`）；短码 4 位右补 `00` | 同城主键，优先于 name |
| `filter` | query | string/null | 否 | `null` | `all` / `mbti` / `alumni` / `hometown` / `hot` / `latest` | 发现页二级筛选或热度 |
| `sort` | query | string | 否 | `latest` | `latest` / `hot` | 排序；`filter=hot` 时按热度 |

`mode=city` 时：**只按帖子发布地 `p.location`** 命中（等值或 `city%` 前缀）；**不**用作者 `residence` / `residence_city_code`。
锚点解析（仅决定筛哪个市名，不参与 OR）：请求 `city`/`city_code` → 同城浏览偏好 `community_city_*` → 仍无则 `422`。不回落到资料现居 `residence_*`。字面量 `city=未设置` → `422`。
发现页 `same_city` 仍只看资料 `residence_city_code`，与同城偏好无关。

`mode=following_and_liked`：作者在 `user_favorite` 且 `user_id=me` 且 `type IN (1, 3)`（喜欢用户 ∪ 关注），服务端去重 + COUNT 分页。关注 Tab「全部」应对接此 mode，禁止客户端双请求假并集。

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

实际提交举报请使用社交接口 `POST /api/v1/security/reports/{target_id}`。

## 10. 与社交 / 发现模块的协作

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

## 11. 当前边界与变更记录

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

当前未提供：动态/纸飞机媒体上传接口、媒体内容审核、敏感词审核、评论点赞接口、纸飞机语音、纸飞机回复自动转私信、独立话题参与表、后台社区审核列表。`join_topic` 仅做存在性校验与幂等成功，不以独立表记录参与。

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
