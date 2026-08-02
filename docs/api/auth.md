# 用户认证接口

## 1. 通用约定

接口前缀：`/api/v1`。

需要登录的接口必须携带：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

成功响应没有统一 `data` 包装层，返回体就是接口定义的对象或数组。`204` 接口没有响应体。

登录、刷新 Token 和发送短信不需要登录；微信登录需要配置微信 `AppID` 和 `AppSecret`；短信登录需要配置短信服务。

## 1.1 已有账号测试登录

### `POST /api/v1/auth/test-login`

仅 `development`/`testing` 环境开放，用于没有短信或微信第三方 API 时使用手机号和密码登录已有账号。不会创建新账号：

```json
{"phone":"13900000001","password":"password123","device_id":"dev-device-01","platform":"devtools","app_version":"test"}
```

账号不存在返回 `404`，密码错误返回 `401`，冻结/注销账号返回 `403`。密码必须在 `users.password` 中保存为 bcrypt 哈希，不能保存明文。成功返回与手机号/微信登录相同的 `TokenResponse`，并遵守现有会话数量限制。`staging`/`production` 环境固定返回 `403`。

## 2. 发送短信验证码

### 基本信息

- URL：`POST /api/v1/auth/sms/send`
- 权限：公开
- Content-Type：`application/json`
- 成功状态：`202 Accepted`

### 请求参数

| 字段 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `phone` | body | string | 是 | 11 位大陆手机号，正则 `^1[3-9]\d{9}$` | 接收验证码的手机号 |
| `purpose` | body | string | 否 | `login` 或 `bind_phone`，默认 `login` | 验证码用途，不能跨用途使用 |

请求示例：

```json
{"phone":"13800138000","purpose":"login"}
```

非法示例：

```json
{"phone":"123","purpose":"login"}
```

### 返回参数

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `message` | string | 是 | 提示文本 |
| `expires_in` | integer | 是 | 验证码有效秒数，默认 300 |
| `retry_after` | integer | 是 | 同手机号再次发送前需要等待的秒数 |

成功响应：

```json
{"message":"验证码已发送","expires_in":300,"retry_after":60}
```

规则：同一手机号同一用途 60 秒内不能重复发送，24 小时最多 10 次；验证码成功校验后立即删除，重新发送后旧验证码失效；连续错误 3 次锁定 15 分钟。

## 3. 手机号验证码登录

### 基本信息

- URL：`POST /api/v1/auth/phone/login`
- 权限：公开
- Content-Type：`application/json`
- 成功状态：`200 OK`

### 请求参数

| 字段 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `phone` | body | string | 是 | 11 位大陆手机号 | 登录手机号 |
| `purpose` | body | string | 是 | 必须为 `login` | 防止绑定验证码用于登录 |
| `code` | body | string | 是 | 6 位数字 | 短信验证码 |
| `device_id` | body | string/null | 否 | 最长 128 字符 | 设备标识，用于会话管理 |
| `platform` | body | string/null | 否 | 最长 32 字符 | 客户端平台 |
| `app_version` | body | string/null | 否 | 最长 32 字符 | 客户端版本 |

请求示例：

```json
{
  "phone":"13800138000",
  "purpose":"login",
  "code":"123456",
  "device_id":"device-1",
  "platform":"wechat-mini-program",
  "app_version":"1.0.0"
}
```

### 返回参数

| 字段 | 类型 | 必返 | 含义 |
| --- | --- | --- | --- |
| `access_token` | string | 是 | 调用受保护接口的访问 Token |
| `refresh_token` | string | 是 | 刷新访问 Token 的长期 Token |
| `token_type` | string | 是 | 固定为 `bearer` |
| `expires_in` | integer | 是 | Access Token 有效秒数 |
| `need_bind_phone` | boolean | 是 | 是否需要绑定手机号，手机号登录成功后固定为 `false` |
| `user_id` | integer | 是 | 平台内部用户 ID |

