# 阿里云敏感词服务接入完整方案

## 1. 目标

当前项目已经具备本地敏感词过滤能力，词库来自 MySQL 表 `config_sensitive_word`。

本方案的目标是：

1. 保留本地词库作为第一层过滤。
2. 接入阿里云云市场敏感词过滤 API。
3. 使用阿里云作为第二层内容审核。
4. 将阿里云命中结果接入现有的拦截、替换和人工审核流程。
5. 保证阿里云故障时系统有明确的降级策略。
6. 不把 AppCode、用户原文和第三方完整响应泄露到日志中。

推荐架构：

```text
用户提交文本
    |
    v
本地 config_sensitive_word 快速匹配
    |
    |-- 本地高风险词：直接 reject
    |
    v
阿里云敏感词 API
    |
    |-- 正常：allow
    |-- 明确高风险：reject
    |-- 有风险但需要上下文：manual_review
    |-- 普通广告或引流：replace
    |-- API 故障：进入人工审核或按配置拒绝
    |
    v
保存内容和审核结果
```

## 2. 两个阿里云链接的区别

### 2.1 云市场敏感词过滤 API

地址：

<https://market.aliyun.com/detail/cmapi00065247#sku=yuncode5924700002>

这是当前社区文本审核应该使用的产品，适用于动态、评论、纸飞机、纸飞机回复、会话文本消息以及其他用户生成文本。

公开产品信息：

```text
产品编码：cmapi00065247
API 域名：lxmingan.market.alicloudapi.com
API 区域：cn-beijing
API 类型：云市场 API
```

购买后需要从云市场控制台取得：

- AppCode；
- 真实请求路径；
- HTTP 方法；
- 请求参数名称；
- 请求 Content-Type；
- 返回 JSON 结构；
- 错误码；
- 文本长度限制；
- 调用频率限制；
- 计费规则。

不能仅凭商品名称猜测请求路径和字段。

### 2.2 智能媒体服务自定义敏感词

地址：

<https://www.alibabacloud.com/help/zh/ims/user-guide/custom-sensitive-words>

该功能用于智能媒体服务、STT 语音识别节点和实时字幕脱敏。它不是当前社区文本审核 API，也不会自动同步到 `config_sensitive_word`。

官方限制：

- 使用 TXT 文件；
- 文件必须是 UTF-8；
- 每个文件最多 500 个词；
- 每个词最长 10 个字符；
- 不能包含标点和特殊字符；
- 上传后需要保存或创建工作流才生效。

当前项目如果只审核社区文本，不需要接入第二个功能。

## 3. 购买和开通云市场 API

打开：

<https://market.aliyun.com/detail/cmapi00065247#sku=yuncode5924700002>

操作步骤：

1. 登录阿里云。
2. 完成实名认证。
3. 进入云市场商品页面。
4. 选择套餐 `yuncode5924700002`。
5. 确认免费试用次数或正式套餐价格。
6. 完成购买或试用开通。
7. 进入阿里云云市场买家控制台。
8. 找到“敏感词过滤”服务。
9. 打开“API 调用”或“接口文档”。
10. 记录 AppCode、API 请求地址、API 请求路径、HTTP Method、请求参数、请求头、Content-Type、成功响应、命中响应和失败响应。

开通前要确认：免费额度、正式计费方式、每秒请求限制、单次文本长度限制、是否支持批量文本、超出额度后的行为、服务超时和失败处理、服务商是否保存用户文本、数据保存期限以及隐私和合规要求。

## 4. AppCode 管理

AppCode 只能放在后端环境变量或部署平台密钥中。

禁止放入前端代码、小程序代码、Git、`.env.example`、日志、API 响应和数据库普通业务字段。

生产环境建议通过部署平台 Secret、阿里云密钥管理服务、容器 Secret 或服务器环境变量注入 AppCode。

## 5. 项目环境变量配置

当前项目使用 `app/core/config.py`。在本地 `.env` 中增加：

```env
ALIYUN_CONTENT_MODERATION_ENABLED=false
ALIYUN_CONTENT_MODERATION_BASE_URL=https://lxmingan.market.alicloudapi.com
ALIYUN_CONTENT_MODERATION_PATH=/YOUR_API_PATH
ALIYUN_CONTENT_MODERATION_APP_CODE=YOUR_ALIYUN_MARKET_APPCODE
ALIYUN_CONTENT_MODERATION_TIMEOUT_SECONDS=2.5
ALIYUN_CONTENT_MODERATION_FAIL_MODE=review
ALIYUN_CONTENT_MODERATION_DEFAULT_ACTION=manual_review
```

在 `.env.example` 中只保留占位符，生产环境通过部署平台注入真实值：

