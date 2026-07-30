# 红娘（牵线）一期接口

接口前缀：`/api/v1`。

一期范围：

- 服务红娘列表、详情和排行榜。
- 付费牵线和私人定制商品、订单和支付成功后的服务开通。
- 向已付款用户受控交付服务红娘微信。
- 用户查询自己的申请。
- 已通过审核的服务红娘查询和处理分配给自己的申请。
- 超级管理员查询、分配和处理牵线服务申请。
- 红娘申请提交、我的申请和管理员审核继续使用 `docs/api/identity.md` 中的接口。

二期范围：人脸第三方接入、会员权益扩展、积分兑换红娘服务、AI 红娘和实时媒体能力。

> **业务边界（2026-07-24）**：红娘商业化服务只有 `1=付费牵线` 和 `3=私人定制`，新用户没有免费红娘次数，红娘服务次数只能现金购买。用户之间的“申请认识”属于 `docs/api/social.md` / `docs/api/discovery.md` 的独立流程，不得复用本模块的服务订单。

## 1. 认证和权限

- 公开接口：红娘列表、详情、排行榜。
- 登录用户：查询商品、创建待支付订单、查询订单和已支付服务。
- 有效服务红娘角色：查询分配给自己的申请、处理自己的申请。
- 超级管理员：查询、分配和处理全部牵线申请。当前系统使用 `user_role.role_code=admin` 表示超级管理员。
- 创建订单要求实名认证通过；订单支付成功后服务申请默认通过，不等待红娘审批。
- 拉黑、封禁和用户状态校验必须在业务服务层执行，不能只依赖前端。

通用请求头：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

## 2. 查询服务红娘列表

### `GET /api/v1/matchmakers`

- 权限：公开。
- 用途：查询审核通过、账号正常且有效服务红娘角色的列表。
- Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `page` | integer | 否 | `1` | 1~1000 | 页码 |
| `page_size` | integer | 否 | `20` | 1~50 | 每页数量 |

请求示例：

```http
GET http://127.0.0.1:8000/api/v1/matchmakers?page=1&page_size=20
```

成功响应 `200`：

```json
{
  "items": [
    {
      "user_id": 12,
      "nickname": "红娘小张",
      "avatar": "/storage/uploads/12/avatar.webp",
      "application_type": "service_matchmaker",
      "intro": "有多年婚恋咨询和沟通经验",
      "certification_tags": ["平台认证"],
      "success_count": 3,
      "rating_score": 4.8,
      "rating_count": 5,
      "is_available": true
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "has_more": false
}
```

字段说明：`success_count` 为已完成服务数；`rating_score` 无评价时为 `0`；`rating_count` 为评价数量；`is_available` 只有有效服务红娘角色才为 `true`；资质原图不会通过公开接口返回，只返回认证标签。

## 3. 查询服务红娘详情

### `GET /api/v1/matchmakers/{matchmaker_id}`

- 权限：公开。
- Path 参数：`matchmaker_id`，正整数，红娘用户 ID。
- 资源规则：只返回审核通过、账号正常且服务红娘角色有效的红娘。

请求示例：

```http
GET http://127.0.0.1:8000/api/v1/matchmakers/12
```

成功响应字段与列表中的单个 `MatchmakerCard` 相同。

错误：

```json
{"detail":"服务红娘不存在或暂不可用"}
```

- `404`：红娘不存在、申请未通过、角色暂停或账号不可用。
- `422`：`matchmaker_id` 小于 1 或格式错误。

## 4. 查询热心红娘排行榜

### `GET /api/v1/matchmakers/ranking`

- 权限：公开。
- Query 参数：同红娘列表。
- 排序：已完成服务数降序、评分降序、审核时间降序、申请 ID 降序。
- 一期不实现收费排序和会员曝光；排行榜只使用已有完成服务和评价数据。

请求示例：