成功响应：

```json
{
  "access_token":"<access-token>",
  "refresh_token":"<refresh-token>",
  "token_type":"bearer",
  "expires_in":3600,
  "need_bind_phone":false,
  "user_id":1
}
```

未注册手机号会自动创建普通用户；账号被冻结、停用或注销时返回 `403`。

## 4. 微信登录

### 基本信息

- URL：`POST /api/v1/auth/wechat/login`
- 权限：公开
- Content-Type：`application/json`
- 成功状态：`200 OK`

### 请求参数

| 字段 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `code` | body | string | 是 | 1~512 字符 | `wx.login()` 返回的临时凭证 |
| `nickname` | body | string/null | 否 | 最长 64 字符 | 微信昵称 |
| `avatar` | body | string/null | 否 | 最长 255 字符 | 微信头像地址 |
| `device_id` | body | string/null | 否 | 最长 128 字符 | 设备标识 |
| `platform` | body | string/null | 否 | 最长 32 字符 | 客户端平台 |
| `app_version` | body | string/null | 否 | 最长 32 字符 | 客户端版本 |

请求示例：

```json
{
  "code":"wx.login-return-code",
  "nickname":"用户昵称",
  "avatar":"https://example.com/avatar.jpg",
  "device_id":"device-1",
  "platform":"wechat-mini-program",
  "app_version":"1.0.0"
}
```

返回字段与手机号登录相同。`need_bind_phone=true` 时，前端必须引导绑定手机号；推荐、关系、聊天、社区和纸飞机接口会返回 `403`。

## 5. 绑定手机号

### 基本信息

- URL：`POST /api/v1/auth/bind-phone`
- 权限：登录用户
- Content-Type：`application/json`
- 成功状态：`204 No Content`

### 请求参数

请求体与手机号登录相同，但 `purpose` 必须为 `bind_phone`：

```json
{
  "phone":"13800138000",
  "purpose":"bind_phone",
  "code":"123456",
  "device_id":"device-1"
}
```

响应无 Body。一个手机号只能绑定一个账号，已绑定其他账号返回 `409`，不会覆盖原账号。

## 6. 刷新 Token

### `POST /api/v1/auth/refresh`

- 权限：公开，但必须提供有效 Refresh Token
- 请求体：

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `refresh_token` | string | 是 | 20~512 字符 | 当前会话的刷新 Token |

请求：

```json
{"refresh_token":"<refresh-token>"}
```

响应字段与登录接口一致。刷新成功后旧 Refresh Token 立即撤销并轮换为新 Token。

## 7. 退出登录和当前用户

### `POST /api/v1/auth/logout`

- 权限：可选登录态
- 请求体：无
- 成功：`204 No Content`

撤销当前 Token 对应的会话，重复调用不会报错。

### `POST /api/v1/auth/logout-all`

- 权限：登录用户
- 请求体：无
- 成功：`204 No Content`

撤销当前用户的全部有效设备会话。

### `GET /api/v1/auth/me`

响应字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | integer | 当前用户 ID |
| `phone_masked` | string/null | 脱敏手机号，如 `138****0000` |
| `nickname` | string/null | 昵称 |
| `avatar` | string/null | 头像地址 |
| `status` | integer | 账号状态 |
| `phone_verified` | boolean | 是否已验证手机号 |
| `realname_status` | integer | 实名状态，`0` 未认证、`1` 审核中、`2` 通过、`3` 失败 |
| `need_bind_phone` | boolean | 是否需要绑定手机号 |

## 8. 协议

### `GET /api/v1/auth/agreements`

公开接口，无请求参数。返回当前发布协议版本：

```json
{
  "user_service":{"type":"user_service","version":"v1"},
  "privacy_policy":{"type":"privacy_policy","version":"v1"},
  "safety_pledge":{"type":"safety_pledge","version":"v1"},
  "community_rules":{"type":"community_rules","version":"v1"}
}
```

