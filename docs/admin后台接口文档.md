# 管理后台接口盘点

盘点日期：2026-08-06

## 结论

当前管理后台共有 **101 个页面目录**，其中 `love-customer-config/page(1).tsx` 是重复文件，不会生成额外路由。

按当前页面的列表、详情、创建、编辑、状态动作、统计和配置交互估算：

| 统计口径 | 数量 | 说明 |
| --- | ---: | --- |
| 页面 | 101 | `src/app/(admin)` 下的页面目录 |
| 接口族 | 约 133 | 例如 `members` 是一个接口族，包含列表、详情和写操作 |
| HTTP 接口操作 | 约 195 | 将 `GET/POST/PATCH/DELETE` 和详情、动作分别计数 |
| 当前页面已调用真实后台 | 5 | 登录、me、退出、审核队列、审核处理 |

195 是按页面现有 UI 的完整可用目标估算，不是后端已经实现的数量。具体字段、动作和是否合并接口，需要后端确认契约。

## 一、当前已经存在或已接入

### 认证

```text
POST /api/v1/admin/auth/login
GET  /api/v1/admin/auth/me
POST /api/v1/admin/auth/logout
POST /api/v1/admin/auth/refresh       # 当前前端尚未调用，但生产登录应提供
```

管理员账号不开放注册，账号由后端手动写入数据库。所有管理接口都必须在后端验证管理员角色和权限。

### 已有管理端文档/封装

```text
GET   /api/v1/admin/community/moderation-items
PATCH /api/v1/admin/community/moderation-items/{task_id}/review
PATCH /api/v1/admin/community/posts/{post_id}/moderation
PATCH /api/v1/admin/community/comments/{comment_id}/moderation
PATCH /api/v1/admin/community/paper-planes/{plane_id}/moderation
GET   /api/v1/admin/reports
GET   /api/v1/admin/reports/{report_id}
PATCH /api/v1/admin/reports/{report_id}/review
GET   /api/v1/admin/report-appeals
PATCH /api/v1/admin/report-appeals/{appeal_id}/review
PATCH /api/v1/admin/media/{media_id}/review
PATCH /api/v1/admin/users/{user_id}/certifications/{kind}/review
PATCH /api/v1/admin/matchmaker/applications/{application_id}
GET   /api/v1/admin/matchmaker/service-requests
PATCH /api/v1/admin/matchmaker/service-requests/{service_id}
POST  /api/v1/admin/matchmaker/meetings/requests/{request_id}/schedule
POST  /api/v1/admin/users/grant
GET/POST/PATCH /api/v1/admin/finance/*
```

当前真正有页面调用的是内容审核队列和审核处理；`admin-endpoints.ts` 中其余封装是调用入口，页面尚未全部迁移出 mock。

## 二、全页面接口需求

下面的路径是按页面和业务动作整理的接口族。`{id}` 表示资源 ID，列表统一建议支持 `page`、`page_size`、`keyword`、`status` 和业务筛选字段。

### 1. 概览和账号：5 个接口族

页面：`reg-user-all`、`reg-user-log`，以及当前首页统计组件。

```text
GET   /api/v1/admin/dashboard/stats
GET   /api/v1/admin/users
GET   /api/v1/admin/users/{id}
PATCH /api/v1/admin/users/{id}/status
GET   /api/v1/admin/users/login-logs
```

### 2. 客源线索：8 个接口族

页面：`love-customer-list`、`love-customer-statistics`、`customer-follow-up`、`customer-landing`、`love-customer-config`。

```text
GET   /api/v1/admin/customer-leads
GET   /api/v1/admin/customer-leads/{id}
POST  /api/v1/admin/customer-leads
PATCH /api/v1/admin/customer-leads/{id}
GET   /api/v1/admin/customer-leads/{id}/follow-ups
POST  /api/v1/admin/customer-leads/{id}/follow-ups
PATCH /api/v1/admin/customer-leads/{id}/assignment
GET/PATCH /api/v1/admin/customer-leads/config
GET   /api/v1/admin/customer-leads/statistics
```

### 3. 会员 CRM：10 个接口族

页面：`love-user-list`、`love-user-auth`、`love-user-statistics`、`love-user-vip`、`love-user-vip-underline`、`love-user-behavior`、`love-user-follow-up`。