```http
GET http://127.0.0.1:8000/api/v1/matchmakers/ranking?page=1&page_size=10
```

返回结构与 `GET /api/v1/matchmakers` 相同。

## 5. 查询红娘服务商品

### `GET /api/v1/matchmaker/service-products`

- 权限：公开。
- 返回：仅返回上架的付费牵线（`service_type=1`）和私人定制（`service_type=3`）。免费热心红娘（旧值 `2`）不再创建新订单。

成功响应 `200`：

```json
[
  {
    "id": 1,
    "code": "paid_matchmaking",
    "name": "付费牵线",
    "service_type": 1,
    "price": "99.00",
    "description": "支付后获得指定红娘微信服务",
    "active": true,
    "created_at": "2026-07-24T10:00:00",
    "updated_at": "2026-07-24T10:00:00"
  }
]
```

管理员使用同一路径的 `POST` 创建商品，要求超级管理员权限：

```json
{
  "code": "paid_matchmaking",
  "name": "付费牵线",
  "service_type": 1,
  "price": "99.00",
  "description": "支付后获得指定红娘微信服务",
  "active": true
}
```

## 6. 创建红娘服务订单

### `PATCH /api/v1/matchmaker/service-products/{product_id}`

- 权限：管理员。
- 作用：修改商品名称、服务类型、价格、描述，或通过 `active=false` 下架商品。已支付订单保留原商品快照，不受后续改价影响。
- Body：所有字段可选，但至少提供一个字段；`service_type` 仅支持 `1=付费牵线` 或 `3=私人定制`，`price` 必须大于 0。
- 下架商品不会出现在用户商品列表，也不能创建新订单；已创建的待支付订单仍按订单状态处理。

```json
{
  "price": "199.00",
  "active": true
}
```

成功响应：`200`，返回完整商品对象。商品不存在返回 `404`，请求字段不合法返回 `422`。

### `POST /api/v1/matchmaker/service-requests/orders`

- 权限：登录且实名认证通过。
- Headers：`Authorization: Bearer <access_token>`、`Idempotency-Key: <8-128字符>`、`Content-Type: application/json`。
- 业务：用户选择红娘和商品后创建待支付订单。服务端根据商品配置决定金额，不能相信前端金额。

请求体：

```json
{
  "product_id": 1,
  "matchmaker_id": 12,
  "requirement": "希望寻找认真稳定的婚恋关系，年龄和城市可以适当放宽"
}
```

成功响应 `201`：

```json
{
  "id": 31,
  "order_no": "XM202607240001abcd",
  "user_id": 23,
  "matchmaker_id": 12,
  "product_id": 1,
  "service_type": 1,
  "product_name": "付费牵线",
  "amount": "99.00",
  "status": 0,
  "service_request_id": null,
  "created_at": "2026-07-24T10:00:00",
  "pay_time": null
}
```

状态：`0=待支付`、`1=支付成功`、`2=支付失败`、`3=已退款`。相同用户重复使用同一个 `Idempotency-Key` 返回同一订单，不重复创建订单。

## 7. 查询红娘服务订单

### `GET /api/v1/matchmaker/service-requests/orders/{order_no}`

- 权限：订单所属用户。
- 其他用户访问返回 `404`，不得通过订单号查询他人订单。

## 8. 支付成功后开通红娘服务

支付成功后由支付回调或受控的测试结算流程调用服务开通逻辑：

```text
支付成功
-> 校验服务端订单金额和支付状态
-> 创建红娘服务申请
-> 默认通过服务申请
-> 通知指定红娘
-> 红娘提交微信
-> 用户查看红娘微信
```

同一支付订单只能开通一次服务。当前真实支付回调仍需微信支付验签配置；生产环境不能使用沙箱结算接口代替真实支付。

## 9. 提交已支付订单对应的服务申请

### `POST /api/v1/matchmaker/service-requests`

