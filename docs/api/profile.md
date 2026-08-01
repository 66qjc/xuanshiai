# 个人资料、媒体与择偶要求接口

接口前缀：`/api/v1`。

除 `GET /profile/tag-options` 和 `GET /users/me/intro-templates` 外，接口需要：

```http
Authorization: Bearer <access_token>
```

JSON 接口使用 `Content-Type: application/json`；上传接口使用 `multipart/form-data`。

## 0. 我的页面聚合信息

### `GET /api/v1/users/me/overview`

- 权限：登录用户；只能返回当前 Token 对应用户的信息
- 参数：无
- 成功状态：`200 OK`

服务端聚合我的页面首屏所需的摘要，不返回密码、Token、完整手机号、实名证件、支付密钥或风控字段。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | integer | 当前用户 ID |
| `nickname` / `avatar` | string/null | 当前用户昵称和头像 |
| `account_status` | integer | 当前账号状态；正常登录用户为 `1`。冻结/注销账号访问受保护接口时返回 `403` |
| `completion_score` | number | 服务端计算的资料完整度 |
| `certification` | object | 实名认证摘要，仅包含状态和展示文案 |
| `certification.status` | integer | `0` 未提交、`1` 审核中、`2` 已通过、`3` 未通过 |
| `membership` | object | 当前有效会员摘要 |
| `membership.is_vip` | boolean | 当前是否有有效会员 |
| `membership.package_type` | string/null | 当前会员套餐类型 |
| `membership.expires_at` | datetime/null | 当前会员到期时间 |
| `unread_notification_count` | integer | 未读通知数量 |
| `incoming_application_count` / `outgoing_application_count` | integer | 待处理的收到/发出申请数量 |
| `match_count` | integer | 当前有效匹配数量 |
| `visitor_count` | integer | 去重后的历史访客数量 |
| `favorite_count` | integer | 当前用户收藏他人的数量 |
| `favorite_received_count` | integer | 他人收藏当前用户的数量 |
| `superlike_sent_count` | integer | 当前用户发出的爆灯数量 |
| `superlike_received_count` | integer | 当前用户收到的爆灯数量 |
| `shortcuts` | object | 当前账号可用的快捷入口权限，来源于服务端门槛计算 |

响应示例：

```json
{
  "user_id": 12,
  "nickname": "小明",
  "avatar": "/storage/uploads/12/avatar.webp",
  "account_status": 1,
  "completion_score": 100,
  "certification": {"status": 2, "label": "已通过"},
  "membership": {"is_vip": false, "package_type": null, "expires_at": null},
  "unread_notification_count": 2,
  "incoming_application_count": 1,
  "outgoing_application_count": 0,
  "match_count": 3,
  "shortcuts": {"can_browse": true, "can_apply": true, "can_chat": true, "can_edit_profile": true, "can_manage_media": true}
}
```

### 变更记录

- 2026-07-23：新增 overview 聚合接口；认证、会员、通知、申请和匹配摘要均由服务端查询，旧的独立资料接口保持不变。
- 2026-08-01：overview 增加访客、收藏和爆灯统计；资料更新支持 `weight`，身高范围调整为 140~220cm，自我介绍上限统一为 500 字。旧客户端忽略新增字段即可。

## 1. 固定标签目录

### `GET /api/v1/profile/tag-options`

- 权限：公开
- 请求参数：无
- 成功状态：`200 OK`

返回字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `version` | string | 标签目录版本 |
| `categories` | array | 标签分类数组 |
| `categories[].key` | string | 提交时使用的分类 key |
| `categories[].label` | string | 分类展示名称 |
| `categories[].options` | array[string] | 可选择的固定标签值 |

响应示例：

```json
{
  "version":"v1",
  "categories":[
    {"key":"relationship_expectation","label":"期望关系","options":["寻找长期伴侣","先交友看缘分","轻松约会"]}
  ]
}
```

标签只能从该接口返回的选项中选择，不能提交自定义标签。

## 2. 查询资料完整度

### `GET /api/v1/users/me/completion`

- 权限：登录用户
- 参数：无
- 成功状态：`200 OK`

返回字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `score` | number | 服务端计算的完整度，0~100 |
| `missing_items` | array[string] | 未完成项目展示名称 |
| `items` | array | 逐项完成状态 |
| `items[].key` | string | 稳定项目 key |
| `items[].label` | string | 项目名称 |
| `items[].weight` | integer | 项目权重 |
| `items[].completed` | boolean | 是否完成 |
| `can_browse` | boolean | 是否满足推荐浏览门槛 |
| `can_apply` | boolean | 是否满足申请认识门槛 |
| `can_chat` | boolean | 是否满足聊天门槛 |

响应示例：

```json
{
  "score":57,
  "missing_items":["相册","实名认证"],
  "items":[
    {"key":"gender","label":"性别","weight":7,"completed":true},
    {"key":"avatar","label":"头像","weight":15,"completed":true}
  ],
  "can_browse":false,
  "can_apply":false,
  "can_chat":false
}
```