```env
ENVIRONMENT=production
DEBUG=false
AUTO_INIT_DB=false
ALIYUN_CONTENT_MODERATION_ENABLED=true
```

## 6. 增加配置模型

修改 `app/core/config.py`，导入：

```python
from typing import Literal
from pydantic import Field, SecretStr
```

在 `Settings` 类中增加：

```python
aliyun_content_moderation_enabled: bool = False
aliyun_content_moderation_base_url: str = (
    "https://lxmingan.market.alicloudapi.com"
)
aliyun_content_moderation_path: str = "/YOUR_API_PATH"
aliyun_content_moderation_app_code: SecretStr | None = None
aliyun_content_moderation_timeout_seconds: float = Field(
    default=2.5, gt=0, le=10
)
aliyun_content_moderation_fail_mode: Literal["review", "reject"] = "review"
aliyun_content_moderation_default_action: Literal[
    "manual_review", "reject", "replace"
] = "manual_review"
```

增加生产环境校验：

```python
@model_validator(mode="after")
def validate_aliyun_moderation(self) -> "Settings":
    if (
        self.environment == "production"
        and self.aliyun_content_moderation_enabled
        and not self.aliyun_content_moderation_app_code
    ):
        raise ValueError(
            "生产环境启用阿里云敏感词服务时必须配置 AppCode"
        )
    return self
```

## 7. 安装 HTTP 客户端

当前项目部分代码已使用 `httpx`，但它目前属于开发依赖。生产环境也需要使用，应将它放入 `pyproject.toml` 的主依赖：

```toml
"httpx>=0.27,<1.0",
```

然后执行：

```powershell
uv sync
```

## 8. 先单独测试阿里云接口

必须使用云市场接口文档中的真实路径和请求字段。

如果文档要求 JSON：

```bash
curl --request POST \
  --url "https://lxmingan.market.alicloudapi.com/YOUR_API_PATH" \
  --header "Authorization: APPCODE YOUR_APPCODE" \
  --header "Content-Type: application/json" \
  --data '{"text":"测试文本"}'
```

如果文档要求表单请求：

```bash
curl --request POST \
  --url "https://lxmingan.market.alicloudapi.com/YOUR_API_PATH" \
  --header "Authorization: APPCODE YOUR_APPCODE" \
  --header "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "text=测试文本"
```

测试正常文本、命中文本、空文本、超长文本、错误 AppCode、缺少参数、频率超限和接口 5xx，并确认 HTTP 状态码、返回 JSON、命中字段、风险等级、请求 ID、错误结构和服务延迟。

## 9. 新增阿里云服务适配器

新建 `app/services/aliyun_content_moderation.py`：

```python
"""Alibaba Cloud Market sensitive-content moderation adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class ProviderDecision:
    action: str
    matched_words: tuple[str, ...] = ()
    risk_level: int = 0
    provider: str = "aliyun_market"


def _extract_matched_words(payload: Any) -> tuple[str, ...]:
    """根据云市场实际响应结构调整解析逻辑。"""
    if not isinstance(payload, dict):
        return ()

    data = payload.get("data")
    candidates = [
        payload.get("matched_words"),
        payload.get("words"),
        payload.get("sensitiveWords"),
        data.get("matched_words") if isinstance(data, dict) else None,
        data.get("words") if isinstance(data, dict) else None,
        data.get("sensitiveWords") if isinstance(data, dict) else None,
    ]

    for value in candidates:
        if isinstance(value, list):
            result = []
            for item in value:
                text = str(item).strip()
                if text:
                    result.append(text)
            return tuple(result)

    return ()


def _is_blocked(payload: Any) -> bool:
    """根据云市场实际响应结构调整解析逻辑。"""
    if not isinstance(payload, dict):
        return False

    data = payload.get("data")

    if isinstance(data, dict):
        for key in ("blocked", "hit", "isSensitive", "is_sensitive"):
            if data.get(key) is True:
                return True

    for key in ("blocked", "hit", "isSensitive", "is_sensitive"):
        if payload.get(key) is True:
            return True

    status = str(
        payload.get("status")
        or payload.get("result")
        or ""
    ).lower()

    return status in {"blocked", "sensitive", "illegal", "reject"}


async def moderate_with_aliyun(content: str) -> ProviderDecision:
    if not settings.aliyun_content_moderation_enabled:
        return ProviderDecision(action="allow")

    if not content.strip():
        return ProviderDecision(action="allow")

    if not settings.aliyun_content_moderation_app_code:
        raise RuntimeError("阿里云敏感词服务未配置 AppCode")

    url = (
        settings.aliyun_content_moderation_base_url.rstrip("/")
        + "/"
        + settings.aliyun_content_moderation_path.lstrip("/")
    )

    headers = {
        "Authorization": (
            "APPCODE "
            + settings.aliyun_content_moderation_app_code.get_secret_value()
        ),
        "Accept": "application/json",
    }

    # 字段名必须根据购买后的云市场接口文档修改。
    request_payload = {"text": content}

    try:
        async with httpx.AsyncClient(
            timeout=settings.aliyun_content_moderation_timeout_seconds
        ) as client:
            response = await client.post(
                url,
                headers=headers,
                json=request_payload,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        if settings.aliyun_content_moderation_fail_mode == "reject":
            return ProviderDecision(action="reject", risk_level=3)
        return ProviderDecision(action="manual_review", risk_level=2)

    if not _is_blocked(payload):
        return ProviderDecision(action="allow")

    return ProviderDecision(
        action=settings.aliyun_content_moderation_default_action,
        matched_words=_extract_matched_words(payload),
        risk_level=2,
    )
```