- 权限：登录用户。
- Content-Type：`application/json`。
- 前置条件：用户账号正常、实名认证通过、订单支付成功、目标红娘有效。
- 该接口用于支付成功后的幂等开通，不是免费申请入口。
- `requirement` 必须与创建订单时保存的服务需求完全一致；历史订单未保存需求时，首次开通会补写该字段。

请求字段：

| 字段 | 位置 | 类型 | 必填 | 规则 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `order_no` | body | string | 是 | 8~64 字符，必须属于当前用户且已支付 | 红娘服务订单号 |
| `requirement` | body | string | 是 | 10~2000 字符 | 用户牵线需求 |

请求示例：

```http
POST http://127.0.0.1:8000/api/v1/matchmaker/service-requests
Authorization: Bearer <user_access_token>
Content-Type: application/json
```

```json
{
  "order_no": "XM202607240001abcd",
  "requirement": "希望寻找认真稳定的婚恋关系，年龄和城市可以适当放宽"
}
```

成功响应 `201`：

```json
{
  "id": 31,
  "user_id": 23,
  "matchmaker_id": 12,
  "service_type": 1,
  "order_id": 31,
  "product_id": 1,
  "status": 0,
  "requirement": "希望寻找认真稳定的婚恋关系，年龄和城市可以适当放宽",
  "feedback": null,
  "created_at": "2026-07-22T10:00:00",
  "updated_at": "2026-07-22T10:00:00",
  "start_at": null,
  "end_at": null
}
```

状态：`0` 已通过待开始、`1` 服务中、`2` 服务完成、`3` 已取消/退款。服务成功表示已开通红娘服务，不表示用户已经脱单。

业务顺序：校验账号/实名/红娘/拉黑关系 -> 查询重复申请并加行锁 -> 创建服务申请 -> 写入红娘通知 -> 提交事务。通知失败不能造成重复申请，后续可加入通知重试。

错误：

| HTTP | 触发条件 | 示例 |
| --- | --- | --- |
| `401` | 未登录或 Token 失效 | `请先登录` |
| `403` | 未实名认证、账号受限 | `提交牵线申请前必须完成实名认证` |
| `404` | 订单、商品或红娘不存在 | `红娘服务订单不存在` |
| `409` | 订单未支付、服务已开通或需求与订单不一致 | `红娘服务订单尚未支付成功` / `服务需求与订单需求不一致，请重新提交` |
| `422` | 订单不是红娘服务订单或参数非法 | `订单不是红娘服务订单` |

## 10. 红娘微信交付

### `PATCH /api/v1/matchmaker/service-requests/{service_id}/contact`

- 权限：被分配的服务红娘。
- Body：`{"wechat_contact":"matchmaker_wechat"}`。
- 作用：提交或更新自己的微信联系方式。
- 已取消/退款的服务不能提交联系方式。

### `GET /api/v1/matchmaker/service-requests/{service_id}/contact`

- 权限：已付款服务的所属用户。
- 作用：查看指定红娘提交的微信联系方式。
- 红娘尚未提交时返回 `409`；其他用户访问返回 `404`。
- 查看行为写入业务审计日志。该接口不返回用户之间的联系方式。

成功响应：

```json
{
  "service_id": 31,
  "matchmaker_id": 12,
  "wechat_contact": "matchmaker_wechat",
  "delivered_at": "2026-07-24T10:30:00"
}
```

### `GET /api/v1/matchmaker/service-requests/contact-exchanges/{exchange_id}/contacts`

- 权限：联系方式交换双方。
- 前置条件：交换状态必须为 `DELIVERED`，即双方都已明确同意。
- 作用：查看双方已授权的手机号；访问行为写入业务审计日志。
- 未完成双方授权时返回 `409`；非交换参与方返回 `404`。

成功响应 `200`：