```text
GET   /api/v1/admin/members
GET   /api/v1/admin/members/{id}
POST  /api/v1/admin/members
PATCH /api/v1/admin/members/{id}
PATCH /api/v1/admin/members/{id}/status
GET   /api/v1/admin/members/{id}/behavior
GET   /api/v1/admin/members/{id}/follow-ups
POST  /api/v1/admin/members/{id}/follow-ups
GET   /api/v1/admin/members/vip
PATCH /api/v1/admin/members/{id}/vip
GET   /api/v1/admin/members/statistics
GET/PATCH /api/v1/admin/members/{id}/certifications/{kind}
```

认证审核也可以复用现有的：

```text
PATCH /api/v1/admin/users/{user_id}/certifications/{kind}/review
```

### 4. 红娘、门店和分配：12 个接口族

页面：`love-matchmaker-list`、`love-matchmaker-apportion`、`love-matchmaker-distribution`、`love-matchmaker-distribution-details`、`branch-matchmaker-list`、`branch-distribution-list`、`branch-report-list`、`mendian-list`、`branch-config`、`poplove-matchmaker-list`、`poplove-matchmaker-distribution`、`poplove-matchmaker-distribution-details`。

```text
GET   /api/v1/admin/matchmakers
GET   /api/v1/admin/matchmakers/{id}
POST  /api/v1/admin/matchmakers
PATCH /api/v1/admin/matchmakers/{id}
PATCH /api/v1/admin/matchmakers/{id}/status
GET   /api/v1/admin/matchmakers/{id}/statistics
GET   /api/v1/admin/branches
GET   /api/v1/admin/branches/{id}
POST  /api/v1/admin/branches
PATCH /api/v1/admin/branches/{id}
PATCH /api/v1/admin/branches/{id}/status
GET   /api/v1/admin/branches/{id}/report
GET/PATCH /api/v1/admin/matchmaker/assignment-config
GET   /api/v1/admin/matchmaker/assignments
POST  /api/v1/admin/matchmaker/assignments
PATCH /api/v1/admin/matchmaker/assignments/{id}
GET/PATCH /api/v1/admin/matchmaker/commission-config
GET   /api/v1/admin/matchmaker/distributions/{id}
GET   /api/v1/admin/matchmaker/distributions/{id}/details
```

已有红娘服务申请接口还要与这些管理列表统一分页和状态枚举。

### 5. 活动、报名和互选：10 个接口族

页面：`active-list`、`active-config`、`active-alliance`、`active-signupmanager`、`mutual-selection-list`、`mutual-selection-record`。

```text
GET   /api/v1/admin/activities
GET   /api/v1/admin/activities/{id}
POST  /api/v1/admin/activities
PATCH /api/v1/admin/activities/{id}
PATCH /api/v1/admin/activities/{id}/status
GET   /api/v1/admin/activities/{id}/signups
GET   /api/v1/admin/activity-signups/{id}
PATCH /api/v1/admin/activity-signups/{id}
GET/PATCH /api/v1/admin/activities/config
GET   /api/v1/admin/mutual-selections
GET   /api/v1/admin/mutual-selections/{id}/records
POST  /api/v1/admin/mutual-selections
```

### 6. 商家、商品和订单：9 个接口族

页面：`merchant-management`、`merchant-product`、`merchant-order`、`merchant-alliance-config`。

```text
GET   /api/v1/admin/merchants
GET   /api/v1/admin/merchants/{id}
POST  /api/v1/admin/merchants
PATCH /api/v1/admin/merchants/{id}
GET   /api/v1/admin/merchant-products
GET   /api/v1/admin/merchant-products/{id}
POST  /api/v1/admin/merchant-products
PATCH /api/v1/admin/merchant-products/{id}
GET   /api/v1/admin/merchant-orders
GET   /api/v1/admin/merchant-orders/{id}
PATCH /api/v1/admin/merchant-orders/{id}
POST  /api/v1/admin/merchant-orders/{id}/refund
GET/PATCH /api/v1/admin/merchants/alliance-config
```

### 7. 短视频：8 个接口族

页面：`short-video-list`、`short-video-comment`、`short-video-config`、`short-video-homepage`、`short-video-red-packet`、`short-video-tip`。

```text
GET   /api/v1/admin/videos
GET   /api/v1/admin/videos/{id}
POST  /api/v1/admin/videos
PATCH /api/v1/admin/videos/{id}
PATCH /api/v1/admin/videos/{id}/review
GET   /api/v1/admin/video-comments
PATCH /api/v1/admin/video-comments/{id}
GET   /api/v1/admin/video-tips
PATCH /api/v1/admin/video-tips/{id}
GET/PATCH /api/v1/admin/videos/config
```

