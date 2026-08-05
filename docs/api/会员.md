# 会员与测试支付接口

## 业务规则

- 三档套餐共享同一个 VIP 标识，默认赠送 3/5/20 次申请；无免费试用，必须购买后生效。
- 普通用户每日申请 3 次，会员每日申请次数为 3 次基础额度加套餐赠送次数。
- 普通用户每日查看他人资料 8 次，会员每日 20 次。
- 购买接口只创建待支付订单；会员必须在服务端完成支付履约后开通。
- development/testing 使用本地测试支付接口，不调用微信；生产环境禁止使用测试支付。
- `GET /api/v1/users/me/quotas` 可查询当前用户各项功能的每日额度和额外兑换额度。

会员只有 VIP 一种等级，套餐由 `config_membership_package` 的上架数据决定。支付方式固定微信支付，服务端决定价格、有效天数和权益。

## 接口

| Method | URL | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/membership/packages` | 无 | 查询在售套餐 |
| GET | `/api/v1/users/me/membership` | 登录 | 当前有效期和权益 |
| GET | `/api/v1/users/me/membership/history?page=1&page_size=20` | 登录 | 会员历史分页 |
| POST | `/api/v1/membership/orders` | 登录 | 创建待支付测试订单 |
| GET | `/api/v1/membership/orders/{order_no}` | 订单本人 | 查询订单 |
| POST | `/api/v1/payments/test/pay` | 登录（仅 development/testing） | 模拟支付成功/失败并触发订单履约 |
| POST | `/api/v1/payments/wechat/callback` | 微信服务端 | 支付回调边界 |

会员购买接口会创建待支付订单。测试环境调用 `POST /api/v1/payments/test/pay`，请求 `{"order_no":"...","success":true}` 即可完成测试支付并开通会员；重复调用幂等。该接口不能在 staging/production 使用。

当前会员权益：普通用户每日申请 3 次、爆灯 1 次、浏览 20 次；VIP 每日申请 10 次、爆灯 3 次、普通浏览不限、可看历史浏览和访客详情。会员不能绕过手机号、资料完整、实名认证、隐私、拉黑和封禁规则。

套餐价格支持环境变量覆盖数据库套餐配置：`MEMBERSHIP_MONTHLY_PRICE`、`MEMBERSHIP_QUARTERLY_PRICE`、`MEMBERSHIP_YEARLY_PRICE`，以及对应的 `_ORIGINAL_PRICE` 和 `_DAILY_PRICE`。未配置时使用 `config_membership_package` 中的值。

微信回调在商户 API v3 密钥和验签适配器配置前返回 `503`，不会把前端请求或未验签回调标记为支付成功，也不会发放会员。
