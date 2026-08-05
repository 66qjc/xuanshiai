# 一期订单、分成、账本和提现接口

本组接口只实现订单记录、规则配置、沙箱结算和提现申请。真实微信支付回调、微信转账和生产分账必须通过供应商适配器接入，不能使用测试接口替代。

## 用户接口

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/finance/orders` | 已登录用户 | 创建待支付订单 |
| `GET` | `/finance/balance` | 已登录用户 | 查询待结算和可用余额 |
| `GET` | `/finance/commission-entries` | 已登录用户 | 查询自己的分成明细 |
| `POST` | `/finance/withdrawals` | 已登录用户 | 申请提现 |

创建订单：

```json
{"product_type":1,"product_name":"会员服务","amount":"99.00"}
```

金额使用十进制定点值，后端按分保存，不使用浮点数。新订单状态为 `0` 待支付，真实支付成功后才允许结算。

## 管理接口

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/admin/finance/commission-rules` | 超级管理员 | 创建一条分成规则版本 |
| `GET` | `/admin/finance/commission-rules` | 超级管理员 | 查询生效规则 |
| `POST` | `/admin/finance/product-commission-rules/{product_id}` | 超级管理员 | 配置商品分成对象和比例 |
| `POST` | `/admin/finance/orders/{order_id}/settle` | 超级管理员 | 对已支付订单生成分成 |
| `POST` | `/admin/finance/orders/{order_id}/refund` | 超级管理员 | 退款并生成分成冲正流水 |
| `POST` | `/admin/finance/commission-entries/{entry_id}/release` | 超级管理员 | 将待结算分成释放为可用 |
| `PATCH` | `/admin/finance/withdrawals/{withdrawal_id}` | 超级管理员 | 审核提现 |
| `GET` | `/admin/finance/report` | 超级管理员 | 查询分成汇总报表 |

规则示例：

```json
{"beneficiary_type":"promoter","name":"推广消费分成","mode":"rate","rate_percent":"10.0000","priority":10}
```

支付成功时必须固化规则 ID、规则版本、归属主体、订单实收基数和分成金额；之后修改规则不得影响历史订单。商品未明确配置的分成主体不得自动参与分成。所有主体分成合计不得超过订单可分成金额。`POST /admin/finance/orders/{order_id}/settle` 是开发/测试环境的沙箱确认接口，生产环境返回 `503`，必须由真实支付回调确认订单成功。

账本状态：`PENDING`、`AVAILABLE`、`REVERSED`。管理员释放分成时只允许 `PENDING -> AVAILABLE`；订单退款会关闭关联红娘服务、隐藏联系方式、关闭未完成约见、取消未开始约会，将分成标记为冲正并写入反向账本流水。提现申请状态：`PENDING_REVIEW -> APPROVED -> PROCESSING -> SUCCEEDED`，拒绝或失败会写入反向账本流水，原流水不会删除。提现创建时会锁定同一账户的账本行，防止并发超额提现。

商品分成配置示例：

```json
{"beneficiary_type":"service_matchmaker","mode":"rate","rate_percent":"30.0000"}
```

同一商品和主体创建新配置时，旧配置会停用并递增版本。红娘商品结算只读取该商品的有效配置。

错误：`401` 未登录，`403` 无权限，`404` 订单/提现不存在，`409` 订单状态冲突、余额不足或分成超过订单金额，`422` 金额和规则参数不合法。