### 8. 财务和电子合同：18 个接口族

页面：`finance-config`、`finance-statistic`、`system-finance-order`、`system-cashout-history`、`system-credit-history`、`free-pay`、`free-form`、`e-contract-config`、`e-contract-list`、`e-contract-template`、`e-contract-yinzhang`。

```text
GET   /api/v1/admin/finance/orders
GET   /api/v1/admin/finance/orders/{id}
POST  /api/v1/admin/finance/orders/{id}/settle
POST  /api/v1/admin/finance/orders/{id}/refund
GET   /api/v1/admin/finance/report
GET/PATCH /api/v1/admin/finance/config
GET   /api/v1/admin/finance/withdrawals
PATCH /api/v1/admin/finance/withdrawals/{id}
GET   /api/v1/admin/finance/credit-history
GET/POST/PATCH /api/v1/admin/finance/free-pay
GET/POST/PATCH /api/v1/admin/finance/free-form
GET   /api/v1/admin/finance/commission-rules
POST  /api/v1/admin/finance/commission-rules
POST  /api/v1/admin/finance/product-commission-rules/{product_id}
GET/PATCH /api/v1/admin/e-contract/config
GET   /api/v1/admin/e-contract/contracts
GET   /api/v1/admin/e-contract/contracts/{id}
GET   /api/v1/admin/e-contract/contracts/{id}/download
GET/POST/PATCH/DELETE /api/v1/admin/e-contract/templates
GET/POST/PATCH/DELETE /api/v1/admin/e-contract/seals
POST  /api/v1/admin/e-contract/contracts/{id}/void
```

财务写操作必须额外确定幂等键、金额精度、退款状态机和审计字段。

### 9. 公众号：9 个接口族

页面：`wechat-config`、`wechat-fans`、`wechat-menu`、`wechat-autoreply`、`wechat-template`、`wechat-send`。

```text
GET/PATCH /api/v1/admin/wechat/config
GET   /api/v1/admin/wechat/fans
GET   /api/v1/admin/wechat/fans/{id}
GET/PATCH /api/v1/admin/wechat/menu
GET/POST/PATCH /api/v1/admin/wechat/auto-replies
GET/POST/PATCH /api/v1/admin/wechat/templates
GET   /api/v1/admin/wechat/send-records
POST  /api/v1/admin/wechat/send
```

### 10. 短信：8 个接口族

页面：`sms-group`、`sms-notices`、`sms-record`、`sms-signature`。

```text
GET/POST/PATCH /api/v1/admin/sms/groups
GET/POST/PATCH /api/v1/admin/sms/notices
GET   /api/v1/admin/sms/records
GET/POST/PATCH /api/v1/admin/sms/signatures
GET/PATCH /api/v1/admin/sms/config
POST  /api/v1/admin/sms/test-send
```

### 11. 系统、平台和管理员权限：16 个接口族

页面：`system-setting-basic`、`system-setting-admin-user`、`system-setting-admin-group`、`system-setting-admin-log`、`system-setting-adconfig`、`system-empower`、`power-config`、`platform-base`、`platform-config-basic`、`platform-content`、`platform-navconfig`、`platform-page`、`platform-payconfig`、`plugin-center`、`miniprogram-config`。

```text
GET/PATCH /api/v1/admin/system/basic
GET/PATCH /api/v1/admin/system/ad-config
GET   /api/v1/admin/system/admin-users
GET   /api/v1/admin/system/admin-users/{id}
POST  /api/v1/admin/system/admin-users
PATCH /api/v1/admin/system/admin-users/{id}
DELETE /api/v1/admin/system/admin-users/{id}
GET/POST/PATCH /api/v1/admin/system/admin-groups
GET/PATCH /api/v1/admin/system/permissions
GET   /api/v1/admin/system/admin-logs
GET/PATCH /api/v1/admin/system/empower
GET/PATCH /api/v1/admin/platform/base
GET/PATCH /api/v1/admin/platform/config
GET/PATCH /api/v1/admin/platform/content
GET/PATCH /api/v1/admin/platform/navigation
GET/POST/PATCH/DELETE /api/v1/admin/platform/pages
GET/PATCH /api/v1/admin/platform/pay-config
GET/PATCH /api/v1/admin/platform/miniprogram-config
GET   /api/v1/admin/platform/plugins
PATCH /api/v1/admin/platform/plugins/{id}
```

