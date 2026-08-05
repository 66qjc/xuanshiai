# 测试支付、爆灯和置顶接口

接口前缀：`/api/v1`。测试支付仅允许 `development`/`testing` 环境，不调用微信或其他第三方支付。

## 通用测试支付

### `POST /api/v1/payments/test/pay`

Header：`Authorization: Bearer <token>`。

请求：

```json
{"order_no":"TEST...","success":true}
```

`success=false` 会将待支付订单置为支付失败；`success=true` 会将订单置为支付成功并触发对应履约。重复请求已支付订单返回相同成功状态，不重复开通权益。响应字段：`order_no`、`status`（0待支付、1成功、2失败）、`transaction_id`、`payment_required`、`fulfilled`。

## 置顶

| Method | URL | 说明 |
|---|---|---|
| GET | `/api/v1/boost/packages` | 查询测试套餐 |
| GET | `/api/v1/users/me/boost/status` | 查询当前置顶状态 |
| POST | `/api/v1/boost/orders` | 创建订单，需 `Idempotency-Key` |
| GET | `/api/v1/boost/orders/{order_no}` | 查询本人订单 |

套餐编码由服务端返回：`boost_1d`、`boost_7d`、`boost_30d`。创建订单请求为 `{"package_code":"boost_7d"}`。测试支付成功后，`user_boost` 写入当前用户的有效置顶记录，并影响推荐/广场排序。

## 爆灯支付

### `POST /api/v1/spotlights/payments`

请求体：`{"target_user_id":23}`，需 `Idempotency-Key`。金额由后端决定，当前测试金额为 5.00，不能由前端传入覆盖。支付订单创建后调用通用测试支付接口；成功履约后写入爆灯记录并通知目标用户。

### `GET /api/v1/spotlights/payments/{order_no}`

只允许订单创建者查询。原 `POST /api/v1/discovery/superlikes/{target_id}` 仍是免费额度爆灯，两者是不同业务。

## 错误处理

- `401`：未登录。
- `403`：测试支付在非开发/测试环境调用，或无权操作目标资源。
- `404`：套餐、目标用户或订单不存在。
- `409`：订单已失败/已退款/当前状态不可支付，或幂等键对应不同业务请求。
- `422`：参数格式、范围或套餐编码错误。
- `503`：仅真实支付链路的外部服务不可用；测试支付不依赖微信。
