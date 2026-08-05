# 注册身份与红娘申请接口

接口前缀：`/api/v1`。

需要登录的接口携带：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

管理员接口需要具备 `admin` 角色。普通登录用户没有管理员权限。

## 1. 注册身份选项

### `GET /api/v1/registration/intents`

- 权限：公开
- 请求参数：无
- 成功状态：`200 OK`

响应数组字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `intent_type` | string | 稳定枚举值 |
| `label` | string | 前端展示名称 |
| `description` | string | 选项说明 |

响应：

```json
[
  {"intent_type":"self_match","label":"自己找","description":"以本人交友和婚恋匹配为主要目的"},
  {"intent_type":"parent_match","label":"父母帮找","description":"由本人授权父母参与资料和匹配流程"},
  {"intent_type":"companion","label":"找搭子","description":"以兴趣、活动和同城搭子为主要目的"}
]
```

## 2. 提交注册身份

### `PUT /api/v1/auth/registration-intent`

- 权限：登录用户
- 成功状态：`200 OK`
- 幂等：同一用户重复提交会更新当前记录，不创建重复记录

| 字段 | 位置 | 类型 | 必填 | 枚举/默认值 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `intent_type` | body | string | 是 | `self_match`、`parent_match`、`companion` | 当前业务意图 |
| `source` | body | string | 否 | `register` 或 `profile`，默认 `register` | 选择来源 |

请求：

```json
{"intent_type":"self_match","source":"register"}
```

返回：

```json
{"intent_type":"self_match","label":"自己找","description":"以本人交友和婚恋匹配为主要目的"}
```

当前允许重新选择；身份差异化首页、父母授权关系和找搭子独立匹配规则仍需产品确认。

## 3. 查询当前注册身份

### `GET /api/v1/auth/registration-intent`

- 权限：登录用户
- 参数：无

已选择时返回与提交接口相同的对象；未选择时返回：

```json
null
```

## 4. 红娘申请类型

### `GET /api/v1/matchmaker/application-types`

- 权限：公开
- 参数：无

响应：

```json
[
  {"application_type":"promoter","label":"推广红娘"},
  {"application_type":"partner","label":"合伙人招募"},
  {"application_type":"service_matchmaker","label":"服务红娘"}
]
```

`application_type` 是后续提交接口的稳定枚举值。

## 5. 提交红娘申请

### `POST /api/v1/matchmaker/applications`

- 权限：登录用户
- 前置条件：绑定手机号、实名认证通过
- 成功状态：`201 Created`

| 字段 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `application_type` | body | string | 是 | 三种申请类型之一 | 申请分支 |
| `real_name` | body | string | 是 | 2~64 字符 | 申请人姓名 |
| `phone` | body | string | 是 | 11 位大陆手机号 | 联系电话 |
| `intro` | body | string | 是 | 10~2000 字符 | 申请说明 |
| `cert_images` | body | array[string] | 否 | 最多 6 个地址 | 资质材料地址 |
| `application_details` | body | object | 否 | 见下方结构；默认空对象 | 审核扩展资料 |

`application_details` 字段：

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `wechat` | string/null | 否 | 最长 128 字符 | 红娘服务微信号，仅供审核和后续服务交付 |
| `avatar` | string/null | 否 | 最长 512 字符 | 申请头像地址 |
| `specialties` | array[string] | 否 | 最多 5 项，不可重复 | 擅长领域 |
| `expected_price` | number/null | 否 | 0~1000000 | 期望服务价格 |
| `success_cases` | array[object] | 否 | 最多 3 条 | 脱敏成功案例 |
| `success_cases[].description` | string | 是（案例存在时） | 1~1000 字符 | 案例说明 |
| `success_cases[].images` | array[string] | 否 | 最多 3 个地址 | 案例图片 |

请求：

```json
{
  "application_type":"service_matchmaker",
  "real_name":"张三",
  "phone":"13800138000",
  "intro":"有多年婚恋咨询和沟通经验",
  "cert_images":["/storage/application/cert-1.jpg"],
  "application_details": {
    "wechat":"matchmaker_demo",
    "avatar":"/storage/application/avatar.jpg",
    "specialties":["高知青年","同城牵线"],
    "expected_price":199,
    "success_cases":[{"description":"已完成脱敏案例说明","images":[]}]
  }
}
```

返回字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | integer | 申请 ID |
| `application_type` | string | 申请类型 |
| `status` | integer | `0` 待审核、`1` 通过、`2` 驳回、`3` 暂停 |
| `real_name` | string | 姓名 |
| `phone_masked` | string | 脱敏手机号 |
| `intro` | string | 申请说明 |
| `cert_images` | array[string] | 材料地址 |
| `application_details` | object | 本人或管理员可见的审核扩展资料；公开红娘接口不会返回 |
| `fail_reason` | string/null | 驳回/暂停原因 |
| `created_at` | string | ISO 时间 |
| `reviewed_at` | string/null | 审核时间 |

已存在同类型待审核、通过或暂停记录时返回 `409`；驳回记录允许重新提交。

## 6. 查询我的申请

### `GET /api/v1/matchmaker/applications/mine`

- 权限：登录用户
- 参数：无
- 成功状态：`200 OK`

返回申请对象数组；没有申请时返回：

```json
[]
```

## 7. 管理员审核红娘申请

### `PATCH /api/v1/admin/matchmaker/applications/{application_id}`

- 权限：管理员
- Path 参数：`application_id`，正整数，申请 ID
- 成功状态：`200 OK`

请求参数：

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `status` | integer | 是 | `1` 通过、`2` 驳回、`3` 暂停 | 审核结果 |
| `fail_reason` | string/null | 否 | 最长 255 字符；驳回/暂停建议填写 | 处理原因 |

请求：

```json
{"status":1,"fail_reason":null}
```

通过后授予对应角色；驳回或暂停会撤销对应角色状态。只有待审核或暂停申请可进入审核流。

审核完成后会向申请人写入一条 `matchmaker_application_reviewed` 站内通知：通过、驳回和暂停分别返回对应的处理结果。通知写入与申请审核在同一数据库事务中提交。

## 8. 错误响应

```json
{"detail":"申请红娘或合伙人前必须绑定手机号并完成实名认证"}
```

| HTTP | 触发条件 |
| --- | --- |
| `401` | 未登录或会话失效 |
| `403` | 未绑定手机号、未实名或无管理员权限 |
| `404` | 申请不存在 |
| `409` | 同类型申请状态冲突 |
| `422` | 枚举、手机号、长度或审核参数不合法 |

## 9. 变更记录

### 2026-07-22

- 修改 `PATCH /api/v1/admin/matchmaker/applications/{application_id}`：审核通过、驳回或暂停后新增申请人站内通知。
- 影响范围：申请人通知列表；原有申请响应结构和状态枚举保持兼容。

### 2026-07-27

- 修改 `POST /api/v1/matchmaker/applications`：新增 `application_details`，保存微信号、头像、擅长领域、期望价格和脱敏成功案例。
- 兼容旧客户端：仍接受旧版顶层字段 `wechat`、`avatar`、`specialties`、`expected_price`、`success_cases`，服务端会归一化到 `application_details`。
- 修改申请响应：新增 `application_details`；该字段只对申请人本人和管理员返回，不进入公开红娘列表或详情。