```json
{
  "exchange_id": 8,
  "source_user_id": 23,
  "target_user_id": 41,
  "source_phone": "13800138000",
  "target_phone": "13900139000",
  "delivered_at": "2026-07-27T10:30:00"
}
```

联系方式交换状态流：

```text
PENDING -> ONE_SIDE_CONSENT -> DELIVERED
PENDING/ONE_SIDE_CONSENT -> REVOKED
已退款服务 -> HIDDEN
```

## 11. 查询我的牵线申请

### `GET /api/v1/matchmaker/service-requests/mine`

- 权限：登录用户。
- Query 参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。
- 资源范围：只返回当前用户作为申请人的记录。

请求示例：

```http
GET http://127.0.0.1:8000/api/v1/matchmaker/service-requests/mine?page=1&page_size=20
Authorization: Bearer <user_access_token>
```

返回分页结构：`items` 为服务申请数组，`total` 为总数，`has_more` 表示是否还有下一页。

## 12. 查询分配给我的申请

### `GET /api/v1/matchmaker/service-requests/assigned`

- 权限：登录用户且必须拥有有效 `service_matchmaker` 角色。
- Query 参数：`page` 1~1000，默认 1；`page_size` 1~50，默认 20。
- 资源范围：只返回 `matchmaker_id` 等于当前用户的记录。

错误：

```json
{"detail":"当前用户不是有效服务红娘"}
```

## 8. 红娘处理服务申请

### `PATCH /api/v1/matchmaker/service-requests/{service_id}`

- 权限：登录用户且必须是该申请被分配的服务红娘。
- Path 参数：`service_id`，正整数。
- 状态只能按 `0待接单 -> 1服务中 -> 2服务完成` 或 `0/1 -> 3已取消/退款` 流转。
- `status=1` 自动写入 `start_at`。
- `status=2/3` 自动写入 `end_at`。
- `status=2/3` 必须填写 `feedback`。

请求：

```http
PATCH http://127.0.0.1:8000/api/v1/matchmaker/service-requests/31
Authorization: Bearer <matchmaker_access_token>
Content-Type: application/json
```

```json
{"status":1,"feedback":"已接单，将先确认你的择偶要求"}
```

完成示例：

```json
{"status":2,"feedback":"已完成初步沟通并给出匹配建议"}
```

错误：

- `403`：不是被分配的服务红娘。
- `404`：服务申请不存在。
- `409`：申请已完成或已取消。
- `422`：状态值非法或完成/取消未填写处理说明。

## 9. 管理员查询服务申请

### `GET /api/v1/admin/matchmaker/service-requests`

- 权限：超级管理员。
- Query 参数：`status` 可选 `0~3`；`page` 1~1000；`page_size` 1~50。
- 返回所有服务申请的分页列表。

请求示例：

```http
GET http://127.0.0.1:8000/api/v1/admin/matchmaker/service-requests?status=0&page=1&page_size=20
Authorization: Bearer <admin_access_token>
```

## 10. 管理员分配或处理服务申请

### `PATCH /api/v1/admin/matchmaker/service-requests/{service_id}`

- 权限：超级管理员。
- 可以分配有效服务红娘、修改状态或补充处理说明。
- `matchmaker_id` 必须是当前有效 `service_matchmaker` 角色用户。
- `status=2/3` 必须填写 `feedback`。

请求示例：

```http
PATCH http://127.0.0.1:8000/api/v1/admin/matchmaker/service-requests/31
Authorization: Bearer <admin_access_token>
Content-Type: application/json
```

```json
{
  "matchmaker_id": 12,
  "status": 1,
  "feedback": "已分配给服务红娘小张"
}
```

错误：

- `401`：未登录或 Token 失效。
- `403`：当前用户不是超级管理员。
- `404`：服务申请不存在。
- `422`：红娘角色无效、状态非法或完成/取消未填写说明。

## 11. 一期状态和权限矩阵

