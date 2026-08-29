"""AI providers and the provider registry.

``MockAIProvider`` is the deterministic fixture provider with failure injection.
Real providers share the ``_OpenAICompatProvider`` base (OpenAI-compatible chat
API + JSON output mode): ``DeepSeekAIProvider`` was the first, and
``DotsAIProvider``（小红书 hi lab dots.llm）follows the same pattern. They are
intended for development/testing only — production enablement requires the full
approval gate (``ai_policy_approved`` / ``ai_provider_approved`` / retention).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import SecretStr, ValidationError

from app.core.config import settings
from app.schemas.ai_profile import ProfileSubject
from app.services.ai.base import (
    AIProvider,
    ExtractedEntry,
    ExtractedField,
    ModerationRequest,
    ModerationResult,
    NarrativeDimension,
    NarrativeHistoryObservation,
    NarrativeIdealWeight,
    NarrativeRecentChange,
    NarrativeRequest,
    NarrativeResult,
    ProviderError,
    ProviderErrorKind,
    ReplyRequest,
    ReplyResult,
    SearchCondition,
    SearchParseRequest,
    SearchParseResult,
    StructuredExtractRequest,
    StructuredExtractResult,
)
from app.services.ai.prompts.profile_extract import build_profile_extract_prompt
from app.services.ai.prompts.profile_narrative import build_profile_narrative_prompt
from app.services.ai.prompts.search_parse import build_search_parse_prompt
from app.services.ai.prompts.voice_reply import build_voice_reply_prompt

logger = logging.getLogger(__name__)

# OpenAI SDK 异常类与客户端构造（DeepSeek 兼容 OpenAI API）。
#
# 依赖选择理由（PROJECT_RULES §2.4.3）：项目已有 httpx，但 DeepSeek 是 OpenAI 兼容
# API，openai SDK 提供了类型化异常分类（RateLimitError / APITimeoutError /
# APIConnectionError / APIError 及其 4xx 子类）、自动重试、response_format JSON
# 模式辅助与 AsyncOpenAI 异步客户端，裸 httpx 需要手写这些能力且易出错。openai
# 是 MIT 许可、活跃维护的成熟库，与现有 httpx 共存（openai 内部亦依赖 httpx），
# 无版本冲突风险。替代方案 httpx 已评估但不采用，理由如上。
from openai import (  # noqa: E402
    APIConnectionError as _APIConnectionError,
    APIError as _APIError,
    APITimeoutError as _APITimeoutError,
    AsyncOpenAI,
    RateLimitError as _RateLimitError,
)
# 4xx 状态码异常子类，用于区分可重试与不可重试。
from openai import (  # noqa: E402
    APIStatusError as _APIStatusError,
    AuthenticationError as _AuthenticationError,
    BadRequestError as _BadRequestError,
    PermissionDeniedError as _PermissionDeniedError,
)


def _build_openai_compat_client(
    api_key: Any, base_url: str, *, timeout: float = 30.0
) -> AsyncOpenAI:
    """构造 OpenAI 兼容 API 的异步客户端（DeepSeek / Dots 共用）。

    timeout 必须显式传入——SDK 默认 600s，配合 worker 心跳续租会形成
    "合法死锁"（挂起调用占住 worker 10 分钟）。由 settings.ai_gateway_timeout_seconds
    提供（默认 30.0，范围 0-120）。
    """
    return AsyncOpenAI(
        api_key=api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key,
        base_url=base_url,
        timeout=timeout,
    )

# Deterministic fixture field values for profile extraction. Keys are the
# frozen allowlist field names; values are (value, source_quote, confidence).
_PERSONAL_PROFILE_FIXTURE_FIELDS: dict[str, tuple[Any, str | None, float]] = {
    "interest_tags": (["旅行", "看展"], "周末喜欢旅行和看展", 0.91),
    "city_code": ("330100", "住在杭州", 0.95),
    "marriage_status": ("single", "未婚", 0.90),
    "education_level": (4, "本科", 0.93),
    "height_cm": (172, "身高172cm", 0.97),
    "income_band": (2, "月收入一档到二档", 0.72),
    "occupation_group": ("technology", "互联网做技术", 0.85),
    "lifestyle_tags": (["户外"], "周末愿意户外", 0.78),
    "relationship_goal": ("marriage", "想认真奔着结婚", 0.88),
}

_IDEAL_PARTNER_FIXTURE_FIELDS: dict[str, tuple[Any, str | None, float]] = {
    "age": ({"min": 26, "max": 32}, "26到32岁", 0.92),
    "city_code": (("330100", "330200"), "杭州或宁波", 0.95),
    "marriage_status": (("single",), "希望未婚", 0.90),
    "education_level": ({"min": 3, "max": None}, "本科及以上", 0.89),
    "height_cm": ({"min": 160, "max": 180}, "身高160到180", 0.96),
    "income_band": ({"min": 10000, "max": None}, "月收入至少一万", 0.83),
    "occupation_group": (("technology", "education"), "技术或教育行业", 0.76),
    "interest_tags": (("旅行", "音乐"), "喜欢旅行和音乐", 0.81),
    "lifestyle_tags": (("户外",), "愿意周末户外", 0.74),
    "relationship_goal": (("marriage",), "以结婚为目标", 0.88),
}

_SEARCH_FIXTURE_CONDITIONS: tuple[SearchCondition, ...] = (
    SearchCondition(
        field_key="age",
        operator="between",
        value={"min": 26, "max": 32},
        kind="hard",
        confidence=0.99,
        source_span="26到32岁",
    ),
    SearchCondition(
        field_key="city_code",
        operator="eq",
        value="330100",
        kind="hard",
        confidence=0.95,
        source_span="住杭州",
    ),
    SearchCondition(
        field_key="education_level",
        operator="gte",
        value=4,
        kind="hard",
        confidence=0.90,
        source_span="本科以上",
    ),
    SearchCondition(
        field_key="interest_tags",
        operator="contains",
        value="户外",
        kind="soft",
        confidence=0.78,
        source_span="周末愿意户外",
    ),
)


# Narrative fixtures for MockAIProvider — aligned with the frontend mock
# (mock/ai-profile.uts mockGetPortraitNarrative) so tests see the same shape.
_NARRATIVE_FIXTURE_PERSONAL = NarrativeResult(
    persona_title="慢热但真诚的长期主义者",
    persona_tags=("慢热", "真诚", "边界感", "长期主义"),
    insight="你看起来并不依赖高频陪伴,但对于重要的人,你希望彼此能够真正回应。",
    dimensions=(
        NarrativeDimension(
            key="relationship", icon="relationship", title="感情观",
            summary="希望建立稳定、长期,但彼此保留个人空间的关系。",
        ),
        NarrativeDimension(
            key="personality", icon="personality", title="性格",
            summary="慢热,熟悉以后表达欲明显增加。",
        ),
        NarrativeDimension(
            key="lifestyle", icon="lifestyle", title="生活方式",
            summary="喜欢相对规律、安静、有自己节奏的生活。",
        ),
        NarrativeDimension(
            key="future", icon="future", title="人生规划",
            summary="对未来有比较明确的方向,希望另一半也拥有自己的目标。",
        ),
    ),
    ideal_weights=(),
    recent_change=NarrativeRecentChange(
        direction="up",
        summary="比三个月前,你现在更看重「稳定」",
        observation="过去你更容易被有趣吸引,现在你开始更加关注长期相处是否舒服。",
    ),
    history_observations=(
        NarrativeHistoryObservation(
            revision_id=1,
            keywords=("稳定", "长期主义", "边界感"),
            observation="你现在比以前更加确定自己想要怎样的关系。",
        ),
    ),
    conclusion="总的来说，你要的并不复杂——一份能彼此回应的关系，和一段能一起慢慢走远的路。",
)

_NARRATIVE_FIXTURE_IDEAL_PARTNER = NarrativeResult(
    persona_title="温柔稳定且拥有自己世界的人",
    persona_tags=("真诚", "稳定", "有目标", "边界感", "愿意沟通"),
    insight="你更看重对方在重要时刻的回应,而不是日常的高频陪伴。",
    dimensions=(
        NarrativeDimension(
            key="relationship", icon="relationship", title="感情观",
            summary="希望建立稳定、长期,但彼此保留个人空间的关系。",
        ),
        NarrativeDimension(
            key="personality", icon="personality", title="性格",
            summary="期待对方情绪稳定,熟悉以后愿意表达。",
        ),
        NarrativeDimension(
            key="lifestyle", icon="lifestyle", title="生活方式",
            summary="希望对方有相对规律、安静、有自己节奏的生活。",
        ),
        NarrativeDimension(
            key="future", icon="future", title="人生规划",
            summary="希望另一半也拥有自己的目标与方向。",
        ),
    ),
    ideal_weights=(
        NarrativeIdealWeight(key="values", label="价值观", percent=92),
        NarrativeIdealWeight(key="communication", label="沟通方式", percent=88),
        NarrativeIdealWeight(key="emotion", label="情绪稳定", percent=84),
        NarrativeIdealWeight(key="lifestyle", label="生活节奏", percent=73),
        NarrativeIdealWeight(key="appearance", label="外在条件", percent=41),
    ),
    recent_change=NarrativeRecentChange(
        direction="up",
        summary="比三个月前,你现在更看重「稳定」",
        observation="过去你更容易被有趣吸引,现在你开始更加关注长期相处是否舒服。",
    ),
    history_observations=(
        NarrativeHistoryObservation(
            revision_id=1,
            keywords=("稳定", "长期主义", "边界感"),
            observation="你现在比以前更加确定自己想要怎样的关系。",
        ),
    ),
    conclusion="总的来说，你要的并不复杂——一份能彼此回应的关系，和一段能一起慢慢走远的路。",
)


class MockAIProvider:
    """Deterministic fixture provider with failure injection.

    ``failures`` accepts any of: ``timeout``, ``http_429``, ``http_500``,
    ``schema_invalid``, ``policy_blocked``.  A ``"<method>:<name>"`` entry
    scopes the failure to one method (``structured_extract``,
    ``parse_search_query``, ``moderate_text``).
    """

    def __init__(
        self,
        failures: Iterable[str] = (),
        response_delay_seconds: float = 0.0,
    ) -> None:
        self._failures = set(failures)
        self._response_delay_seconds = response_delay_seconds

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------
    async def structured_extract(
        self, request: StructuredExtractRequest
    ) -> StructuredExtractResult:
        self._check_failure("structured_extract")
        fixture = (
            "profile-ideal-partner-v1"
            if request.subject == ProfileSubject.IDEAL_PARTNER.value
            else "profile-personal-v1"
        )
        if "schema_invalid" in self._failures or "structured_extract:schema_invalid" in self._failures:
            fixture = "profile-schema-invalid"
        return await self.structured_extract_fixture(fixture, request=request)

    async def parse_search_query(
        self, request: SearchParseRequest
    ) -> SearchParseResult:
        self._check_failure("parse_search_query")
        conditions = tuple(
            condition
            for condition in _SEARCH_FIXTURE_CONDITIONS
            if condition.field_key in request.allowlist
        )
        unknown: tuple[str, ...] = ()
        if "pure_free" in request.query_text:
            unknown = ("pure_free",)
        return SearchParseResult(
            schema_version="search-condition-v1",
            conditions=conditions,
            unknown=unknown,
        )

    async def moderate_text(
        self, request: ModerationRequest
    ) -> ModerationResult:
        self._check_failure("moderate_text")
        blocked = any(
            token in request.text
            for token in ("联系方式", "加微信", "敏感", "绝对保证")
        )
        if blocked:
            return ModerationResult(allowed=False, action="reject", reason_code="SENSITIVE_TEXT")
        return ModerationResult(allowed=True, action="allow")

    async def generate_narrative(
        self, request: NarrativeRequest
    ) -> NarrativeResult:
        self._check_failure("generate_narrative")
        is_personal = request.subject == ProfileSubject.PERSONAL.value
        return _NARRATIVE_FIXTURE_PERSONAL if is_personal else _NARRATIVE_FIXTURE_IDEAL_PARTNER

    async def generate_reply(
        self, request: ReplyRequest
    ) -> ReplyResult:
        self._check_failure("generate_reply")
        return ReplyResult(reply_text="好的，记下啦。那你现在生活在哪个城市呀？")

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
    ):
        """开发对话台用的确定性流：先推理片段，再正文。"""
        self._check_failure("stream_chat")
        last_user = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                last_user = item.get("content") or ""
                break
        preview = last_user.replace("\n", " ").strip()[:24] or "（空输入）"
        yield ("reasoning", "mock 不调用外网，按当前输入回放一段固定回复。")
        if json_mode:
            yield (
                "content",
                '{"reply_text":"好的，记下啦。那你现在生活在哪个城市呀？"}',
            )
        else:
            yield ("content", f"这是 mock 回复。你刚才说：{preview}")
        yield ("finish", "stop")

    # ------------------------------------------------------------------
    # Deterministic fixture accessor (used by the acceptance test)
    # ------------------------------------------------------------------
    async def structured_extract_fixture(
        self,
        fixture_name: str,
        request: StructuredExtractRequest | None = None,
    ) -> StructuredExtractResult:
        """Return a deterministic typed extraction fixture.

        ``profile-interest-v1`` is the canonical acceptance fixture: its first
        field is ``interest_tags`` with ``confirmation_status == "suggested"``.
        """
        self._check_failure("structured_extract")
        allowlist = request.allowlist if request is not None else None
        if fixture_name == "profile-interest-v1":
            fields = self._fields_for(
                ("interest_tags",),
                allowlist,
                ProfileSubject.PERSONAL,
                _PERSONAL_PROFILE_FIXTURE_FIELDS,
                request,
            )
        elif fixture_name == "profile-schema-invalid":
            fields = (
                ExtractedField.model_construct(
                    field_key="interest_tags",
                    subject=ProfileSubject.PERSONAL,
                    value=["旅行"],
                    confidence=1.7,  # schema violation: confidence outside 0..1
                ),
            )
        elif fixture_name == "profile-ideal-partner-v1":
            fields = self._fields_for(
                tuple(_IDEAL_PARTNER_FIXTURE_FIELDS),
                allowlist,
                ProfileSubject.IDEAL_PARTNER,
                _IDEAL_PARTNER_FIXTURE_FIELDS,
                request,
            )
        else:
            # profile-personal-v1 and the default fallback.
            fields = self._fields_for(
                tuple(_PERSONAL_PROFILE_FIXTURE_FIELDS),
                allowlist,
                ProfileSubject.PERSONAL,
                _PERSONAL_PROFILE_FIXTURE_FIELDS,
                request,
            )
        return StructuredExtractResult(
            schema_version="profile-extract-v1", fields=fields
        )

    # ------------------------------------------------------------------
    # Failure injection helpers
    # ------------------------------------------------------------------
    def _check_failure(self, method: str) -> None:
        """Raise the configured failure for the method, if any."""
        failure_map: dict[str, tuple[str, ProviderErrorKind, int]] = {
            "timeout": ("AI_TEMPORARILY_UNAVAILABLE", ProviderErrorKind.RETRYABLE, 0),
            "http_429": ("AI_QUOTA_EXCEEDED", ProviderErrorKind.RETRYABLE, 2000),
            "http_500": ("AI_TEMPORARILY_UNAVAILABLE", ProviderErrorKind.RETRYABLE, 1000),
            "policy_blocked": ("AI_POLICY_DENIED", ProviderErrorKind.NON_RETRYABLE, 0),
        }
        for name, (code, kind, retry_after_ms) in failure_map.items():
            if name in self._failures or f"{method}:{name}" in self._failures:
                raise ProviderError(
                    code=code,
                    message=f"mock provider injected failure: {name}",
                    kind=kind,
                    retry_after_ms=retry_after_ms,
                )
        if "schema_invalid" in self._failures or f"{method}:schema_invalid" in self._failures:
            # schema_invalid does not raise; it returns an invalid result that
            # the Gateway must reject as a non-retryable schema violation.
            return

    def _fields_for(
        self,
        keys: tuple[str, ...],
        allowlist: frozenset[str] | None,
        subject: ProfileSubject,
        fixture_fields: dict[str, tuple[Any, str | None, float]],
        request: StructuredExtractRequest | None,
    ) -> tuple[ExtractedField, ...]:
        fields: list[ExtractedField] = []
        for key in keys:
            if allowlist is not None and key not in allowlist:
                continue
            value, source_quote, confidence = fixture_fields[key]
            fields.append(
                ExtractedField(
                    field_key=key,
                    subject=subject,
                    value=value,
                    source_quote=source_quote,
                    source_span=source_quote,
                    confidence=confidence,
                    needs_confirmation=True,
                    confirmation_status="suggested",
                    schema_version="profile-extract-v1",
                    prompt_version="profile-extract-prompt-v1",
                    policy_revision=(
                        request.policy_revision
                        if request is not None
                        else "ai-policy-2026-08-07-v1"
                    ),
                )
            )
        return tuple(fields)


# ==================== OpenAI 兼容真 provider（DeepSeek / Dots） ====================

# 版本常量与业务模块保持一致，确保审计元数据可追溯。
_PROFILE_SCHEMA_VERSION = "profile-extract-v1"
_PROFILE_PROMPT_VERSION = "profile-extract-prompt-v1"
_SEARCH_SCHEMA_VERSION = "search-condition-v1"
_SEARCH_PROMPT_VERSION = "search-parse-prompt-v1"

_MODERATION_SYSTEM = (
    "你是文本安全审核器。判断以下文本是否包含联系方式、引流、"
    "色情、暴力、诈骗或其它违规内容。以 JSON 格式输出："
    "{\"allowed\": true 或 false, \"reason_code\": 违规类型或 null}。"
)


def _delta_reasoning_and_content(delta: Any) -> tuple[str, str]:
    """从流式 delta 或完整 message 取出推理文本与正文。

    dots3-note-prev 等推理模型会把思维链放在 ``reasoning_content``，
    正式回复在 ``content``。其它供应商通常只有 content。
    """
    content = getattr(delta, "content", None) or ""
    reasoning = (
        getattr(delta, "reasoning_content", None)
        or getattr(delta, "reasoning", None)
        or ""
    )
    extra = getattr(delta, "model_extra", None)
    if not reasoning and isinstance(extra, dict):
        reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
    return str(reasoning or ""), str(content or "")


def _safe_confidence(value: Any) -> float:
    """把模型返回的 confidence 安全转为 0-1 浮点，非法值回退到 1.0。

    防御 ``float(None)``（模型返回 JSON null）和 ``float("高")`` 抛 TypeError/
    ValueError 绕过 Gateway 的 ProviderError 分类。
    """
    if isinstance(value, bool):  # bool 是 int 子类，先排除
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _parse_json_response(content: str) -> Any:
    """解析 OpenAI 兼容 provider 返回的 JSON 内容。

    空内容视为 provider 未生成有效输出(NON_RETRYABLE,配置问题);
    非合法 JSON 视为偶发输出漂移(RETRYABLE,重试可能成功)。
    """
    if not content or not content.strip():
        raise ProviderError(
            code="AI_INPUT_INVALID",
            message="provider 返回空内容",
            kind=ProviderErrorKind.NON_RETRYABLE,
        )
    try:
        return json.loads(content)
    except (ValueError, TypeError) as exc:
        raise ProviderError(
            code="AI_TEMPORARILY_UNAVAILABLE",
            message=f"provider 返回非合法 JSON: {exc}",
            kind=ProviderErrorKind.RETRYABLE,
        ) from exc


# ideal_weights 维度中文名 → 稳定 key（与前端 mock / DESIGN 组件语言对齐）。
_IDEAL_WEIGHT_KEY_BY_LABEL = {
    "价值观": "values",
    "沟通方式": "communication",
    "情绪稳定": "emotion",
    "生活节奏": "lifestyle",
    "外在条件": "appearance",
}

# 叙事维度 icon 白名单。dots 实测会把 ⌂ 写成 "<Vertex" 这类伪标签，
# 小程序 rich-text/text 会把它当 HTML 起始标签，页面叠字。
_NARRATIVE_DIMENSION_ICON_BY_KEY = {
    "relationship": "relationship",
    "personality": "personality",
    "lifestyle": "lifestyle",
    "future": "future",
}
_NARRATIVE_DIMENSION_ICON_ALIASES = {
    "♡": "relationship",
    "☀": "personality",
    "⌂": "lifestyle",
    "↗": "future",
    "relationship": "relationship",
    "personality": "personality",
    "lifestyle": "lifestyle",
    "future": "future",
}


def sanitize_narrative_dimension_icon(key: str, icon: Any) -> str:
    """把模型输出的 emoji / 伪标签归一为维度 token 名，供前端映射 iconfont。"""
    fallback = _NARRATIVE_DIMENSION_ICON_BY_KEY.get(str(key) or "", "·")
    if not isinstance(icon, str):
        return fallback
    text = icon.strip()
    if not text or "<" in text or ">" in text or "/" in text:
        return fallback
    mapped = _NARRATIVE_DIMENSION_ICON_ALIASES.get(text)
    if mapped is not None:
        return mapped
    if text in _NARRATIVE_DIMENSION_ICON_BY_KEY:
        return text
    return fallback


def _normalize_narrative_payload(data: Any) -> Any:
    """把模型对叙事 JSON 的常见漂移写法归一为 schema 期望的形状。

    实测 dots3-note-prev（推理模型）偶发输出：
    - ``ideal_weights`` 项用 ``{"dimension", "weight"}``；
    - ``dimensions`` 项把 ``summary`` 写成 ``string``；
    - ``recent_change`` 输出成纯字符串（如"无明显变化"）而非对象；
    - ``history_observations`` 的 ``revision_id`` 写成 ``"version2"``。
    在 Gateway schema 校验前做宽容映射；无法识别的内容保持原样交给校验拦截。
    """
    if not isinstance(data, dict):
        return data
    dims = data.get("dimensions")
    if isinstance(dims, list):
        for item in dims:
            if not isinstance(item, dict):
                continue
            if "summary" not in item and isinstance(item.get("string"), str):
                item["summary"] = item.pop("string")
            item["icon"] = sanitize_narrative_dimension_icon(
                str(item.get("key") or ""), item.get("icon")
            )
    if isinstance(data.get("recent_change"), str):
        # 无 direction 的纯文本变化描述无法满足 up|down 约束，按无变化处理。
        data["recent_change"] = None
    observations = data.get("history_observations")
    if isinstance(observations, list):
        fixed: list[Any] = []
        for item in observations:
            if isinstance(item, dict) and isinstance(item.get("revision_id"), str):
                digits = re.sub(r"\D", "", item["revision_id"])
                if not digits:
                    continue
                item["revision_id"] = int(digits)
            fixed.append(item)
        data["history_observations"] = fixed
    weights = data.get("ideal_weights")
    if not isinstance(weights, list):
        return data
    normalized: list[Any] = []
    for item in weights:
        if (
            isinstance(item, dict)
            and "key" not in item
            and isinstance(item.get("dimension"), str)
            and item.get("weight") is not None
        ):
            label = item["dimension"]
            normalized.append(
                {
                    "key": _IDEAL_WEIGHT_KEY_BY_LABEL.get(label, label),
                    "label": label,
                    "percent": item["weight"],
                }
            )
        else:
            normalized.append(item)
    data["ideal_weights"] = normalized
    return data


class _OpenAICompatProvider:
    """OpenAI 兼容 chat API provider 共享基类（DeepSeek / Dots 复用）。

    使用 JSON output mode（response_format=json_object）获取结构化输出，
    再映射为 ``AIProvider`` Protocol 要求的类型化结果。Gateway 会对返回值做
    二次 schema 校验，因此即使模型输出偏差也会被拦在业务下游之外。

    子类只需在 __init__ 中从 settings 解析各自的 api_key / base_url / model /
    max_tokens 后调用 super().__init__()；异常到 ProviderError 的映射与四个
    Protocol 方法实现均在基类。
    """

    def __init__(
        self,
        api_key: SecretStr | None,
        base_url: str,
        model: str,
        max_tokens: int,
        api_key_env: str,
        **kwargs: Any,
    ) -> None:
        # 延迟 key 校验到首次调用：__init__ 阶段抛异常会绕过 Gateway.invoke 的
        # except ProviderError（AIGateway 在 handler 内构造），被 worker 的
        # except Exception 误判为 retryable=True。改为在 _chat_json 内抛
        # ProviderError(NON_RETRYABLE)，由 Gateway.invoke 正确分类。
        self._api_key = api_key
        self._base_url = base_url
        # 测试可通过 kwargs 注入 mock client；生产/开发从 settings 读 key 构造。
        self._client = kwargs.pop("client", None)
        self._model = model
        self._max_tokens = max_tokens
        self._api_key_env = api_key_env

    def _ensure_client(self) -> Any:
        """惰性构造客户端；缺 key 时抛 NON_RETRYABLE ProviderError。

        该方法在 _chat_json（即 Gateway.invoke 的 try 块内）被调用，因此抛出的
        ProviderError 会被 Gateway 的 except ProviderError 正确分类为
        non-retryable，不会被 worker 误判为可重试。
        """
        if self._client is not None:
            return self._client
        if self._api_key is None:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message=(
                    f"{self.__class__.__name__} 缺少 API key，请在 .env 配置 "
                    f"{self._api_key_env}（仅开发/测试环境）"
                ),
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        self._client = _build_openai_compat_client(
            self._api_key, self._base_url, timeout=settings.ai_gateway_timeout_seconds
        )
        return self._client

    def _map_openai_exception(self, exc: Exception) -> ProviderError:
        """把 OpenAI SDK 异常映射为稳定的 ProviderError。"""
        if isinstance(exc, _RateLimitError):
            return ProviderError(
                code="AI_QUOTA_EXCEEDED",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
                retry_after_ms=2000,
            )
        if isinstance(exc, (_APITimeoutError, _APIConnectionError)):
            return ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            )
        if isinstance(exc, (_AuthenticationError, _PermissionDeniedError, _BadRequestError)):
            return ProviderError(
                code="AI_INPUT_INVALID",
                message=str(exc),
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        if isinstance(exc, _APIStatusError):
            return ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            )
        if isinstance(exc, _APIError):
            return ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            )
        return ProviderError(
            code="AI_TEMPORARILY_UNAVAILABLE",
            message=str(exc),
            kind=ProviderErrorKind.RETRYABLE,
        )

    async def _chat_json(self, prompt: str) -> Any:
        """调用 OpenAI 兼容 chat API 并返回解析后的 JSON 对象。"""
        return await self._chat_json_with_max(prompt, self._max_tokens)

    async def _chat_json_with_max(self, prompt: str, max_tokens: int) -> Any:
        """调用 OpenAI 兼容 chat API 并返回解析后的 JSON 对象（自定义 max_tokens）。"""
        client = self._ensure_client()
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
        except (
            _RateLimitError,
            _APITimeoutError,
            _APIConnectionError,
            _AuthenticationError,
            _PermissionDeniedError,
            _BadRequestError,
            _APIStatusError,
            _APIError,
        ) as exc:
            raise self._map_openai_exception(exc) from exc

        if not response.choices:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message="provider 返回空 choices",
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        content = response.choices[0].message.content
        return _parse_json_response(content)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
    ):
        """流式调用 OpenAI 兼容 chat API，产出 (kind, text) 片段。

        kind 为 ``reasoning`` / ``content`` / ``finish``。仅开发对话台使用，
        不进入生产 Protocol。JSON 模式若供应商拒绝 stream+json_object，
        会回退到一次非流式调用，再把完整正文作为单段 content 产出。
        """
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "stream": True,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            stream = await client.chat.completions.create(**kwargs)
        except _BadRequestError as exc:
            if json_mode:
                async for item in self._stream_json_fallback(client, messages):
                    yield item
                return
            raise self._map_openai_exception(exc) from exc
        except (
            _RateLimitError,
            _APITimeoutError,
            _APIConnectionError,
            _AuthenticationError,
            _PermissionDeniedError,
            _APIStatusError,
            _APIError,
        ) as exc:
            raise self._map_openai_exception(exc) from exc

        finish_reason = "stop"
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            reason = getattr(choice, "finish_reason", None)
            if reason:
                finish_reason = str(reason)
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            reasoning, content = _delta_reasoning_and_content(delta)
            if reasoning:
                yield ("reasoning", reasoning)
            if content:
                yield ("content", content)
        yield ("finish", finish_reason)

    async def _stream_json_fallback(self, client: Any, messages: list[dict[str, str]]):
        """供应商不支持 stream+json_object 时的非流式回退。"""
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=self._max_tokens,
            )
        except (
            _RateLimitError,
            _APITimeoutError,
            _APIConnectionError,
            _AuthenticationError,
            _PermissionDeniedError,
            _BadRequestError,
            _APIStatusError,
            _APIError,
        ) as exc:
            raise self._map_openai_exception(exc) from exc
        if not response.choices:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message="provider 返回空 choices",
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        message = response.choices[0].message
        reasoning, content = _delta_reasoning_and_content(message)
        if reasoning:
            yield ("reasoning", reasoning)
        if content:
            yield ("content", content)
        yield ("finish", "stop")

    # ------------------------------------------------------------------
    # AIProvider Protocol
    # ------------------------------------------------------------------
    async def structured_extract(
        self, request: StructuredExtractRequest
    ) -> StructuredExtractResult:
        prompt = build_profile_extract_prompt(
            request.subject,
            request.turn_texts,
            target_field_key=request.target_field_key,
        )
        data = await self._chat_json(prompt)
        fields_data = data.get("fields", []) if isinstance(data, dict) else []
        fields: list[ExtractedField] = []
        subject = ProfileSubject(request.subject)
        for item in fields_data:
            if not isinstance(item, dict):
                continue
            field_key = item.get("field_key", "")
            if field_key not in request.allowlist:
                continue
            fields.append(
                ExtractedField(
                    field_key=field_key,
                    subject=subject,
                    value=item.get("value"),
                    source_quote=item.get("source_quote"),
                    confidence=_safe_confidence(item.get("confidence")),
                    needs_confirmation=True,
                    confirmation_status="suggested",
                    schema_version=_PROFILE_SCHEMA_VERSION,
                    prompt_version=_PROFILE_PROMPT_VERSION,
                    policy_revision=request.policy_revision,
                )
            )
        # WP-P1：条目通道。category/content 由 ExtractedEntry 的 Pydantic
        # 校验把关（9 枚举 + ≤200 字）；非法条目整条丢弃，不让坏数据进草稿。
        entries_data = data.get("entries", []) if isinstance(data, dict) else []
        entries: list[ExtractedEntry] = []
        for item in entries_data:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    ExtractedEntry(
                        category=item.get("category", ""),
                        content=item.get("content", ""),
                        subject=subject,
                        source_quote=item.get("source_quote"),
                        confidence=_safe_confidence(item.get("confidence")),
                        needs_confirmation=True,
                        confirmation_status="suggested",
                        schema_version=_PROFILE_SCHEMA_VERSION,
                        prompt_version=_PROFILE_PROMPT_VERSION,
                        policy_revision=request.policy_revision,
                    )
                )
            except ValidationError:
                continue
        return StructuredExtractResult(
            schema_version=_PROFILE_SCHEMA_VERSION,
            fields=tuple(fields),
            entries=tuple(entries),
        )

    async def parse_search_query(
        self, request: SearchParseRequest
    ) -> SearchParseResult:
        prompt = build_search_parse_prompt(request.query_text)
        data = await self._chat_json(prompt)
        conditions_data = data.get("conditions", []) if isinstance(data, dict) else []
        conditions: list[SearchCondition] = []
        for item in conditions_data:
            if not isinstance(item, dict):
                continue
            field_key = item.get("field_key", "")
            if field_key not in request.allowlist:
                continue
            conditions.append(
                SearchCondition(
                    field_key=field_key,
                    operator=item.get("operator", ""),
                    value=item.get("value"),
                    kind=item.get("kind", "hard"),
                    confidence=_safe_confidence(item.get("confidence")),
                    source_span=item.get("source_span"),
                )
            )
        return SearchParseResult(
            schema_version=_SEARCH_SCHEMA_VERSION,
            conditions=tuple(conditions),
        )

    async def moderate_text(
        self, request: ModerationRequest
    ) -> ModerationResult:
        prompt = f"{_MODERATION_SYSTEM}\n\n待审核文本：\n{request.text}"
        data = await self._chat_json(prompt)
        # 审核闸门 fail-closed：无法解析或字段缺失时拒绝，不放行。
        if not isinstance(data, dict):
            return ModerationResult(
                allowed=False, action="review", reason_code="MODERATION_PARSE_FAILED"
            )
        # 显式判断 True（避免 bool("false") == True 的真值陷阱）。
        allowed_value = data.get("allowed")
        if allowed_value is True:
            return ModerationResult(allowed=True, action="allow")
        return ModerationResult(
            allowed=False,
            action="reject",
            reason_code=data.get("reason_code") or "SENSITIVE_TEXT",
        )

    async def generate_narrative(
        self, request: NarrativeRequest
    ) -> NarrativeResult:
        prompt = build_profile_narrative_prompt(
            request.subject,
            request.current_fields,
            request.previous_fields,
            request.history_summaries,
        )
        data = await self._chat_json(prompt)
        # NarrativeResult.model_validate 做完整字段校验（长度/direction/
        # percent 范围）。生成是非确定性的：偶发 schema 漂移（归一化覆盖不到
        # 的）转为可重试 ProviderError，让 Worker 重新生成，而不是一次性判死。
        try:
            return NarrativeResult.model_validate(_normalize_narrative_payload(data))
        except ValidationError as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=f"narrative 输出未通过 schema 校验: {exc}",
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc

    async def generate_reply(
        self, request: ReplyRequest
    ) -> ReplyResult:
        prompt = build_voice_reply_prompt(
            request.transcript,
            request.field_key,
            request.known_fields,
        )
        # 语音回复极短（≤30字），用小 max_tokens 加速生成
        data = await self._chat_json_with_max(prompt, 128)
        # 回复是自由文本，模型偶发输出超长/空串：schema 漂移转为可重试
        # 错误，由调用方（对话编排器）降级到模板回复，对话不中断。
        try:
            return ReplyResult.model_validate(data)
        except ValidationError as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=f"reply 输出未通过 schema 校验: {exc}",
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc


class DeepSeekAIProvider(_OpenAICompatProvider):
    """DeepSeek 真 provider（OpenAI 兼容 API），配置取自 ai_deepseek_*。

    开发/测试环境可用；生产启用需先满足三道审批门禁（见 config.py）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            api_key=settings.ai_deepseek_api_key,
            base_url=settings.ai_deepseek_base_url,
            model=settings.ai_deepseek_model,
            max_tokens=settings.ai_deepseek_max_tokens,
            api_key_env="AI_DEEPSEEK_API_KEY",
            **kwargs,
        )


