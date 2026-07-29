# 积分接口

积分摘要返回当前余额、累计获得和累计扣减；积分流水返回每笔变动后的余额及原因。申请、资料查看等免费或套餐额度使用记录写入 `user_quota_usage`，用于按用户、功能和日期审计次数。

`GET /api/v1/users/me/quotas` 返回申请、资料查看、爆灯和纸飞机的每日额度、已用次数、每日剩余次数、积分兑换剩余次数及总剩余次数。

积分永久有效，不可提现、转账或由前端修改。所有列表稳定分页，返回 `items`、`page`、`page_size`、`total`、`has_more`。

| Method | URL | 说明 |
|---|---|---|
| GET | `/api/v1/users/me/points` | 返回 `balance`、`total_earned`、`total_spent` |
| GET | `/api/v1/users/me/points/ledger?page=1&page_size=20` | 查询积分流水 |
| POST | `/api/v1/users/me/checkin` | 每日签到 5 分，重复签到幂等 |
| GET | `/api/v1/users/me/tasks` | 查询资料完成、实名认证任务 |
| POST | `/api/v1/users/me/tasks/{task_code}/claim` | 领取已完成任务奖励 |
| GET | `/api/v1/users/me/invites?page=1&page_size=20` | 查询本人邀请记录 |
| GET | `/api/v1/points/products` | 查询可兑换商品和权益 |
| POST | `/api/v1/users/me/points/redeem` | 扣除积分并创建兑换订单 |

当前任务奖励：资料完整度 100% 奖励 50 分，实名认证状态为 2 奖励 100 分。任务只能领取一次；余额与流水在同一事务中写入，当前实现只做收入类积分，积分兑换接口按设计预留到后续阶段。

签到和任务奖励可通过 `POINT_CHECKIN_REWARD`、`POINT_PROFILE_COMPLETE_REWARD`、`POINT_REALNAME_VERIFIED_REWARD` 配置。积分商品的每次消耗可通过对应的 `POINT_COST_<功能编码>` 配置，例如 `POINT_COST_EXTRA_APPLY`；配置值会覆盖 `config_point_product.points_cost`，商品的库存、上下架和权益内容仍以数据库为准。

兑换请求体为 `{"product_code":"extra_apply"}`，必须带 `Idempotency-Key`（1~128 字符）。商品由 `config_point_product` 配置，商品类型可为 `goods` 或 `right`；服务端校验库存和余额，在同一事务中扣除积分、写入兑换订单并减少库存。兑换订单初始状态为 0（待履约），实物发货或权益发放由后台履约流程处理；同一用户同一幂等键重复请求不会重复扣分。