| 操作 | 普通登录用户 | 服务红娘 | 超级管理员 |
| --- | --- | --- | --- |
| 查看红娘列表/详情/排行 | 是 | 是 | 是 |
| 提交牵线申请 | 实名后 | 是 | 是 |
| 查看我的申请 | 只能本人 | 只能本人 | 可全部查询 |
| 查看分配申请 | 否 | 只能本人被分配项 | 可全部查询 |
| 处理服务申请 | 否 | 只能本人被分配项 | 可处理全部 |
| 分配红娘 | 否 | 否 | 是 |
| 查看申请人联系方式 | 一期不返回 | 按业务授权 | 后台按治理需要 |

## 12. 联系方式授权交换

红娘微信交付和用户双方联系方式交换是两条不同流程。`PATCH /matchmaker/service-requests/{service_id}/contact` 只交付红娘自己的微信，不代表双方用户已经同意交换联系方式。

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/matchmaker/service-requests/{service_id}/contact-exchanges` | 被分配的服务红娘 | 创建双方联系方式授权申请 |
| `GET` | `/matchmaker/service-requests/contact-exchanges/{exchange_id}` | 交换双方 | 查询授权状态 |
| `PATCH` | `/matchmaker/service-requests/contact-exchanges/{exchange_id}` | 交换双方 | `CONSENT` 同意或 `REVOKE` 撤回 |

授权状态为 `PENDING`、`ONE_SIDE_CONSENT`、`APPROVED`、`DELIVERED`、`REVOKED`、`HIDDEN`。只有双方都同意后才能进入 `APPROVED`，退款、拉黑、账号注销或风控处理会进入 `HIDDEN`。已交付记录不删除，只停止后续展示并保留审计记录。

## 13. 现有模糊点

- 具体哪些功能必须实名认证仍待产品确认；本模块当前提交牵线申请先要求实名认证。
- 一期是否允许收费红娘、会员、积分和爆灯尚未实现，当前 `service_type=2` 固定为基础/免费牵线。
- 私人定制、付费咨询、预约日期、支付和退款属于二期。
- 红娘资料认证后哪些字段不可修改仍待产品确认；当前一期通过申请审核状态控制展示。
- 红娘服务申请被拒绝、过期或用户主动撤回后的规则尚未冻结；当前一期只提供红娘处理和管理员处理，未增加用户撤回接口。
- 服务完成后的评价、评分反作弊和排行榜刷量治理只使用已有预留表，完整评价接口属于后续 P1/P2 任务。
- 超级管理员查看敏感材料、导出证据和聊天原文的二次确认及留存期限仍待确认。
- 申请认识次数、推荐算法、会员曝光和爆灯规则不在本期红娘服务接口中实现。

## 14. Swagger 手动测试前置条件

1. 启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

2. 打开 `http://127.0.0.1:8000/docs`。
3. 先准备三类账号：
   - 普通实名用户：用于提交牵线申请。
   - 已审核通过的服务红娘：用于查看和处理分配申请。
   - 超级管理员：用于查询、分配和治理申请。
4. 在 Swagger 页面点击右上角 `Authorize`，输入：

```text
Bearer <对应账号的 access_token>
```

5. 数据库中至少需要存在：
   - 一条 `user_matchmaker_apply.application_type='service_matchmaker'` 且 `status=1` 的申请。
   - 对应用户一条 `user_role.role_code='service_matchmaker'` 且 `status=1` 的角色。
   - 一个已完成实名认证的普通测试用户。

## 15. Swagger 手动测试顺序

### 场景 A：公开查看红娘

1. 不授权调用 `GET /api/v1/matchmakers`，确认返回 `200` 和分页结构。
2. 调用 `GET /api/v1/matchmakers/ranking`，确认返回排序字段。
3. 复制某个 `user_id`，调用 `GET /api/v1/matchmakers/{matchmaker_id}`。
4. 使用不存在的 ID，确认返回 `404 服务红娘不存在或暂不可用`。

