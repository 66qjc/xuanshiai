# 用户在线状态接口

在线状态采用 Redis 会话心跳和 MySQL 最近活跃时间投影：前端建议每 30 秒调用一次心跳，Redis Key 90 秒过期；MySQL `user_profile.last_active_at` 最多每 60 秒更新一次。

## 刷新心跳

`POST /api/v1/users/me/presence/heartbeat`

需要登录，服务端根据当前 Access Token 的会话 ID 刷新当前设备的在线 Key。成功返回：

```json
{
  "status": 1,
  "last_active_at": "2026-07-23T12:00:00",
  "heartbeat_interval_seconds": 30,
  "expires_after_seconds": 90
}
```

Redis 不可用时返回 `503`，前端应稍后重试；不会把 Redis 错误伪装成在线成功。

## 设置在线或隐身

`POST /api/v1/users/me/presence/status`

请求体：`{"status":1}` 或 `{"status":2}`。`1` 为在线，`2` 为隐身。退出登录由服务端自动清理当前会话的在线状态，不能通过此接口伪造离线。

## 查询我的状态

`GET /api/v1/users/me/presence`

需要登录，返回当前会话状态、最近活跃时间、心跳间隔和过期秒数。

## 状态规则

- 多设备分别保存会话 Key；退出一个设备不会影响其他设备。
- 全部会话退出后，服务端写入离线。
- 心跳过期后展示离线；推荐卡片最多依据最近 90 秒活跃时间展示在线。
- `hide_online_status=true` 时，对外推荐卡片统一返回离线；本人查询仍返回自己的真实状态。
- 会员不能绕过在线状态隐私设置。
