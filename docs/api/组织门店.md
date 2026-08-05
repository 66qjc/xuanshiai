# 一期组织、门店和归属接口

接口前缀为 `/api/v1`。除推广触点和加入团队接口外，组织配置接口要求超级管理员权限。所有接口使用 JSON，认证头为 `Authorization: Bearer <access_token>`。

## 门店和资源

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/organizations/stores` | 超级管理员 | 创建门店 |
| `GET` | `/organizations/stores` | 超级管理员 | 查询门店 |
| `GET` | `/organizations/stores/{store_id}` | 超级管理员 | 查询门店详情 |
| `POST` | `/organizations/stores/{store_id}/members` | 超级管理员 | 添加店长或门店红娘 |
| `POST` | `/organizations/assignments` | 超级管理员 | 分派会员门店和服务红娘 |

创建门店请求：

```json
{"code":"shanghai-01","name":"上海门店","display_name":"上海直营店","region_code":"310000","auto_redirect":false}
```

`code` 只能使用字母、数字、下划线和短横线；一个门店最多一个有效 `store_manager`。资源分派会结束同一会员的旧有效归属，历史记录保留。

## 推广和合伙团队

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `POST` | `/promotions/touches` | 已登录推广红娘 | 创建自己的推广码 |
| `POST` | `/promotions/attributions` | 已登录用户 | 确认自己的首次推广归属 |
| `POST` | `/partners/teams` | 超级管理员 | 创建合伙团队 |
| `POST` | `/partners/memberships` | 已登录推广红娘 | 加入一个合伙团队 |

推广归属只允许一个有效归属；重复确认返回 `409`。扫描合伙人码只应先记录触点，成为推广红娘并确认加入团队后才成为有效团队成员。

错误：`401` 未登录，`403` 业务身份或权限不足，`404` 组织/推广码/团队不存在，`409` 重复成员、重复归属或店长冲突，`422` 参数不合法。