### 场景 B：普通用户提交牵线申请

1. 使用普通实名用户 Token 调用 `POST /api/v1/matchmaker/service-requests`。
2. 请求体填写目标红娘 ID和不少于 10 个字符的需求。
3. 确认返回 `201`、`status=0`、`service_type=2`。
4. 再次提交相同目标红娘，确认返回 `409`，不会生成第二条处理中申请。
5. 调用 `GET /api/v1/matchmaker/service-requests/mine`，确认能查询刚创建的记录。
6. 使用未实名用户重复提交，确认返回 `403`。
7. 将目标改为自己的用户 ID，确认返回 `422`。

### 场景 C：服务红娘接单和完成

1. 使用服务红娘 Token 调用 `GET /api/v1/matchmaker/service-requests/assigned`。
2. 确认能看到分配给自己的待接单记录。
3. 调用 `PATCH /api/v1/matchmaker/service-requests/{service_id}`：

```json
{"status":1,"feedback":"已接单，开始沟通"}
```

4. 确认返回 `status=1` 且 `start_at` 不为空。
5. 再次调用同一接口：

```json
{"status":2,"feedback":"已完成初步沟通"}
```

6. 确认返回 `status=2` 且 `end_at` 不为空。
7. 对已完成记录再次修改，确认返回 `409`。
8. 使用另一个普通用户 Token 处理该记录，确认返回 `403`。

### 场景 D：超级管理员分配和处理

1. 使用管理员 Token 调用 `GET /api/v1/admin/matchmaker/service-requests?status=0`。
2. 使用 `PATCH /api/v1/admin/matchmaker/service-requests/{service_id}` 分配有效服务红娘。
3. 使用不存在或未审核通过的用户 ID 作为 `matchmaker_id`，确认返回 `422`。
4. 管理员将申请置为 `status=1`，确认红娘在 assigned 列表中可以看到。
5. 使用普通用户 Token 调用管理员接口，确认返回 `403`。

### 场景 E：输入校验

逐项确认 Swagger 返回 `422`：

- `matchmaker_id=0`。
- `requirement` 少于 10 个字符。
- `requirement` 超过 2000 个字符。
- `status=9`。
- `status=2` 但不填写 `feedback`。
- `service_id` 非正整数。

## 16. 运行检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q app
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\pytest.exe
```

## 17. Shared query interfaces for the parent client

The parent application is a separate client and reuses this API with the parent user's own access token. This backend does not implement parent-side pages or an in-app parent workflow.

### `GET /api/v1/matchmaker/service-products/{product_id}`

- Authentication: public.
- Returns one active paid matchmaking or private customization product.
- Returns `404` for an unknown, inactive, or unsupported product.

### `GET /api/v1/matchmaker/service-requests/orders`

- Authentication: logged-in user.
- Query: `page` 1~1000, default `1`; `page_size` 1~50, default `20`.
- Returns only orders owned by the current account, with `items`, `page`, `page_size`, `total`, and `has_more`.

### `GET /api/v1/matchmaker/service-requests/{service_id}`

- Authentication: logged-in user.
- Returns the service record when the current account is the service owner or assigned service matchmaker.
- Other accounts receive `404` to avoid exposing service existence.
- Subsequent service delivery is confirmed through WeChat; this endpoint only exposes the order/service record.

### Change log: 2026-07-30

- Added public product detail lookup.
- Added paginated current-user order listing.
- Added authorized service detail lookup.
- Kept WeChat delivery as the post-purchase service boundary; no in-app matchmaker conversation or meeting workflow was added.
- The ranking `success_count` field is currently defined as the count of paid type-3 matchmaker orders whose linked service record is completed (`status=2`); it is not a confirmed matchmaking-success count.

如果使用 `uv run` 遇到本机 uv 缓存目录权限或路径错误，使用项目已有 `.venv` 中的工具执行上述检查。
