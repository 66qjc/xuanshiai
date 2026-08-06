# 婚恋小程序 C端 API 完整清单

> **版本**：V1.0
> **生成依据**：一期 XMind + 二期(V2.0) XMind + 现有后台 API 参考
> **总 API**：181 个
> **一期**：145 个 | **二期**：36 个
> **模块数**：26 个
> **时间**：2026-07-17

---

## 📊 模块总览

| 模块 | 一期 | 二期 | 小计 |
|------|------|------|------|
| admin (管理端) | 24 | 7 | 31 |
| user (用户资料) | 24 | 1 | 25 |
| auth (认证登录) | 10 | 3 | 13 |
| post (社区动态) | 10 | 0 | 10 |
| event (活动) | 7 | 1 | 8 |
| privacy (隐私) | 7 | 1 | 8 |
| matchmaker (红娘) | 5 | 2 | 7 |
| action (交互操作) | 5 | 1 | 6 |
| chat (私信聊天) | 5 | 1 | 6 |
| center (个人中心) | 6 | 0 | 6 |
| settings (设置) | 6 | 0 | 6 |
| recommend (推荐) | 3 | 2 | 5 |
| psychology (情感实验室) | 0 | 5 | 5 |
| vip (会员) | 5 | 0 | 5 |
| credit (积分) | 5 | 0 | 5 |
| task (签到任务) | 5 | 0 | 5 |
| notification (消息通知) | 5 | 0 | 5 |
| internal (AI内部) | 0 | 5 | 5 |
| match (匹配) | 4 | 0 | 4 |
| paper-plane (纸飞机) | 0 | 4 | 4 |
| top (付费曝光) | 3 | 1 | 4 |
| help (客服) | 1 | 2 | 3 |
| order (订单) | 2 | 0 | 2 |
| square (广场) | 1 | 0 | 1 |
| feedback (反馈) | 1 | 0 | 1 |
| wxpay (微信支付) | 1 | 0 | 1 |
| **合计** | **145** | **36** | **181** |

---

## 一、认证与登录（13个接口）

### 1.1 登录认证

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 微信授权登录 | POST | `/api/auth/wx-login` | 微信一键授权登录，返回token | 一期 |
| 手机号绑定 | POST | `/api/auth/bind-phone` | 绑定或更换手机号 | 一期 |
| 获取当前用户 | GET | `/api/auth/me` | 获取登录用户完整信息 | 一期 |
| 退出登录 | POST | `/api/auth/logout` | 退出当前登录 | 一期 |
| 发送短信验证码 | POST | `/api/auth/sms-code` | 发送手机验证码 | 一期 |

**统一响应**：`{ code: 200, data: {...}, msg: "success" }`

### 1.2 实名与认证（8个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 提交实名认证 | POST | `/api/auth/real-name` | 提交姓名+身份证实名认证 | 一期 |
| 获取认证状态 | GET | `/api/auth/real-name/status` | 查询认证审核状态 | 一期 |
| 获取认证类型列表 | GET | `/api/auth/cert-types` | 所有认证类型(实名/学历/婚姻/房产) | 一期 |
| 提交学历认证 | POST | `/api/auth/education` | 学历证明认证 | 一期 |
| 提交婚姻状态 | POST | `/api/auth/marriage` | 婚姻状态承诺 | 一期 |
| 提交房产认证 | POST | `/api/auth/property` | 房产认证 | 二期 |
| 提交人脸认证 | POST | `/api/auth/face` | 人脸识别认证 | 二期 |
| 签署单身承诺书 | POST | `/api/auth/single-pledge` | 电子签名单身承诺书 | 二期 |
| 获取认证汇总 | GET | `/api/user/cert-summary` | 各认证状态一览 | 一期 |

---

## 二、用户资料（25个接口）

