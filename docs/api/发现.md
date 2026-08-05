# 首页推荐、搜索与名片互动接口

接口前缀：`/api/v1`。

除 `GET /discovery/filter-options` 外，本文件接口需要登录。推荐、广场、搜索、保存筛选还要求手机号已绑定且资料完整度为 100%。推荐、广场和搜索统一在接口依赖层执行该首页准入校验；账号冻结或注销返回 `403`，不会伪装成未登录。

### 变更记录

- 2026-07-23：推荐、广场、搜索统一使用服务端首页准入依赖；冻结/注销账号访问受保护接口时返回 `403`。
- 2026-07-24：推荐、广场、搜索、浏览记录、访客和收藏列表统一按目标用户隐私、停用、拉黑和媒体审核状态过滤；普通用户不会看到仅 VIP 可见的详情字段。
- 2026-07-24：申请认识和爆灯写入前按用户 ID 顺序锁定双方用户，避免并发请求重复创建记录；拒绝申请仅在原申请当天退还额度。

通用请求头：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

错误响应统一为：

```json
{"detail":"请先完善资料后再进入推荐"}
```

## 1. 筛选选项

### `GET /api/v1/discovery/filter-options`

- 权限：公开
- 请求参数：无
- 成功：`200 OK`

返回字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `genders` | array | 性别选项，每项含 `value`、`label` |
| `marriage_statuses` | array | 婚姻选项，每项含 `value`、`label` |
| `education_levels` | array | 学历选项，每项含 `value`、`label` |
| `cities` | array[string] | 固定城市展示名称 |

响应：

```json
{
  "genders":[{"value":1,"label":"男"},{"value":2,"label":"女"}],
  "marriage_statuses":[{"value":1,"label":"未婚"},{"value":2,"label":"离异"},{"value":3,"label":"丧偶"}],
  "education_levels":[{"value":1,"label":"博士"},{"value":3,"label":"本科"}],
  "cities":["北京","上海","杭州"]
}
```

## 2. 名片对象

推荐、搜索、广场和主页摘要中的 `DiscoveryCard` 字段如下：

| 字段 | 类型 | 空值/枚举 | 含义 |
| --- | --- | --- | --- |
| `user_id` | integer | 必返 | 目标用户 ID |
| `nickname` | string/null | 可空 | 昵称 |
| `avatar` | string/null | 可空 | 头像地址 |
| `age` | integer/null | 可空 | 根据出生日期计算的年龄 |
| `height` | integer/null | 锁定时为空 | 身高厘米 |
| `education_level` | integer/null | 锁定时为空 | 学历等级 |
| `occupation` | string/null | 锁定时为空 | 职业 |
| `is_married` | integer/null | 1 未婚、2 离异、3 丧偶 | 婚姻状态 |
| `online_status` | integer | 0 离线、1 在线、2 隐身 | 在线状态 |
| `mbti` | string/null | 锁定时为空 | MBTI 类型 |
| `interest_tags` | array[string] | 锁定时为空数组 | 最多展示 5 个兴趣标签 |
| `certification_tags` | array[string] | 可为空数组 | 实名、单身承诺等认证标签 |
| `match_score` | number | 0~100 | 合拍度分值 |
| `match_reason` | string | 必返 | 合拍原因 |
| `is_favorite` | boolean | 必返 | 当前用户是否收藏 |
| `is_pure_free` | boolean | 必返 | 无 VIP 且无有效置顶/爆灯权益 |
| `is_boosted` | boolean | 必返 | 是否有有效置顶记录 |
| `detail_locked` | boolean | 必返 | 详细资料是否因额度/隐私锁定 |

分页响应结构：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `items` | array[DiscoveryCard] | 当前页名片 |
| `page` | integer | 当前页，从 1 开始 |
| `page_size` | integer | 当前页大小，1~20 |
| `total` | integer | 当前查询候选数，当前实现最多统计 500 条候选 |
| `has_more` | boolean | 是否还有下一页 |

## 3. 推荐和广场

### `GET /api/v1/discovery/recommendations`

- 权限：已绑定手机号、资料完整度 100% 且实名认证通过的登录用户
- 成功：`200 OK`
- 排序：有效置顶优先，其次按年龄相近、标签重合、择偶要求匹配度和活跃度排序
- 排除：自己、未完善用户、不可见用户、互相拉黑、已浏览、已划过、申请中和已匹配用户

查询参数：