当前权重：

| 项目 | key | 权重 |
| --- | --- | ---: |
| 性别 | `gender` | 7 |
| 出生日期/年龄 | `birthday` | 7 |
| 所在地区 | `location` | 7 |
| 婚姻状况 | `marriage` | 5 |
| 职业 | `occupation` | 4 |
| 学历 | `education` | 4 |
| 收入 | `income` | 4 |
| 身高 | `height` | 4 |
| 头像 | `avatar` | 15 |
| 自我介绍 | `intro` | 10 |
| 相册 | `album` | 10 |
| 兴趣标签 | `interest` | 5 |
| 性格标签 | `personality` | 3 |
| MBTI | `mbti` | 2 |
| 择偶要求 | `preference` | 3 |
| 实名认证 | `realname` | 5 |
| 单身承诺 | `single_pledge` | 5 |

总和为 100。推荐、申请和聊天还要求手机号绑定；申请和聊天要求实名认证通过。

## 3. 查询和更新基础资料

### `GET /api/v1/users/me/profile`

- 权限：登录用户
- 参数：无
- 成功状态：`200 OK`

返回字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | integer | 用户 ID |
| `nickname` | string/null | 昵称 |
| `gender` | integer/null | `1` 男、`2` 女 |
| `birthday` | string/date/null | 出生日期，`YYYY-MM-DD` |
| `age` | integer/null | 服务端根据生日计算的年龄 |
| `is_married` | integer/null | `1` 未婚、`2` 离异、`3` 丧偶 |
| `height` | integer/null | 身高，厘米 |
| `occupation` | string/null | 职业 |
| `industry` | string/null | 行业 |
| `education_level` | integer/null | 学历等级 |
| `income` | number/null | 月收入区间对应值 |
| `hometown_province_code/city_code/district_code` | string/null | 籍贯省市区编码 |
| `residence_province_code/city_code/district_code` | string/null | 居住/工作地省市区编码 |
| `self_intro` | string/null | 自我介绍 |
| `interest_tags` | array[string] | 兴趣标签 |
| `personality_tags` | array[string] | 性格标签 |
| `mbti` | string/null | 16 种 MBTI 值之一 |
| `avatar` | string/null | 头像地址 |
| `background_wall` | string/null | 背景墙地址 |
| `tag_selections` | object | 分类 key 到标签数组的映射 |
| `photos` | array | 相册媒体对象 |
| `video` | object/null | 视频媒体对象 |
| `completion_score` | number | 当前完整度 |

媒体对象字段：`id` 媒体 ID、`media_type` 媒体类型、`file_url` 文件地址、`thumbnail_url` 缩略图地址、`sort_order` 排序、`is_primary` 是否首图、`duration_seconds` 视频时长。

### `PATCH /api/v1/users/me/profile`

- 权限：登录用户
- 成功状态：`200 OK`
- 更新方式：只更新请求体中提供的字段，未提供字段保持原值

请求字段：

| 字段 | 类型 | 必填 | 规则和含义 |
| --- | --- | --- | --- |
| `gender` | integer/null | 否 | `1` 男、`2` 女；首次写入后不可自行修改 |
| `birthday` | date/null | 否 | `YYYY-MM-DD`；服务端计算年龄且必须满 18 岁 |
| `is_married` | integer/null | 否 | `1` 未婚、`2` 离异、`3` 丧偶 |
| `height` | integer/null | 否 | 150~200cm |
| `weight` | integer/null | 否 | 40~120kg |
| `occupation` | string/null | 否 | 最长 128 字符 |
| `industry` | string/null | 否 | 最长 128 字符 |
| `education_level` | integer/null | 否 | 1~8 |
| `income` | number/null | 否 | 0~1,000,000 |
| `hometown_province_code/city_code/district_code` | string/null | 否 | 各最长 32 字符 |
| `residence_province_code/city_code/district_code` | string/null | 否 | 各最长 32 字符 |
| `self_intro` | string/null | 否 | 最长 500 字符 |
| `interest_tags` | array[string] | 否 | 3~5 个，必须来自固定目录 |
| `personality_tags` | array[string] | 否 | 3~5 个，必须来自固定目录 |
| `mbti` | string/null | 否 | `INTJ` 等 16 种标准值 |
| `tag_selections` | object | 否 | 分类 key 和标签必须来自固定目录，每类最多 5 个 |

请求示例：

```json
{
  "gender":1,
  "birthday":"1995-05-20",
  "is_married":1,
  "height":175,
  "occupation":"软件工程师",
  "industry":"互联网",
  "education_level":3,
  "income":15000,
  "residence_province_code":"310000",
  "residence_city_code":"310100",
  "self_intro":"喜欢旅行、摄影和运动，希望认识真诚的人。",
  "interest_tags":["旅行","摄影","健身"],
  "personality_tags":["真诚","耐心","乐观"],
  "mbti":"ENFP",
  "tag_selections":{"relationship_expectation":["寻找长期伴侣"]}
}
```