### 2.1 个人资料管理

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取个人资料 | GET | `/api/user/profile` | 获取当前用户完整资料 | 一期 |
| 更新个人资料 | POST | `/api/user/profile` | 更新基础信息(昵称/年龄/职业等) | 一期 |
| 上传头像 | POST | `/api/user/avatar` | 上传个人头像 | 一期 |
| 上传相册 | POST | `/api/user/album` | 上传相册(图片/视频) | 一期 |
| 删除相册 | DELETE | `/api/user/album/{id}` | 删除相册项 | 一期 |
| 获取择偶要求 | GET | `/api/user/requirements` | 获取择偶要求设置 | 一期 |
| 设置择偶要求 | POST | `/api/user/requirements` | 设置/更新择偶要求 | 一期 |
| 获取资料完整度 | GET | `/api/user/completion` | 资料完整度百分比 | 一期 |
| 获取用户公开主页 | GET | `/api/user/public/{userId}` | 指定用户的公开资料(他用户视角) | 一期 |
| 预览个人主页 | GET | `/api/user/preview` | 预览自己主页(完整) | 一期 |
| 设置交友状态 | POST | `/api/user/status` | 公开/委托/私密/暂停/已脱单 | 一期 |

### 2.2 我的数据

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取浏览记录 | GET | `/api/user/view-history` | 我浏览过的用户 | 一期 |
| 查看滑过用户 | GET | `/api/user/skipped` | 回看已滑过用户(可重新喜欢) | 一期 |
| 获取访客记录 | GET | `/api/user/visitors` | 谁看过我(会员解锁) | 一期 |
| 获取我的喜欢 | GET | `/api/user/my-likes` | 我喜欢的用户 | 一期 |
| 获取我的收藏 | GET | `/api/user/my-favorites` | 我的收藏(仅自己可见) | 一期 |
| 获取申请记录 | GET | `/api/user/my-applies` | 我发出的申请 | 一期 |
| 获取剩余次数 | GET | `/api/user/remaining-applies` | 今日剩余申请次数 | 一期 |
| 获取爆灯记录 | GET | `/api/user/my-spotlights` | 爆灯记录(我给/谁给我) | 二期 |
| 获取互动记录 | GET | `/api/user/interactions` | 点赞/关注/收藏记录 | 一期 |
| 获取我的动态 | GET | `/api/user/my-posts` | 我的发布记录 | 一期 |

### 2.3 举报

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 举报动态 | POST | `/api/post/report/{postId}` | 举报违规动态 | 一期 |
| 举报用户 | POST | `/api/user/report/{userId}` | 举报用户 | 一期 |

---

## 三、首页推荐（5个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取推荐卡片 | GET | `/api/recommend/list` | 首页推荐卡片流(LBS+AI) | 一期 |
| 获取广场卡片 | GET | `/api/square/list` | 广场海量浏览 | 一期 |
| 简单筛选 | GET | `/api/recommend/simple-filter` | 基础条件筛选(免费) | 一期 |
| 高级筛选 | POST | `/api/recommend/filter` | 多条件筛选(会员) | 一期 |
| 获取AI合拍度 | GET | `/api/recommend/match-score/{userId}` | AI合拍度评分(0-100) | 二期 |
| 获取推荐理由 | GET | `/api/recommend/reason/{userId}` | 推荐该用户的AI理由 | 二期 |

---

## 四、交互操作（6个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 喜欢/收藏 | POST | `/api/action/like` | 喜欢或收藏用户 | 一期 |
| 无感/跳过 | POST | `/api/action/pass` | 标记无感 | 一期 |
| 申请认识 | POST | `/api/action/apply` | 申请认识(消耗次数) | 一期 |
| 爆灯 | POST | `/api/action/spotlight` | 爆灯通知对方(5元/次) | 二期 |
| 取消喜欢 | DELETE | `/api/action/like/{userId}` | 取消对用户的喜欢 | 一期 |
| 记录浏览行为 | POST | `/api/action/view` | 浏览他人主页时上报 | 一期 |

---

## 五、匹配（4个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 处理申请 | POST | `/api/match/handle` | 同意/拒绝申请认识 | 一期 |
| 查询匹配状态 | GET | `/api/match/status/{userId}` | 与某用户的匹配状态 | 一期 |
| 双向喜欢列表 | GET | `/api/match/mutual-likes` | 互相喜欢的用户(匹配成功列表) | 一期 |
| 待处理申请 | GET | `/api/match/pending` | 收到的待处理申请列表 | 一期 |

---