### `POST /api/v1/auth/agreements/accept`

- 权限：登录用户
- 成功：`204 No Content`

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `agreement_type` | string | 是 | `user_service`、`privacy_policy`、`safety_pledge`、`community_rules` | 协议类型 |
| `version` | string | 是 | 最长 32 字符，必须是当前发布版本 | 协议版本 |
| `content_hash` | string/null | 否 | 64 位哈希 | 前端展示内容哈希 |
| `scene` | string/null | 否 | 最长 32 字符 | 签署场景 |

示例：

```json
{"agreement_type":"safety_pledge","version":"v1","content_hash":"<64-char-sha256>","scene":"profile"}
```

签署 `safety_pledge` 会同步记录单身承诺完成状态。

## 9. 实名认证

### `POST /api/v1/auth/realname`

- 权限：登录用户
- 成功：`200 OK`

| 字段 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- |
| `real_name` | string | 是 | 2~64 字符 | 实名姓名 |
| `id_card` | string | 是 | 合法 18 位身份证格式 | 身份证号；仅加密保存，不回传原文 |

请求：

```json
{"real_name":"张三","id_card":"110101199001011234"}
```

响应：

```json
{"status":1,"id_card_masked":"1101**********1234"}
```

`status=1` 表示审核中；当前代码完成格式、年龄和重复认证校验，第三方二要素服务接入后才能完成姓名与身份证匹配。

## 10. 错误响应

错误响应统一为：

```json
{"detail":"验证码已过期"}
```

## 协议签署

`GET /api/v1/auth/agreements` 返回当前发布的协议类型和版本；`POST /api/v1/auth/agreements/accept` 记录签署。

`POST /api/v1/auth/agreements/accept`

请求：`{"agreement_type":"safety_pledge","version":"v1","content_hash":"<sha256>","scene":"profile"}`。版本必须与服务端当前发布版本一致，记录用户、版本、时间、IP、设备和场景。

## 实名认证

`POST /api/v1/auth/realname`

请求：`{"real_name":"张三","id_card":"110101199001011234"}`。服务端校验身份证格式和年满 18 周岁，身份证原文加密保存，提交后进入审核中：`{"status":1,"id_card_masked":"1101**********1234"}`。一期不伪造第三方认证成功；接入二要素服务后由认证适配器更新为已认证（`status=2`）。

## 个人资料

- `GET /api/v1/users/me/profile`
- `PATCH /api/v1/users/me/profile`
- `GET /api/v1/users/me/completion`

出生日期由服务端计算年龄且必须满 18 周岁；性别首次提交后不可自行修改。完整的资料、媒体、择偶要求和主页预览接口见 [`docs/api/profile.md`](profile.md)。

签署当前版本的 `safety_pledge` 协议后，会同步记录用户的单身承诺完成状态。

## 错误码

| HTTP | 场景 |
| --- | --- |
| `400` | 验证码错误、过期或微信凭证无效 |
| `401` | Token 无效、过期或会话已撤销 |
| `403` | 账号冻结/注销或权限不足 |
| `409` | 手机号冲突、协议版本过期、认证信息不可修改 |
| `422` | 请求格式、长度、范围或成年人校验失败 |
| `429` | 验证码频率、次数或错误锁定限制 |
| `503` | 微信或短信服务未配置/不可用 |
# 认证接口

## 测试环境 Mock

没有短信服务商或微信小程序配置时，可以在 `development` 或 `testing` 环境配置：

```env
SMS_PROVIDER=mock
SMS_MOCK_CODE=123456
WECHAT_PROVIDER=mock
```

Mock 短信验证码固定为 `123456`。Mock 微信登录请求中的 `code` 使用 `mock-code-001` 等格式，同一个后缀会映射到同一个测试用户。生产环境禁止启用 Mock，接口路径、请求结构和响应结构与真实服务保持一致。
