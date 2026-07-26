# 一期约见接口

接口前缀为 `/api/v1`，需要登录并绑定手机号。约见反馈只返回写入成功，不提供公开反馈查询接口。

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/matchmaker/meetings/requests` | 已登录用户 | 已废弃，普通用户不能直接发起约见 |
| `POST` | `/matchmaker/meetings/requests/from-service` | 已认证服务红娘 | 基于红娘服务单发起约见 |
| `GET` | `/matchmaker/meetings/requests/mine` | 已登录用户 | 查询自己发出或收到的申请 |
| `PATCH` | `/matchmaker/meetings/requests/{request_id}` | 申请参与方 | 接受、拒绝、联系或关闭申请 |
| `POST` | `/admin/matchmaker/meetings/requests/{request_id}/schedule` | 超级管理员 | 安排具体约会 |
| `POST` | `/matchmaker/meetings/{meeting_id}/feedback` | 约会参与方 | 提交私有反馈 |

提交申请：

```json
{"target_user_id":23,"note":"希望在门店进行首次沟通"}
```

申请状态：`SUBMITTED`、`CONTACTED`、`ACCEPTED`、`DECLINED`、`CLOSED`。拒绝或关闭必须填写 `reason`。只有 `ACCEPTED` 才能由管理员安排约会。

红娘基于服务单发起约见时，服务状态必须为 `1=服务中` 或 `2=服务完成`；`3=已取消/退款`、服务过期、红娘停用或账号封禁时禁止发起。约见记录必须保留 `service_id` 和 `matchmaker_id`，以便退款、责任和分成追溯。

约会状态：`SCHEDULED`、`REMINDED`、`CHECKED_IN`、`COMPLETED`、`CANCELLED`、`NO_SHOW`。反馈只有约会进入 `CHECKED_IN` 或 `COMPLETED` 后允许提交。