## 六、私信聊天（6个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取会话列表 | GET | `/api/chat/sessions` | 聊天会话列表 | 一期 |
| 获取聊天记录 | GET | `/api/chat/messages` | 与某用户的聊天历史 | 一期 |
| 发送消息 | POST | `/api/chat/send` | 发送私信(文字/图片/语音) | 一期 |
| 标记已读 | POST | `/api/chat/read` | 标记消息为已读 | 一期 |
| 未读消息数 | GET | `/api/chat/unread-count` | 未读消息数汇总 | 一期 |
| 发送小纸条 | POST | `/api/chat/paper-note` | 向未匹配用户发消息(付费) | 二期 |

---

## 七、社区/动态（10个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 发布动态 | POST | `/api/post/create` | 发布图文/打卡动态 | 一期 |
| 获取动态流 | GET | `/api/post/feed` | 社区动态(关注/同城/发现Tab) | 一期 |
| 点赞动态 | POST | `/api/post/like/{postId}` | 点赞指定动态 | 一期 |
| 取消点赞 | DELETE | `/api/post/like/{postId}` | 取消点赞 | 一期 |
| 评论动态 | POST | `/api/post/comment/{postId}` | 发表评论 | 一期 |
| 获取评论 | GET | `/api/post/comments/{postId}` | 获取评论列表 | 一期 |
| 删除动态 | DELETE | `/api/post/{postId}` | 删除自己发布的动态 | 一期 |
| 获取话题列表 | GET | `/api/post/topics` | 热门话题列表 | 一期 |
| 参与话题 | GET | `/api/post/topic/{topicId}` | 话题下动态列表 | 一期 |
| 关注用户 | POST | `/api/user/follow/{userId}` | 关注用户 | 一期 |
| 取消关注 | DELETE | `/api/user/follow/{userId}` | 取消关注 | 一期 |

---

## 八、纸飞机（4个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 扔纸飞机 | POST | `/api/paper-plane/throw` | 扔出匿名纸飞机 | 二期 |
| 捡纸飞机 | GET | `/api/paper-plane/catch` | 随机捡一个(每日3次) | 二期 |
| 我的纸飞机 | GET | `/api/paper-plane/my` | 我扔出的纸飞机列表 | 二期 |
| 回复纸飞机 | POST | `/api/paper-plane/reply/{planeId}` | 回复纸飞机 | 二期 |

---

## 九、情感实验室（5个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取测试列表 | GET | `/api/psychology/tests` | 可用心理测试列表 | 二期 |
| 提交MBTI测试 | POST | `/api/psychology/mbti` | 提交MBTI测试答案 | 二期 |
| 获取MBTI结果 | GET | `/api/psychology/mbti/result` | 已测试的MBTI类型 | 二期 |
| AI合拍度分析 | POST | `/api/psychology/compatibility` | 基于MBTI+星座+兴趣计算合拍度 | 二期 |
| 生成测试海报 | GET | `/api/psychology/poster/{resultId}` | 测试结果分享海报 | 二期 |

---

## 十、1v1红娘（7个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 服务红娘列表 | GET | `/api/matchmaker/service-list` | 付费专业红娘列表 | 一期 |
| 热心红娘排行榜 | GET | `/api/matchmaker/ambassador-list` | 热心红娘排行榜 | 一期 |
| 联系红娘 | POST | `/api/matchmaker/contact` | 获取红娘联系方式 | 一期 |
| 申请私人定制 | POST | `/api/matchmaker/private-custom` | 申请定制服务 | 一期 |
| 我的定制订单 | GET | `/api/matchmaker/my-orders` | 我的定制服务订单状态 | 一期 |
| 申请成为红娘 | POST | `/api/matchmaker/apply` | 用户申请入驻成为红娘 | 二期 |
| AI红娘咨询 | POST | `/api/matchmaker/ai-chat` | AI红娘破冰提示/沟通引导 | 二期 |

---

## 十一、会员与增值（14个接口）