管理员新增接口只能由更高权限管理员或部署脚本使用，不能开放公共注册。

### 12. 运营工具、会员权益和其他页面：14 个接口族

页面：`single-page`、`generate-tool`、`gift-list`、`gift-exchange`、`love-gift-wrap`、`love-partner-list`、`love-partner-config`、`love-partner-relation`、`love-partner-bonus-config`、`love-partner-bonus-details`、`vip-line-record`、`vip-popularize-record`、`tool-lovecard`、`tool-theme`、`operate-center`、`out-call-list`、`out-call-record`、`outbound-call-platform`、`customer-landing`。

```text
GET/POST/PATCH /api/v1/admin/content/pages
GET   /api/v1/admin/content/pages/{id}
PATCH /api/v1/admin/content/pages/{id}/publish
POST  /api/v1/admin/content/generate
GET/POST/PATCH /api/v1/admin/gifts
GET/POST /api/v1/admin/gift-exchanges
GET/POST/PATCH /api/v1/admin/partners
GET/PATCH /api/v1/admin/partners/config
GET   /api/v1/admin/partners/{id}/relations
GET/PATCH /api/v1/admin/partners/bonus-config
GET   /api/v1/admin/partners/bonus-details
GET   /api/v1/admin/vip/line-records
GET   /api/v1/admin/vip/popularize-records
GET/PATCH /api/v1/admin/tools/love-card
GET/PATCH /api/v1/admin/tools/theme
GET   /api/v1/admin/outbound-calls
GET   /api/v1/admin/outbound-calls/{id}
GET   /api/v1/admin/outbound-calls/{id}/recording
GET/PATCH /api/v1/admin/outbound-call-platform
```

## 三、按页面覆盖情况

| 模块 | 页面数 | 当前状态 | 主要接口范围 |
| --- | ---: | --- | --- |
| 账号/概览 | 2 | mock | dashboard、users、login-logs |
| 客源线索 | 5 | mock | customer-leads、follow-ups、statistics、config |
| 会员 CRM | 7 | mock/审核部分真实 | members、VIP、behavior、follow-ups、certifications |
| 红娘/门店 | 12 | mock/部分已有文档 | matchmakers、branches、assignments、commission |
| 活动/互选 | 6 | mock | activities、signups、mutual-selections |
| 商家 | 4 | mock | merchants、products、orders、alliance |
| 短视频 | 6 | mock/审核部分真实 | videos、comments、tips、review |
| 财务/合同 | 11 | mock/部分已有文档 | finance、withdrawals、contracts、templates、seals |
| 公众号 | 6 | mock | wechat config、fans、menu、template、send |
| 短信 | 4 | mock | groups、notices、records、signatures |
| 系统/平台 | 15 | mock | admins、groups、logs、platform config |
| 运营/其他 | 23 | mock | content、gifts、partners、VIP、outbound calls |

页面数合计为 101。部分页面包含多个 Tab 或详情区域，因此接口数量会高于页面数量。

## 四、还需要后端确认的接口问题

以下问题不确认，接口即使写出来也容易返工：

1. 所有列表是否统一返回 `items/page/page_size/total/has_more`。
2. 筛选字段和状态枚举是否统一，例如 `status=0/1/2` 还是字符串状态。
3. 新增、编辑、审核、退款、提现、发布是否统一要求 `Idempotency-Key`。
4. 批量审核、批量分配、批量导出是否需要独立接口，以及最大批量数量。
5. 文件上传、合同下载、录音和短信发送是否使用异步任务，如何查询任务状态。
6. 财务金额单位是元还是分，退款和结算是否允许重复提交。
7. 管理员角色、权限粒度、数据范围权限和超级管理员规则。
8. 所有敏感字段的脱敏规则，例如手机号、身份证、微信号、合同和录音地址。
9. 删除是软删除还是物理删除，列表是否默认过滤已删除数据。
10. 审计日志保存周期、查询字段和是否记录变更前后 JSON。

## 五、建议的后端实现顺序

1. 管理员认证、角色权限、审计日志、统一分页和错误格式。
2. 账号、会员、客源线索、红娘/门店，这是后台最基础的 CRM 数据。
3. 活动、商家、订单、短视频审核。
4. 财务、提现、退款和电子合同，先确定状态机再开发。
5. 公众号、短信、系统配置和平台配置。
6. 运营工具、礼物、推广、外呼和批量导出。

