"""AI-CORE Gateway: schema validation, safety checks and audit boundary.

The Gateway is the only path through which a provider is called.  It performs
schema validation on every provider response, classifies failures as retryable
or not, and produces a minimal ``GatewayCallRecord`` for the audit trail.

Sensitive information never crosses the audit/log boundary: prompts, original
answers, provider raw responses and secrets are not part of any record produced
here.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.services.ai.base import (
    AIProvider,
    AITaskContext,
    GatewayCallRecord,
    ProviderError,
    ProviderErrorKind,
    SearchParseResult,
    StructuredExtractResult,
    ModerationResult,
)
from app.services.ai.audit import GenerationAuditEvent, record_generation_audit
from app.services.ai.providers import get_provider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class InvokeOutcome(Generic[T]):
    """Typed outcome of one Gateway invocation."""

    result: T | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    retry_after_ms: int = 0


_SCHEMA_VIOLATION_CODE = "AI_INPUT_INVALID"
_POLICY_DENIED_CODE = "AI_POLICY_DENIED"

# Safe copy for every stable error code the Gateway can emit.  The outward
# ``InvokeOutcome.error_message`` is always drawn from this mapping (or a
# generic fallback); a provider's raw ``ProviderError.message`` never reaches
# it, because real provider messages may embed raw response fragments.
_SAFE_ERROR_MESSAGES: dict[str, str] = {
    "AI_INPUT_INVALID": "provider 输出未通过 Schema 校验",
    "AI_POLICY_DENIED": "请求未通过 AI 安全与策略校验",
    "AI_QUOTA_EXCEEDED": "AI 服务请求频率过高，请稍后重试",
    "AI_TEMPORARILY_UNAVAILABLE": "AI 服务暂时不可用",
}
_DEFAULT_SAFE_ERROR_MESSAGE = "AI 服务调用失败"

# Keys that must never survive a provider message, even inside debug logs.
_PROVIDER_MESSAGE_SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "raw_response",
        "phone",
        "id_card",
        "precise_location",
        "raw_ip",
    }
)


def _safe_error_message(code: str) -> str:
    """Map a stable error code to fixed safe copy; never provider text."""
    return _SAFE_ERROR_MESSAGES.get(code, _DEFAULT_SAFE_ERROR_MESSAGE)


def _redact_provider_message(message: str) -> str:
    """Return a debug-safe rendering of a provider message.

    Only structured (JSON) messages are eligible: sensitive keys are removed
    recursively by name.  Unstructured provider text is never emitted verbatim
    (a substring filter could be bypassed and could leak raw response
    fragments), so it degrades to an empty string.
    """
    if not message:
        return ""
    stripped = message.strip()
    if not stripped.startswith(("{", "[")):
        return ""
    try:
        payload = json.loads(stripped)
    except ValueError:
        return ""
    return json.dumps(_redact_keys(payload), ensure_ascii=False)


def _redact_keys(value: Any) -> Any:
    """Recursively drop keys that are sensitive by name (key allowlist)."""
    if isinstance(value, dict):
        return {
            key: _redact_keys(item)
            for key, item in value.items()
            if key.lower() not in _PROVIDER_MESSAGE_SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_keys(item) for item in value]
    return value


class AIGateway:
    """Schema-checking, safety-checking provider gateway."""

    def __init__(
        self,
        provider: AIProvider | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        # Resolve the provider from settings when none is explicitly supplied,
        # instead of hard-coding "mock".  In production a mock provider is a
        # deployment misconfiguration: ``settings.ai_provider`` is constrained
        # to "mock" in phase 1, so warn loudly rather than silently falling back
        # to a stub that returns deterministic fixtures.
        provider_name = settings.ai_provider
        self._provider = provider or get_provider(provider_name)
        if settings.environment == "production" and provider_name == "mock":
            logger.warning(
                "ai_gateway_mock_provider_in_production "
                "AIGateway 在生产环境使用 mock provider，请配置真实 AI provider"
            )
        self._timeout_seconds = timeout_seconds
        # Token usage / cost hooks; phase 1 mock reports none.
        self._cost_hook: Any | None = None

    def set_provider(self, provider: AIProvider) -> None:
        """Swap the provider at runtime (used by tests and future config)."""
        self._provider = provider

    async def invoke(
        self,
        context: AITaskContext,
        method: str,
        *args: Any,
        response_type: type[T] | None = None,
    ) -> InvokeOutcome[T]:
        """Run one provider call and normalise the outcome.

        ``method`` must be one of ``structured_extract``, ``parse_search_query``
        or ``moderate_text``.  The provider's typed result is validated with
        ``response_type`` when provided, turning schema violations into a
        non-retryable ``AI_INPUT_INVALID``.
        """
        started = time.monotonic()
        try:
            handler = getattr(self._provider, method)
            raw_result = await handler(*args)
            record = self._record(
                context, method, started, error_code=None, succeeded=True
            )
            if response_type is not None:
                if not isinstance(raw_result, BaseModel):
                    raise ProviderError(
                        code=_SCHEMA_VIOLATION_CODE,
                        message="provider 返回类型必须经过 Pydantic 验证",
                        kind=ProviderErrorKind.NON_RETRYABLE,
                    )
                raw_result = response_type.model_validate(raw_result.model_dump())
            await self._log_audit(record)
            return InvokeOutcome(result=raw_result)
        except ProviderError as exc:
            # The provider's raw message only ever reaches the debug log, and
            # only after key-level redaction; the outward error message is
            # always fixed safe copy derived from the stable error code.
            detail = _redact_provider_message(exc.message)
            if detail:
                logger.debug(
                    "ai_gateway_provider_error method=%s request_id=%s "
                    "code=%s detail=%s",
                    method,
                    context.request_id,
                    exc.code,
                    detail,
                )
            record = self._record(
                context, method, started, error_code=exc.code, succeeded=False
            )
            await self._log_audit(record)
            return InvokeOutcome(
                error_code=exc.code,
                error_message=_safe_error_message(exc.code),
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
            )
        except (ValidationError, ValueError):
            # Schema violation or invalid field value: never retry.
            record = self._record(
                context, method, started, error_code=_SCHEMA_VIOLATION_CODE,
                succeeded=False,
            )
            await self._log_audit(record)
            return InvokeOutcome(
                error_code=_SCHEMA_VIOLATION_CODE,
                error_message=_safe_error_message(_SCHEMA_VIOLATION_CODE),
                retryable=False,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            # Network / IO failures are genuinely transient: retryable.
            logger.warning(
                "ai_gateway_retryable_failure method=%s request_id=%s err=%s",
                method,
                context.request_id,
                type(exc).__name__,
            )
            record = self._record(
                context, method, started,
                error_code="AI_TEMPORARILY_UNAVAILABLE", succeeded=False,
            )
            await self._log_audit(record)
            return InvokeOutcome(
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                error_message=_safe_error_message("AI_TEMPORARILY_UNAVAILABLE"),
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 - boundary conversion
            # Programming errors (TypeError/AttributeError/KeyError/etc.) are
            # not transient: retrying would just reproduce the same fault.  Mark
            # them non-retryable so callers fail fast instead of looping.
            logger.warning(
                "ai_gateway_unhandled method=%s request_id=%s err=%s",
                method,
                context.request_id,
                type(exc).__name__,
            )
            record = self._record(
                context, method, started,
                error_code="AI_TEMPORARILY_UNAVAILABLE", succeeded=False,
            )
            await self._log_audit(record)
            return InvokeOutcome(
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                error_message=_safe_error_message("AI_TEMPORARILY_UNAVAILABLE"),
                retryable=False,
            )

    def _record(
        self,
        context: AITaskContext,
        method: str,
        started: float,
        error_code: str | None,
        succeeded: bool,
    ) -> GatewayCallRecord:
        duration_ms = int((time.monotonic() - started) * 1000)
        return GatewayCallRecord(
            request_id=context.request_id or uuid.uuid4().hex,
            task_id=context.task_id,
            scene=context.scene,
            provider=context.provider,
            model=context.model,
            prompt_version=context.prompt_version,
            schema_version=context.schema_version,
            duration_ms=duration_ms,
            token_usage=None,
            error_code=error_code,
            succeeded=succeeded,
            input_revision=dict(context.input_revision),
            policy_revision=context.policy_revision,
        )

    async def _log_audit(self, record: GatewayCallRecord) -> None:
        """Log and persist minimal metadata; never payloads or secrets.

        ``record_generation_audit`` is async because the underlying DB write is
        offloaded to a worker thread; awaiting here keeps the event loop free
        while the audit row is persisted.
        """
        logger.info(
            "ai_generation request_id=%s task_id=%s scene=%s provider=%s "
            "model=%s prompt_version=%s schema_version=%s duration_ms=%d "
            "error=%s",
            record.request_id,
            record.task_id,
            record.scene,
            record.provider,
            record.model,
            record.prompt_version,
            record.schema_version,
            record.duration_ms,
            record.error_code,
        )
        await record_generation_audit(
            GenerationAuditEvent(
                request_id=record.request_id,
                task_id=record.task_id,
                scene=record.scene,
                provider=record.provider,
                model=record.model,
                prompt_version=record.prompt_version,
                schema_version=record.schema_version,
                input_revision=record.input_revision,
                policy_revision=record.policy_revision,
                status="succeeded" if record.succeeded else "failed",
                error_code=record.error_code,
                usage_cost=record.token_usage,
                display_eligible=False,
                duration_ms=record.duration_ms,
            )
        )

    # ------------------------------------------------------------------
    # Typed convenience methods so business modules never call raw methods.
    # ------------------------------------------------------------------
    async def structured_extract(
        self, context: AITaskContext, request: Any
    ) -> InvokeOutcome[StructuredExtractResult]:
        return await self.invoke(
            context, "structured_extract", request,
            response_type=StructuredExtractResult,
        )

    async def parse_search_query(
        self, context: AITaskContext, request: Any
    ) -> InvokeOutcome[SearchParseResult]:
        return await self.invoke(
            context, "parse_search_query", request,
            response_type=SearchParseResult,
        )

    async def moderate_text(
        self, context: AITaskContext, request: Any
    ) -> InvokeOutcome[ModerationResult]:
        return await self.invoke(
            context, "moderate_text", request,
            response_type=ModerationResult,
        )