### 11.1 会员（5个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取会员套餐 | GET | `/api/vip/packages` | 所有会员套餐 | 一期 |
| 获取VIP状态 | GET | `/api/vip/status` | 当前VIP状态及到期时间 | 一期 |
| 开通/续费会员 | POST | `/api/vip/subscribe` | 开通或续费会员(微信支付) | 一期 |
| 获取会员权益 | GET | `/api/vip/benefits` | 各套餐权益详情 | 一期 |
| VIP权限校验 | GET | `/api/vip/check-access` | 前端校验VIP专属功能 | 一期 |

### 11.2 积分（5个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取积分余额 | GET | `/api/credit/balance` | 积分余额及每日重置信息 | 一期 |
| 获取积分明细 | GET | `/api/credit/history` | 积分收支记录 | 一期 |
| 积分兑换牵线 | POST | `/api/credit/exchange-apply` | 积分兑换申请次数 | 一期 |
| 积分兑换置顶 | POST | `/api/credit/exchange-top` | 积分兑换置顶曝光 | 一期 |
| 获取积分规则 | GET | `/api/credit/rules` | 积分获取/消耗规则说明 | 一期 |

### 11.3 付费曝光（4个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取置顶套餐 | GET | `/api/top/packages` | 置顶曝光套餐价格 | 一期 |
| 购买置顶 | POST | `/api/top/purchase` | 购买置顶曝光(微信支付) | 一期 |
| 我的置顶状态 | GET | `/api/top/my-status` | 当前置顶剩余时间 | 一期 |
| 获取曝光权重 | GET | `/api/top/boost` | 曝光权重提升状态 | 二期 |

---

## 十二、签到与任务（5个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 每日签到 | POST | `/api/task/sign-in` | 每日签到领积分 | 一期 |
| 签到状态 | GET | `/api/task/sign-in/status` | 今日签到状态及连续签到天数 | 一期 |
| 获取任务列表 | GET | `/api/task/list` | 所有任务(新手/日常/成长) | 一期 |
| 领取任务奖励 | POST | `/api/task/claim/{taskId}` | 领取任务奖励 | 一期 |
| 签到历史 | GET | `/api/task/sign-in/history` | 签到日历历史 | 一期 |

---

## 十三、线下活动（8个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取活动列表 | GET | `/api/event/list` | 活动列表(筛选中/已结束) | 一期 |
| 活动详情 | GET | `/api/event/{eventId}` | 活动详细信息(含报名状态) | 一期 |
| 报名活动 | POST | `/api/event/{eventId}/apply` | 报名参加活动(微信支付) | 一期 |
| 取消报名 | POST | `/api/event/{eventId}/cancel` | 取消已报名活动 | 一期 |
| 我的报名 | GET | `/api/event/my-activities` | 我的报名列表 | 一期 |
| 活动签到 | POST | `/api/event/{eventId}/check-in` | 活动现场签到核销 | 一期 |
| 获取活动地点 | GET | `/api/event/{eventId}/location` | 活动地点(报名后解锁) | 一期 |
| 活动状态回调 | POST | `/api/event/webhook/status` | 活动状态变更通知 | 二期 |

---

## 十四、消息通知（5个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取消息列表 | GET | `/api/notification/list` | 消息中心(申请/匹配/系统) | 一期 |
| 标记已读 | POST | `/api/notification/read` | 标记消息为已读 | 一期 |
| 未读消息数 | GET | `/api/notification/unread` | 各类型未读消息数 | 一期 |
| 系统公告 | GET | `/api/notification/announcements` | 系统公告列表 | 一期 |
| 订阅微信消息 | POST | `/api/notification/wx-subscribe` | 订阅微信通知模板 | 一期 |

---

## 十五、个人中心（6个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 个人中心首页 | GET | `/api/center/home` | 个人中心首页汇总数据 | 一期 |
| 生成相亲海报 | GET | `/api/center/poster` | 生成相亲海报(含二维码) | 一期 |
| 海报模板列表 | GET | `/api/center/poster/templates` | 可用海报模板列表 | 一期 |
| 邀请好友链接 | GET | `/api/center/invite-link` | 获取邀请链接/海报 | 一期 |
| 邀请记录 | GET | `/api/center/invite/records` | 邀请成功记录及奖励 | 一期 |
| 互动统计 | GET | `/api/center/stats` | 收藏/喜欢/访客数统计 | 一期 |