必须根据官方接口文档修改：

```text
aliyun_content_moderation_path
request_payload 的字段名
Content-Type
_is_blocked()
_extract_matched_words()
```

如果官方要求表单请求，应使用：

```python
response = await client.post(
    url,
    headers=headers,
    data={"text": content},
)
```

如果官方要求 JSON，应使用：

```python
response = await client.post(
    url,
    headers=headers,
    json={"text": content},
)
```

## 10. 统一审核入口

当前社区业务会调用 `assert_text_allowed()` 和 `decide_text()`。如果两个函数分别调用阿里云，同一条内容会被发送两次。

建议在 `app/services/content_filter.py` 中扩展：

```python
@dataclass(frozen=True)
class ContentDecision:
    action: str
    display_content: str
    matched_words: tuple[str, ...] = ()
    max_level: int = 0
    provider: str = "local"
```

增加统一入口：

```python
async def moderate_text(
    db: AsyncSession,
    content: str | None,
) -> ContentDecision:
    local_decision = await decide_text(db, content)

    if local_decision.action == "reject":
        return local_decision

    from app.services.aliyun_content_moderation import (
        moderate_with_aliyun,
    )

    provider_decision = await moderate_with_aliyun(content or "")

    if provider_decision.action == "allow":
        return local_decision

    return ContentDecision(
        action=provider_decision.action,
        display_content=content or "",
        matched_words=provider_decision.matched_words,
        max_level=provider_decision.risk_level,
        provider=provider_decision.provider,
    )
```

社区业务统一使用：

```python
decision = await moderate_text(db, content)

if decision.action == "reject":
    await _notify_moderation_rejected(db, user_id, "post")
    raise HTTPException(
        422,
        detail="内容含违规信息，请修改后重试",
    )
```

不要让同一入口同时执行：

```python
assert_text_allowed()
decide_text()
moderate_with_aliyun()
```

应统一使用：

```python
moderate_text()
```

## 11. 需要修改的社区入口

需要接入动态发布、动态修改、评论、纸飞机、纸飞机回复和纸飞机会话文本消息。

流程统一为：

```text
读取文本
    |
moderate_text()
    |
reject：返回 422
replace：保存替换内容
manual_review：保存但隐藏
allow：正常发布
```

动态修改时必须重新完整审核，不能复用旧审核结果。

纸飞机审核失败时，要退还已经消耗的每日额度。

会话消息只审核文本消息：

```python
if request.type == 1:
    decision = await moderate_text(db, request.content)
```

语音消息不经过文本敏感词检测。

## 12. 处理动作映射

建议第一版采用：

```text
本地 level=3 且 action=reject -> 直接拒绝
阿里云明确高风险          -> reject
阿里云命中但需要上下文      -> manual_review
广告、引流等低风险          -> replace
阿里云正常                 -> allow
阿里云超时或暂时不可用       -> manual_review
```

不要一开始把所有第三方命中都设置为 `reject`。

## 13. 阿里云故障处理

需要处理 DNS 失败、连接失败、请求超时、HTTP 429、HTTP 5xx、AppCode 错误、套餐额度耗尽和返回 JSON 结构变化。

推荐策略：

| 场景 | 处理 |
|---|---|
| 本地高风险词命中 | 直接拒绝，不调用阿里云 |
| 阿里云正常且未命中 | 正常发布 |
| 阿里云明确高风险 | 拒绝 |
| 阿里云风险不明确 | 人工审核 |
| 阿里云超时 | 人工审核 |
| AppCode 错误 | 告警，不静默放行 |
| 连续服务不可用 | 告警并启动降级策略 |

日志中禁止记录 AppCode、完整用户原文和完整阿里云响应。建议只记录：

```text
provider
HTTP status
request_id
latency_ms
error_type
```