class DotsAIProvider(_OpenAICompatProvider):
    """Dots（小红书 hi lab dots.llm）真 provider（OpenAI 兼容 API）。

    配置取自 ai_dots_*；dots3-note-prev 为推理模型，响应含
    reasoning_content + content，_chat_json 只消费 content 中的 JSON。
    开发/测试环境可用；生产启用需先满足三道审批门禁（见 config.py）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            api_key=settings.ai_dots_api_key,
            base_url=settings.ai_dots_base_url,
            model=settings.ai_dots_model,
            max_tokens=settings.ai_dots_max_tokens,
            api_key_env="AI_DOTS_API_KEY",
            **kwargs,
        )


class AIProviderRegistry:
    """Provider registry keyed by configuration name."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., AIProvider]] = {
            "mock": MockAIProvider,
            "deepseek": DeepSeekAIProvider,
            "dots": DotsAIProvider,
        }

    def register(self, name: str, factory: Callable[..., AIProvider]) -> None:
        self._factories[name] = factory

    def create(self, name: str = "mock", **kwargs: Any) -> AIProvider:
        if name not in self._factories:
            raise KeyError(f"未知 AI provider: {name}")
        return self._factories[name](**kwargs)


_provider_registry = AIProviderRegistry()


def get_provider(name: str = "mock", **kwargs: Any) -> AIProvider:
    """Return a provider instance from the shared registry."""
    return _provider_registry.create(name, **kwargs)