---

## 十六、隐私设置（8个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 获取隐私设置 | GET | `/api/privacy/settings` | 当前隐私配置 | 一期 |
| 更新隐私设置 | POST | `/api/privacy/settings` | 更新隐私配置 | 一期 |
| 开启无痕浏览 | POST | `/api/privacy/stealth/on` | 开启无痕浏览(VIP) | 一期 |
| 关闭无痕浏览 | POST | `/api/privacy/stealth/off` | 关闭无痕浏览 | 一期 |
| 屏蔽用户 | POST | `/api/privacy/block/{userId}` | 拉黑指定用户 | 一期 |
| 解除屏蔽 | DELETE | `/api/privacy/block/{userId}` | 解除对用户的屏蔽 | 一期 |
| 获取黑名单 | GET | `/api/privacy/block-list` | 黑名单列表 | 一期 |
| 屏蔽熟人 | POST | `/api/privacy/block-stranger` | 屏蔽通讯录熟人来源 | 二期 |

---

## 十七、设置（6个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 修改密码 | POST | `/api/settings/password` | 修改账号密码 | 一期 |
| 换绑手机 | POST | `/api/settings/phone` | 更换绑定手机号 | 一期 |
| 注销账号 | POST | `/api/settings/account/cancel` | 注销账号(需实名验证) | 一期 |
| 关于我们 | GET | `/api/settings/about` | 关于我们信息 | 一期 |
| 服务协议 | GET | `/api/settings/agreement` | 用户协议与隐私政策 | 一期 |
| 通知开关 | POST | `/api/settings/notify-switches` | 各类型通知开关 | 一期 |

---

## 十八、微信支付（3个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 支付回调 | POST | `/api/wxpay/callback` | 微信支付结果回调 | 一期 |
| 查询订单 | GET | `/api/order/{orderId}` | 订单状态 | 一期 |
| 我的订单 | GET | `/api/order/list` | 所有订单(会员/置顶/活动) | 一期 |

---

## 十九、客服与帮助（3个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| FAQ列表 | GET | `/api/help/faq` | 常见问题列表 | 一期 |
| 在线客服 | POST | `/api/help/chat` | 提交在线客服请求 | 二期 |
| 客服记录 | GET | `/api/help/chat/{ticketId}` | 客服工单状态和对话 | 二期 |

---

## 二十、意见反馈（1个接口）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 意见反馈 | POST | `/api/feedback` | 提交意见反馈 | 一期 |

---

## 二十一、AI/推荐内部接口（5个接口）

> 以下接口为内部调用，不对小程序前端开放，用于服务端 AI 计算

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 用户向量 | POST | `/api/internal/ai/user-embedding` | 更新用户向量用于相似度计算 | 二期 |
| AI推荐 | POST | `/api/internal/ai/recommend` | AI推荐算法调用 | 二期 |
| AI内容审核 | POST | `/api/internal/ai/moderate` | AI辅助内容审核 | 二期 |
| AI破冰 | POST | `/api/internal/ai/ice-breaker` | AI生成聊天破冰话题 | 二期 |

---

## 二十二、管理端 C端接口（31个接口）

### 22.1 用户管理（7个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 用户列表(管理) | GET | `/api/admin/user/list` | 查询用户列表 | 一期 |
| 审核认证(管理) | POST | `/api/admin/user/audit/{auditId}` | 审核用户认证/资料 | 一期 |
| 拉黑用户(管理) | POST | `/api/admin/user/ban/{userId}` | 管理员拉黑用户 | 一期 |
| 举报列表(管理) | GET | `/api/admin/report/list` | 举报记录列表 | 一期 |
| 处理举报(管理) | POST | `/api/admin/report/{reportId}/handle` | 处理举报 | 一期 |
| 认证审核列表(管理) | GET | `/api/admin/cert/list` | 待审核认证列表 | 一期 |
| 统计数据(管理) | GET | `/api/admin/stats/overview` | 后台首页统计数据 | 一期 |

