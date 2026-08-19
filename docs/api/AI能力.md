# AI 能力（阶段一）

接口前缀：`/api/v1/ai`。所有接口需要登录且需要有效会员；未登录返回 `401`，非会员返回 `403`。阶段一只处理文字，不包含图片美化、向量数据库或海报图片生成。

## 配置

使用 OpenAI Chat Completions 兼容协议。测试环境可配置中转站 GPT，生产环境建议配置 DeepSeek：

```env
AI_ENABLED=true
AI_BASE_URL=https://your-relay.example.com/v1
AI_API_KEY=YOUR_TEST_RELAY_KEY
AI_MODEL=gpt-4o-mini
```

```env
AI_ENABLED=true
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=YOUR_DEEPSEEK_API_KEY
AI_MODEL=deepseek-chat
```

API Key 只能放在环境变量或配置中心，不能提交到 Git。`AI_ENABLED=false` 时开发环境使用确定性的 Mock 响应，生产环境应配置真实服务。

## 会员和额度

阶段一 AI 功能仅会员可用。默认每日额度：AI 助手 20 条、资料润色 5 次、AI 搜索 10 次、AI 匹配 10 次。额度使用 Redis 原子扣减；开发/测试 Redis 不可用时使用进程内回退，生产环境返回 `503`。额度键按 UTC 日期重置。

## `POST /api/v1/ai/assistant/sessions`

创建 AI 助手会话。请求体 `{"title":"聊天建议"}`，`title` 可选，最长 80 字符。返回会话 ID、标题、消息数和时间。会话只能由所属用户访问。

## `GET /api/v1/ai/assistant/sessions`

查询本人 AI 会话。Query：`page`（1~1000，默认 1）、`page_size`（1~50，默认 20）。返回 `items/page/page_size/total/has_more`。

## `POST /api/v1/ai/assistant/sessions/{session_id}/messages`

请求体：`{"content":"帮我看看这段聊天是否用心"}`，1~4000 字符。AI 会读取当前用户作为发送方或接收方的全部文本聊天记录，最多取最近 80 条，仅限本人会话上下文；撤回消息不读取。返回 AI 回复消息。AI 只提供沟通建议，不做医疗、法律或高风险判断。

## `POST /api/v1/ai/profile/polish`

只润色用户提交的文字，不自动写回资料，也不得补造职业、收入、学历等事实。

请求体：`content` 1~2000 字符；`style` 为 `natural/warm/humorous/mature/concise`；`max_length` 50~2000，默认 300。返回 `original/polished/style/changed_points`。用户确认后由原资料接口保存。

## `POST /api/v1/ai/search`

请求体：`query` 2~500 字符，`page` 1~1000，`page_size` 1~20。AI 将自然语言转换为现有发现筛选条件，再复用发现服务的隐私、拉黑、关系和推荐规则。AI 不直接生成用户列表，不确定的条件放入 `unresolved`。返回 `query/normalized_query/filters/unresolved/results`，`results` 与 `DiscoveryPage` 结构兼容。

## `GET /api/v1/ai/matches/{match_type}`

`match_type` 枚举：`who_likes_me`、`i_like`、`material`、`soul`。Query：`page` 1~1000，`page_size` 1~20。返回 `match_score`、`score_breakdown`、`match_reason` 和 `suggestions`。

- `who_likes_me`：基于对方偏好、资料和互动信号估算“对方可能喜欢我”。
- `i_like`：基于当前用户偏好、资料和兴趣估算“我会喜欢谁”。
- `material`：内部可使用年龄、城市、学历、身高、收入、婚姻等字段；响应只返回分项分数和概括原因，不返回对方精确敏感值，且必须遵守双方隐私设置。
- `soul`：基于 MBTI、兴趣、性格标签和活跃度，不使用收入、房产等物质字段。

现有推荐的固定规则负责计算分项基础分，AI 只生成解释和建议，不能自行改写分数或建立匹配关系。当前版本不会把 `who_likes_me` 解释为确定的喜欢事实。

错误：`401` 未登录；`403` 非会员；`429` 当日额度耗尽；`503` AI 或 Redis 服务不可用；`422` 参数校验失败。

### 变更记录

- 2026-08-19：新增阶段一 AI 助手、文字资料润色、自然语言搜索和四类匹配解释接口。
- 2026-08-19：图片美化、海报图片增强、向量检索和非会员收费暂不实现。