详细的通用契约见 `docs/admin-contract.md`，原始缺失项见 `docs/missing-interfaces.md`。

## 六、接口用途说明

下面按“一个接口族解决什么问题”说明用途。接口路径中的详情、创建、编辑和动作接口，均围绕同一个业务对象工作。

### 认证和概览

| 接口 | 用途 |
| --- | --- |
| `admin/auth/login` | 管理员使用账号密码登录。后端校验密码、账号状态和管理员角色，成功返回访问 token；不允许注册。 |
| `admin/auth/me` | 前端打开后台后确认 token 是否有效，并获取当前管理员姓名、角色、权限和数据范围。 |
| `admin/auth/logout` | 注销当前管理员会话，使当前 token 失效。 |
| `admin/auth/refresh` | access token 过期时，用 refresh token 换取新 token，避免管理员频繁重新登录。 |
| `admin/dashboard/stats` | 首页统计卡片、趋势图和待处理数量，例如会员数、VIP 数、收入、待审核和待提现数量。 |
| `admin/users` | 账号管理页面的分页列表，支持昵称、手机号、注册时间、状态、会员身份等筛选。 |
| `admin/users/{id}` | 查看某个用户的完整后台资料，包括账号信息、认证状态、会员状态和最近行为。 |
| `admin/users/{id}/status` | 禁用或恢复普通用户账号，并记录操作原因和管理员审计日志。 |
| `admin/users/login-logs` | 展示用户登录时间、IP、设备、登录结果和失败原因，用于账号风险排查。 |

### 客源线索和会员 CRM

| 接口 | 用途 |
| --- | --- |
| `admin/customer-leads` | 客源线索列表，展示线索来源、联系人、负责人、当前状态和最近跟进时间。POST 用于录入线索，PATCH 用于修改线索资料或状态。 |
| `admin/customer-leads/{id}` | 查看单条线索的详情、来源信息、负责人和转化结果。 |
| `admin/customer-leads/{id}/follow-ups` | 查询和新增线索跟进记录，例如电话、微信、到店、意向等级和下次跟进时间。 |
| `admin/customer-leads/{id}/assignment` | 把线索分配或转交给红娘、销售或门店，并保留分配历史。 |
| `admin/customer-leads/config` | 配置线索来源、状态、意向等级和自动分配规则。 |
| `admin/customer-leads/statistics` | 提供线索数量、来源转化率、跟进量和成交率等报表数据。 |
| `admin/members` | 会员 CRM 列表，支持性别、城市、VIP、认证、资料完整度、红娘归属等条件筛选。 |
| `admin/members/{id}` | 查看会员完整资料、联系方式权限、认证记录、VIP 信息和服务记录。 |
| `admin/members/{id}/status` | 修改会员可见、冻结、注销等管理状态，并触发对应的业务限制。 |
| `admin/members/{id}/behavior` | 查看会员登录、浏览、喜欢、报名、聊天和消费等行为流水。 |
| `admin/members/{id}/follow-ups` | 维护会员 CRM 跟进记录，供红娘或运营人员记录服务过程。 |
| `admin/members/vip` | VIP 会员列表和到期筛选，展示会员等级、购买来源、生效时间和到期时间。 |
| `admin/members/{id}/vip` | 开通、续期、变更或取消会员 VIP；涉及金额时必须写入财务流水。 |
| `admin/members/statistics` | 会员总量、男女比例、认证数量、VIP 数量、活跃度和留存数据。 |
| `admin/users/{id}/certifications/{kind}/review` | 审核学历、房产、婚姻等认证材料，写入通过、拒绝、原因和审核管理员。 |

### 红娘、门店和分配