### 22.2 内容审核（5个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 待审动态(管理) | GET | `/api/admin/post/pending` | 待审核动态列表 | 一期 |
| 审核动态(管理) | POST | `/api/admin/post/audit/{postId}` | 通过/拒绝动态 | 一期 |
| 待审评论(管理) | GET | `/api/admin/comment/pending` | 待审核评论列表 | 一期 |
| 审核评论(管理) | POST | `/api/admin/comment/audit/{commentId}` | 通过/拒绝评论 | 一期 |
| AI内容审核(内部) | POST | `/api/internal/content/moderate` | AI辅助审核 | 二期 |

### 22.3 活动管理（6个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 发布活动(管理) | POST | `/api/admin/event/create` | 后台发布新活动 | 一期 |
| 编辑活动(管理) | POST | `/api/admin/event/{eventId}/edit` | 编辑活动信息 | 一期 |
| 报名列表(管理) | GET | `/api/admin/event/{eventId}/applicants` | 活动报名人员列表 | 一期 |
| 审核报名(管理) | POST | `/api/admin/event/{eventId}/audit` | 审核活动报名 | 一期 |
| 导出报名(管理) | GET | `/api/admin/event/{eventId}/export` | 导出活动报名Excel | 一期 |
| 活动转化漏斗(管理) | GET | `/api/admin/event/{eventId}/stats` | 活动数据分析 | 二期 |

### 22.4 红娘管理（5个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 红娘申请列表(管理) | GET | `/api/admin/matchmaker/apply-list` | 红娘入驻申请列表 | 二期 |
| 审核红娘(管理) | POST | `/api/admin/matchmaker/{applyId}/audit` | 审核红娘申请 | 二期 |
| 红娘列表(管理) | GET | `/api/admin/matchmaker/list` | 红娘管理列表 | 一期 |
| 编辑红娘(管理) | POST | `/api/admin/matchmaker/{id}/edit` | 编辑红娘信息/上下架 | 一期 |
| 定制订单(管理) | GET | `/api/admin/matchmaker/order-list` | 定制服务订单 | 一期 |

### 22.5 内容管理（4个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 话题列表(管理) | GET | `/api/admin/topic/list` | 管理话题列表 | 一期 |
| 创建话题(管理) | POST | `/api/admin/topic/create` | 创建话题 | 一期 |
| Banner配置(管理) | GET | `/api/admin/ads/banner` | 获取Banner配置 | 一期 |
| 更新Banner(管理) | POST | `/api/admin/ads/banner` | 添加/编辑Banner | 一期 |

### 22.6 数据分析（4个）

| 接口名 | 方法 | 路径 | 功能说明 | 阶段 |
|--------|------|------|---------|------|
| 指标看板 | GET | `/api/admin/analytics/dashboard` | DAU/新增/留存/转化漏斗 | 二期 |
| 功能使用率 | GET | `/api/admin/analytics/feature-usage` | 各功能使用率统计 | 二期 |
| 用户留存 | GET | `/api/admin/analytics/retention` | 用户留存分析 | 二期 |
| 收入分析 | GET | `/api/admin/analytics/revenue` | 收入分析(按日/类型) | 二期 |

---

## 附录：API与一期/二期模块对照

| XMind模块 | C端接口数 | 主要路径 |
|---------|---------|---------|
| 用户与认证 | 25 | `/api/user/*`, `/api/auth/*` |
| 首页与推荐 | 6 | `/api/recommend/*`, `/api/square/*` |
| 社交互动 | 10+4+6 | `/api/action/*`, `/api/match/*`, `/api/post/*`, `/api/paper-plane/*` |
| 消息通知 | 5+6 | `/api/notification/*`, `/api/chat/*` |
| 个人中心 | 6 | `/api/center/*` |
| 会员与支付 | 14 | `/api/vip/*`, `/api/credit/*`, `/api/top/*` |
| 线下活动 | 8 | `/api/event/*` |
| 红娘与服务 | 7 | `/api/matchmaker/*` |
| 情感实验室 | 5 | `/api/psychology/*` |
| 任务与留存 | 5 | `/api/task/*` |
| 隐私设置 | 8 | `/api/privacy/*` |
| 技术支撑(AI) | 5 | `/api/internal/ai/*` |
| 管理与后台 | 31 | `/api/admin/*` |