成功响应为完整 `ProfileResponse`。非法枚举、标签不在目录、标签数量不足、未成年、超长文本和性别修改返回 `422` 或 `409`。

## 4. 主页预览与自我介绍模板

### `GET /api/v1/users/me/profile/preview`

- 权限：登录用户
- 参数：无
- 返回：

```json
{
  "preview_notice":"这是别人看到你的样子",
  "profile": {"user_id":1,"nickname":"小明","completion_score":80}
}
```

`profile` 内部字段与个人资料查询一致。

### `GET /api/v1/users/me/intro-templates`

- 权限：公开
- 参数：无

返回数组字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `key` | string | 模板 key |
| `title` | string | 模板标题 |
| `content` | string | 可填充到输入框的模板正文 |

录音转文字和后台模板配置尚未接入。

## 5. 媒体上传

所有上传接口：

- Header：`Authorization: Bearer <access_token>`
- Content-Type：`multipart/form-data`
- 文件字段名：`file`
- 成功状态：`201 Created`
- 响应：媒体对象，字段含义见第 3 节

| 接口 | 文件规则 | 业务规则 |
| --- | --- | --- |
| `POST /api/v1/users/me/avatar` | JPG/JPEG/PNG，最大 5MB，最多 2500 万像素 | 覆盖旧头像；转换 WebP 并生成缩略图 |
| `POST /api/v1/users/me/background` | JPG/JPEG/PNG，最大 5MB，最多 2500 万像素 | 覆盖旧背景墙 |
| `POST /api/v1/users/me/photos` | JPG/JPEG/PNG，最大 5MB，最多 2500 万像素 | 最多 9 张，转换 WebP |
| `POST /api/v1/users/me/video` | MP4，最大 50MB，最长 30 秒 | 每个用户最多 1 个；服务端使用 ffprobe 检测真实时长 |

图片示例：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/users/me/photos" \
  -H "Authorization: Bearer <access_token>" \
  -F "file=@photo.jpg"
```

视频时长不接受客户端提交值；未安装 `ffprobe` 返回 `503`。

### 相册操作

#### `PUT /api/v1/users/me/photos/order`

JSON 请求体：

```json
{"media_ids":[12,8,15]}
```

`media_ids` 必须是当前用户全部未删除相册图片 ID，1~9 个且不能重复。成功返回 `204`。

#### `PUT /api/v1/users/me/photos/{media_id}/primary`

Path 参数 `media_id` 为正整数且必须属于当前用户；成功返回新的媒体对象，并同步头像地址。

#### `DELETE /api/v1/users/me/photos/{media_id}`

只能删除自己的相册图片；首图删除后自动选择下一张有效图片。成功返回 `204`。

## 6. 择偶要求

### `GET /api/v1/users/me/preferences`

无请求参数。没有记录时返回所有字段的默认“不限”值：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | integer | 用户 ID |
| `age_min/age_max` | integer/null | 期望年龄区间 |
| `height_min/height_max` | integer/null | 期望身高区间 |
| `education_min` | integer/null | 学历下限 |
| `income_min` | number/null | 收入下限 |
| `marriage_status` | integer/null | 婚姻要求，`0` 表示不限 |
| `preferred_province_code` | string/null | 期望省份 |
| `preferred_city_codes` | array[string] | 期望城市编码 |
| `accept_long_distance` | boolean | 是否接受异地 |
| `accept_cross_province` | boolean | 是否接受跨省 |
| `housing_requirement` | integer/null | 房产要求 |
| `smoking_requirement` | integer/null | 抽烟要求 |
| `drinking_requirement` | integer/null | 饮酒要求 |
| `extra_requirement` | string/null | 其他要求，最长 255 字符 |

### `PUT /api/v1/users/me/preferences`

请求体字段与返回字段相同，均为可选更新字段；年龄和身高下限不能大于上限。更新后影响后续推荐，不修改已经产生的历史记录。

请求示例：

```json
{
  "age_min":25,
  "age_max":35,
  "height_min":160,
  "height_max":175,
  "education_min":3,
  "income_min":10000,
  "marriage_status":1,
  "preferred_province_code":"310000",
  "preferred_city_codes":["310100"],
  "accept_long_distance":false,
  "accept_cross_province":false,
  "housing_requirement":0,
  "smoking_requirement":2,
  "drinking_requirement":1,
  "extra_requirement":"希望作息规律，愿意沟通。"
}
```

## 7. 错误响应

```json
{"detail":"相册最多保存9张图片"}
```

| HTTP | 触发条件 |
| --- | --- |
| `401` | 未登录或 Token 无效 |
| `403` | 没有访问权限 |
| `404` | 用户或媒体不存在 |
| `409` | 性别不可修改、相册/视频数量冲突 |
| `413` | 文件超过大小限制 |
| `415` | 文件格式或真实内容无法识别 |
| `422` | 字段、标签、MBTI、年龄、时长或排序校验失败 |
| `503` | ffprobe 未安装或视频处理服务不可用 |