| 接口 | 用途 |
| --- | --- |
| `admin/matchmakers` | 红娘列表，展示所属门店、服务状态、服务会员数、业绩和可分配额度。 |
| `admin/matchmakers/{id}` | 查看红娘资料、服务范围、业绩、分成和历史分配。 |
| `admin/matchmakers/{id}/status` | 启用、停用或暂停红娘接单，停用时应校验已有服务中的会员。 |
| `admin/matchmakers/{id}/statistics` | 红娘的线索转化、服务订单、成交额、成功脱单和分成统计。 |
| `admin/branches` | 门店列表，展示门店负责人、地区、红娘数量、会员数量和运营状态。 |
| `admin/branches/{id}` | 查看门店详细资料、人员、业绩和配置。 |
| `admin/branches/{id}/report` | 生成门店维度的会员、线索、订单、收入和分成报表。 |
| `admin/matchmaker/assignments` | 查看和维护会员、线索、服务订单与红娘/门店的分配关系。 |
| `admin/matchmaker/assignment-config` | 配置自动分配规则，例如按地区、性别、负载、门店和红娘等级分配。 |
| `admin/matchmaker/commission-config` | 配置红娘、门店和推广方的分成比例、适用产品和生效时间。 |
| `admin/matchmaker/distributions/{id}` | 查看某次分配的明细、分配来源、目标红娘和处理结果。 |
| `admin/matchmaker/applications/{id}` | 审核红娘入驻申请，支持通过、驳回、暂停，并通知申请人。 |
| `admin/matchmaker/service-requests` | 管理会员购买的红娘服务订单，支持查询、分配、改派和处理状态变更。 |
| `admin/matchmaker/meetings/requests/{id}/schedule` | 为相亲或红娘服务请求安排时间、地点和参与人，避免重复预约。 |

### 活动、互选、商家和订单

| 接口 | 用途 |
| --- | --- |
| `admin/activities` | 活动列表和活动编辑，管理标题、时间、地点、费用、人数、报名条件和上下线状态。 |
| `admin/activities/{id}/signups` | 查看活动报名用户、支付状态、审核状态和签到信息。 |
| `admin/activity-signups/{id}` | 审核、取消或确认单条报名，必要时触发退款或通知。 |
| `admin/activities/config` | 配置报名规则、互选规则、报名审核和活动默认参数。 |
| `admin/mutual-selections` | 管理活动中的互选/匹配结果，查看双方选择和匹配状态。 |
| `admin/mutual-selections/{id}/records` | 查看一条互选关系的完整操作记录和通知记录。 |
| `admin/merchants` | 商家入驻、审核、门店资料和合作状态管理。 |
| `admin/merchant-products` | 商品、服务套餐、价格、库存、上下架和分成规则管理。 |
| `admin/merchant-orders` | 订单分页、支付状态、履约状态、退款状态和买家/商家信息查询。 |
| `admin/merchant-orders/{id}/refund` | 发起或审核退款，校验订单状态、退款金额和重复退款。 |
| `admin/merchants/alliance-config` | 配置商家联盟、推广分成、结算周期和合作规则。 |

### 短视频和内容审核

| 接口 | 用途 |
| --- | --- |
| `admin/videos` | 短视频后台列表、详情、发布、编辑和上下架管理。 |
| `admin/videos/{id}/review` | 审核视频内容，支持通过、拒绝、隐藏和填写审核原因。 |
| `admin/video-comments` | 查看视频评论并进行隐藏、恢复或删除等内容处理。 |
| `admin/video-tips` | 查看视频打赏、红包或礼物记录，处理异常记录和统计金额。 |
| `admin/videos/config` | 配置视频审核规则、推荐位、首页展示和红包/打赏开关。 |
| `admin/community/moderation-items` | 内容审核队列，返回待审核内容、风险等级、命中词、原文和展示文本。 |
| `admin/community/.../moderation` | 对帖子、评论和纸飞机执行下架、恢复或隐藏，不直接删除用户原始数据。 |
| `admin/reports` | 举报列表和举报详情，展示举报人、被举报目标、原因、状态和处理结果。 |
| `admin/reports/{id}/review` | 处理举报，可驳回、下架目标内容或恢复内容，并通知相关用户。 |
| `admin/report-appeals` | 查看被举报方的申诉记录。 |
| `admin/media/{id}/review` | 审核头像、相册、背景和个人视频等用户媒体资料。 |

### 财务和电子合同