| 参数 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `gender` | integer | 否 | `1` 或 `2` | 目标性别 |
| `age_min`/`age_max` | integer | 否 | 18~100，下限不能大于上限 | 临时年龄筛选 |
| `province_code` | string | 否 | 最长 32 字符 | 目标居住省编码 |
| `city_code` | string | 否 | 最长 32 字符 | 目标居住市编码 |
| `district_code` | string | 否 | 最长 32 字符 | 目标居住区编码 |
| `marriage_status` | integer | 否 | `1`、`2`、`3` | 婚姻状态 |
| `education_min` | integer | 否 | 1~8 | 学历下限 |
| `height_min`/`height_max` | integer | 否 | 100~250，下限不能大于上限 | 身高区间 |
| `income_min`/`income_max` | number | 否 | 0~1,000,000，下限不能大于上限 | 收入区间 |
| `pure_free` | boolean | 否 | 默认 `false` | 仅展示无 VIP 且无有效置顶/爆灯权益用户 |
| `page` | integer | 否 | 1~1000，默认 1 | 页码 |
| `page_size` | integer | 否 | 1~20，默认 20 | 每页数量 |

请求示例：

```text
GET /api/v1/discovery/recommendations?age_min=25&age_max=35&city_code=310100&page=1&page_size=20
```

成功返回分页结构。没有候选时：

```json
{"items":[],"page":1,"page_size":20,"total":0,"has_more":false}
```

### `GET /api/v1/discovery/plaza`

权限、参数和返回结构与推荐接口相同。广场不排除已浏览和已划过用户，但仍过滤未完善、不可见、停用和互相拉黑用户。

## 4. 按昵称或标签搜索

### `GET /api/v1/discovery/search`

- 权限：已绑定手机号、资料完整度 100% 的登录用户
- 成功：`200 OK`

| 参数 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `nickname` | string | 否 | 最长 64 字符；空白会被去除 | 昵称字面模糊匹配，`%` 和 `_` 不作为通配符 |
| `tag` | string | 否 | 最长 64 字符 | 在兴趣、性格和固定分类标签中搜索 |
| `page` | integer | 否 | 1~1000，默认 1 | 页码 |
| `page_size` | integer | 否 | 1~20，默认 20 | 每页数量 |

`nickname` 与 `tag` 至少提供一个；同时提供时按 AND 条件匹配。搜索结果按年龄相近度、标签重合度和活跃时间排序。

示例：

```text
GET /api/v1/discovery/search?nickname=小明
GET /api/v1/discovery/search?tag=旅行&page=1&page_size=20
GET /api/v1/discovery/search?nickname=小明&tag=摄影
```

## 5. 保存筛选条件

### `GET /api/v1/discovery/filters/saved`

- 权限：已绑定手机号且资料完整度 100% 的登录用户
- 参数：无

已保存时返回：

```json
{"filters":{"gender":2,"age_min":25,"age_max":35,"page":1,"page_size":20}}
```

未保存时返回：

```json
{"filters":null}
```

### `PUT /api/v1/discovery/filters/saved`

请求体字段与推荐筛选参数相同，保存当前用户唯一一组筛选条件，重复保存覆盖旧条件：

```json
{"gender":2,"age_min":25,"age_max":35,"city_code":"310100","page":1,"page_size":20}
```

## 6. 浏览记录、访客和收藏

### `GET /api/v1/discovery/browse-history`

查询参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。返回分页对象，`items[].target` 为名片，`items[].viewed_at` 为浏览时间。

普通用户只能查看当天记录，VIP 可以查看历史记录。访问他人主页时记录浏览；开启 VIP 无痕浏览后不写入对方访客记录。

### `GET /api/v1/discovery/visitors`

查询参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。普通用户返回：

```json
{"can_view_details":false,"count":3,"items":[],"page":1,"page_size":20,"has_more":false}
```

VIP 返回 `can_view_details=true`、分页字段和访客名片数组。

### `GET /api/v1/discovery/favorites`

查询参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。返回 `items`、`page`、`page_size`、`total`、`has_more`；没有收藏时 `items` 返回 `[]`。

### `PUT /api/v1/discovery/favorites/{target_id}`

Path `target_id` 为正整数。收藏成功返回：

```json
{"target_user_id":23,"is_favorite":true}
```

### `DELETE /api/v1/discovery/favorites/{target_id}`

取消当前用户自己的收藏，成功返回 `{"target_user_id":23,"is_favorite":false}`。收藏不通知对方，也不产生匹配。

### `GET /api/v1/discovery/favorites/received`

查询“谁收藏我”。需要登录；按 `page`（1~1000，默认 1）和 `page_size`（1~50，默认 20）分页。返回 `items/page/page_size/total/has_more`，每条 `items[]` 包含 `id`、`user`（`user_id/nickname/avatar/age/city_code`）、`relation=received` 和 `created_at`。已注销、隐藏资料、暂停交友及互相拉黑的用户会被过滤；该接口表达收藏关系，不等同于喜欢或认识申请。