## 14. 审核记录增加来源

当前 `community_moderation_task` 已经记录风险等级、命中词、原文和展示内容。建议增加：

```sql
ALTER TABLE community_moderation_task
ADD COLUMN provider varchar(32)
DEFAULT 'local'
COMMENT 'local/aliyun_market/manual';
```

来源值：

```text
local          本地词库命中
aliyun_market  阿里云命中
manual         管理员人工发现
```

记录审核任务时保存：

```python
"provider": decision.provider
```

如果暂时不改表，也可以把来源放入 `reason` 字段。

## 15. 本地词库维护方式

本地词库继续使用：

```text
config_sensitive_word
```

添加词条：

```sql
INSERT INTO config_sensitive_word
    (word, category, level, action, is_active)
VALUES
    ('业务词条', '广告引流', 1, 'replace', 1);
```

建议策略：

| 风险 | 配置 |
|---|---|
| 明确违法、严重诈骗 | `level=3, action='reject'` |
| 需要上下文判断 | `level=2, action='manual_review'` |
| 普通广告、引流、联系方式 | `level=1, action='replace'` |

停用词条：

```sql
UPDATE config_sensitive_word
SET is_active = 0
WHERE word = '业务词条';
```

恢复词条：

```sql
UPDATE config_sensitive_word
SET is_active = 1
WHERE word = '业务词条';
```

修改规则：

```sql
UPDATE config_sensitive_word
SET level = 2,
    action = 'manual_review',
    category = '其他'
WHERE word = '业务词条';
```

当前本地词库有约 60 秒缓存，修改数据库后需要等待约 60 秒或重启后端服务。

## 16. 测试方案

单元测试应覆盖：

- 配置关闭时不调用阿里云；
- 空文本不调用阿里云；
- AppCode 缺失时正确失败；
- 正常响应返回 `allow`；
- 命中响应返回正确动作；
- 返回结构异常；
- HTTP 429；
- HTTP 5xx；
- 请求超时；
- `fail_mode=review`；
- `fail_mode=reject`；
- 不打印 AppCode；
- 不打印用户原文。

本地词库应测试正常内容、replace、reject、manual_review、多个词同时命中、不同等级、同等级不同动作、空词库和停用词。

业务入口至少测试：

```text
POST /api/v1/community/posts
PUT /api/v1/community/posts/{post_id}
POST 评论
POST 纸飞机
POST 纸飞机回复
POST 纸飞机会话消息
```

验证是否只调用一次阿里云、是否保存替换内容、是否创建审核任务、待审核内容是否隐藏、通知是否正确、纸飞机额度是否退还，以及阿里云故障时是否按策略处理。

## 17. 上线配置

开发环境：

```env
ALIYUN_CONTENT_MODERATION_ENABLED=true
ALIYUN_CONTENT_MODERATION_FAIL_MODE=review
ALIYUN_CONTENT_MODERATION_DEFAULT_ACTION=manual_review
```

测试环境使用同样配置。

生产初期建议：

```env
ALIYUN_CONTENT_MODERATION_ENABLED=true
ALIYUN_CONTENT_MODERATION_FAIL_MODE=review
ALIYUN_CONTENT_MODERATION_DEFAULT_ACTION=manual_review
```

运行稳定并确认误判率后，再将明确高风险结果改为 `reject`。

## 18. 合规注意事项

接入前确认：服务协议、数据处理范围、用户文本是否保存、保存期限、是否涉及跨境传输、个人信息处理条款、API 调用计费，以及用户协议和隐私政策是否覆盖第三方内容审核。

当前项目会在审核任务中保存原文。将用户原文发送给第三方前，必须确认隐私政策已经覆盖该处理行为。

## 19. 最终实施顺序

1. 购买云市场敏感词 API。
2. 获取 AppCode。
3. 获取真实 API path、HTTP method、字段和响应 JSON。
4. 使用 `curl` 单独测试。
5. 增加环境变量。
6. 将 `httpx` 放入正式依赖。
7. 新增阿里云适配器。
8. 增加统一 `moderate_text()`。
9. 修改所有社区文本写入口。
10. 增加审核来源。
11. 增加超时、429、5xx 测试。
12. 以 `manual_review` 模式上线。
13. 统计误判率。
14. 根据实际结果调整 `reject`、`replace` 和 `manual_review`。
15. 后续开发敏感词管理后台、版本、来源、审核人和回滚功能。

当前真正需要从云市场接口文档取得的关键内容是：

```text
API path
HTTP method
request field
Content-Type
success response
hit response
error response
```

这些字段确认后，适配器中的 `YOUR_API_PATH`、请求字段和结果解析器才能改成完全准确的生产实现。