| 接口 | 用途 |
| --- | --- |
| `admin/finance/orders` | 收入订单、支付渠道、金额、商品、付款人和订单状态查询。 |
| `admin/finance/orders/{id}/settle` | 对已完成订单执行结算，生成商家、红娘或平台分成记录。 |
| `admin/finance/orders/{id}/refund` | 处理退款，检查可退金额和订单终态，保证重复请求幂等。 |
| `admin/finance/report` | 财务统计报表，提供收入、退款、分成、净收入和时间趋势。 |
| `admin/finance/withdrawals` | 提现申请列表，展示申请人、金额、账户、审核状态和风险信息。 |
| `admin/finance/withdrawals/{id}` | 审核通过、驳回或打款提现申请，并记录财务审计。 |
| `admin/finance/commission-rules` | 配置平台、门店、红娘和推广方分成规则。 |
| `admin/finance/commission-entries/{id}/release` | 释放已满足条件的分成，使其进入可提现余额。 |
| `admin/finance/config` | 财务基础配置，例如结算周期、最低提现金额和支付渠道。 |
| `admin/e-contract/contracts` | 电子合同列表、详情、签署状态、下载和作废管理。 |
| `admin/e-contract/templates` | 合同模板的新增、编辑、启用、停用和删除。 |
| `admin/e-contract/seals` | 电子印章的上传、配置、启用、停用和使用范围管理。 |

### 公众号和短信

| 接口 | 用途 |
| --- | --- |
| `admin/wechat/config` | 配置公众号 AppID、密钥、Token、消息加解密和回调地址；敏感值必须脱敏返回。 |
| `admin/wechat/fans` | 查询公众号粉丝、标签、关注状态和最近互动。 |
| `admin/wechat/menu` | 查询和发布公众号自定义菜单。 |
| `admin/wechat/auto-replies` | 配置关键词回复、关注回复和默认回复。 |
| `admin/wechat/templates` | 管理微信模板消息内容、变量和启用状态。 |
| `admin/wechat/send` | 向符合条件的粉丝发送模板或通知消息，返回发送任务结果。 |
| `admin/wechat/send-records` | 查询微信消息发送记录、成功数、失败原因和重试状态。 |
| `admin/sms/groups` | 管理短信群组和发送目标。 |
| `admin/sms/notices` | 管理短信通知模板、变量和发送策略。 |
| `admin/sms/records` | 查询短信发送记录、计费条数、回执和失败原因。 |
| `admin/sms/signatures` | 管理短信签名的申请、审核和启用状态。 |
| `admin/sms/test-send` | 给指定测试号码发送测试短信，必须限制权限和频率。 |

### 系统、平台和运营工具

| 接口 | 用途 |
| --- | --- |
| `admin/system/admin-users` | 管理员列表、详情、启停和权限绑定；不提供公开注册。 |
| `admin/system/admin-groups` | 管理角色/权限组及其菜单和数据范围。 |
| `admin/system/admin-logs` | 查询管理员登录、配置修改、审核、财务操作和导出日志。 |
| `admin/system/basic` | 修改平台名称、联系方式、默认规则等基础系统配置。 |
| `admin/system/ad-config` | 管理广告位、投放状态、素材和展示时间。 |
| `admin/platform/navigation` | 配置前台或小程序导航菜单、排序和可见性。 |
| `admin/platform/pages` | 管理内容单页、草稿、发布、下线和版本。 |
| `admin/platform/pay-config` | 配置支付渠道、商户号、回调和支付开关，敏感字段不明文回显。 |
| `admin/platform/plugins` | 查看插件/应用中心，并执行启用、停用或配置操作。 |
| `admin/gifts` | 管理礼物、积分兑换物、价格、库存、上下架和展示素材。 |
| `admin/gift-exchanges` | 查询积分兑换记录并处理异常、取消或补发。 |
| `admin/partners` | 管理推广合作方、关系、状态和推广数据。 |
| `admin/partners/bonus-config` | 配置合作方奖励和分成规则。 |
| `admin/vip/line-records` | 查询 VIP 线下服务记录和跟进结果。 |
| `admin/vip/popularize-records` | 查询 VIP 推广记录、来源和奖励。 |
| `admin/outbound-calls` | 查询外呼任务和通话记录，详情接口返回录音的受控下载地址。 |
| `admin/outbound-call-platform` | 配置外呼平台、线路、坐席和启停状态。 |

## 七、当前页面与接口接入差距

目前页面多数只展示 mock 数据，主要缺口是：

- 列表页没有统一把筛选条件、分页和排序传给后端。
- 详情按钮多数还没有打开详情或调用详情接口。
- 新增、编辑、删除按钮多数只有视觉交互，没有真实写操作。
- 财务、合同、微信、短信和平台配置还没有真实保存、发布和回滚接口。
- 导出、文件上传、录音下载和异步任务状态尚未形成统一契约。

因此，后端可以按本文件先实现契约，前端再按模块逐页移除 mock；不要一次性把所有页面都指向一个泛化的 `admin/*` 接口，否则筛选字段、权限和审计会失控。