## 7. 认识申请

### `POST /api/v1/discovery/applications/{target_id}`

- 权限：已绑定手机号、资料完整度 100% 的登录用户
- Path：`target_id` 正整数
- 成功：`201 Created`

请求体：

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `message` | string/null | 否 | 最长 255 字符 | 给对方的留言 |

示例：

```json
{"message":"你好，很高兴认识你"}
```

返回字段：`id` 申请 ID、`from_user_id` 发起方、`to_user_id` 接收方、`message` 留言、`status` 状态、`expire_at` 过期时间、`created_at` 创建时间。

申请状态：`0` 待处理、`1` 同意、`2` 拒绝、`3` 过期。普通用户每天 3 次，VIP 每天 10 次，申请 48 小时后过期；拒绝会退还发起方当日 1 次额度。

### `GET /api/v1/discovery/applications/incoming`

查询参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。返回分页对象；无数据时 `items` 返回 `[]`。

### `GET /api/v1/discovery/applications/outgoing`

查询参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。返回分页对象；无数据时 `items` 返回 `[]`。

### `POST /api/v1/discovery/applications/{application_id}/accept`

仅申请接收方可调用。Path `application_id` 为正整数。成功后更新为 `status=1`，创建双方匹配记录、聊天会话，并通知发起方。

### `POST /api/v1/discovery/applications/{application_id}/reject`

仅接收方可调用。请求体可省略，也可以传：

```json
{"reason":"暂时不合适"}
```

成功后更新为 `status=2`，拒绝不可撤回，并退还发起方当日额度。

## 8. 爆灯

### `POST /api/v1/discovery/superlikes/{target_id}`

- Path：`target_id` 正整数
- 权限：已绑定手机号、资料完整度 100% 且实名认证通过的登录用户
- 成功：`201 Created`

普通用户每日 1 次，VIP 每日 3 次。返回：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `target_user_id` | integer | 目标用户 |
| `remaining_today` | integer/null | 当日剩余次数 |
| `created_at` | datetime | 爆灯时间 |

爆灯会写入有效记录并通知对方；Redis 不可用返回 `503`。

### `GET /api/v1/discovery/superlikes/sent` / `GET /api/v1/discovery/superlikes/received`

查询本人发出或收到的爆灯记录。需要登录，支持 `page`（1~1000，默认 1）和 `page_size`（1~50，默认 20）。返回 `items/page/page_size/total/has_more`；每条记录包含 `id`、`user`（`user_id/nickname/avatar/age/city_code`）、`direction`、`status`、`created_at`、`matched` 和仅本人可见的 `order_no`。用户注销或互相拉黑后的记录不对外展示。

## 9. 他人主页和海报

### `GET /api/v1/users/{user_id}/profile`

Path `user_id` 为正整数。返回：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | integer | 目标用户 ID |
| `card` | DiscoveryCard | 名片摘要 |
| `profile` | object/null | 额度允许时的公开完整资料 |
| `is_vip_viewer` | boolean | 当前访问者是否 VIP |
| `browse_quota_remaining` | integer/null | 普通用户剩余完整浏览次数，VIP 为 null |
| `can_apply` | boolean | 当前用户是否满足申请门槛 |

普通用户每日 20 次完整浏览，高合拍度名片额外 5 次；超过后 `card.detail_locked=true`，只保留基础字段。

名片、浏览记录、访客和收藏列表会同时应用目标用户的 `show_profile`、交友状态、拉黑关系、媒体审核状态和 VIP 详情设置；被隐藏的记录不会计入 `total`/`count`。

### `GET /api/v1/users/{user_id}/poster?template=1`

- Path：`user_id` 正整数
- Query：`template` 1~25，默认 1
- 成功：`200 OK`
- Content-Type：`image/png`
- 请求体：无

服务端调用微信小程序码接口生成个人主页二维码并合成 PNG。未配置 `WECHAT_APP_ID`/`WECHAT_APP_SECRET` 或微信调用失败返回 `503`。

## 10. 错误码

| HTTP | 触发条件 | 前端处理 |
| --- | --- | --- |
| `401` | 未登录或会话失效 | 清理 Token 并回登录页 |
| `403` | 未绑定手机号、资料未完成、无资源权限或被拉黑 | 展示绑定/完善资料/权限提示 |
| `404` | 用户、申请、名片或会话目标不存在/不可见 | 移除列表项或提示不可用 |
| `409` | 重复收藏、申请冲突或状态已处理 | 刷新当前状态 |
| `422` | 参数类型、范围、枚举或区间错误 | 修正请求参数 |
| `429` | 浏览、申请或爆灯次数用完 | 展示额度/会员提示 |
| `503` | Redis 或微信服务不可用 | 稍后重试 |
