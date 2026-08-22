"""Voice Gateway: schema validation, safety checks and audit boundary.

Parallel to ``app.services.ai.gateway.AIGateway``.  The VoiceGateway is the
only path through which a voice provider is called.  It performs schema
validation on every provider response, classifies failures as retryable or
not, and produces a minimal ``GatewayCallRecord`` for the audit trail.

Audio content, transcripts and secrets never cross the audit/log boundary:
``GatewayCallRecord`` only exposes metadata, never ``text``, ``audio_url``,
raw responses or secrets.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.services.ai.audit import GenerationAuditEvent, record_generation_audit
from app.services.ai.base import (
    AITaskContext,
    GatewayCallRecord,
    ProviderError,
    ProviderErrorKind,
)
from app.services.voice.base import (
    StreamTranscribeRequest,
    SynthesizeResult,
    TranscribeResult,
    VoiceProvider,
)
from app.services.voice.providers import get_voice_provider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class VoiceInvokeOutcome(Generic[T]):
    """Typed outcome of one VoiceGateway invocation."""

    result: T | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    retry_after_ms: int = 0


_SCHEMA_VIOLATION_CODE = "AI_INPUT_INVALID"
_POLICY_DENIED_CODE = "AI_POLICY_DENIED"

# Safe copy for every stable error code the Gateway can emit.  The outward
# ``error_message`` is always drawn from this mapping (or a generic fallback);
# a provider's raw ``ProviderError.message`` never reaches it.
_SAFE_ERROR_MESSAGES: dict[str, str] = {
    "AI_INPUT_INVALID": "语音服务输入或输出未通过校验",
    "AI_POLICY_DENIED": "请求未通过语音服务安全与策略校验",
    "AI_QUOTA_EXCEEDED": "语音服务请求频率过高，请稍后重试",
    "AI_TEMPORARILY_UNAVAILABLE": "语音服务暂时不可用",
}
_DEFAULT_SAFE_ERROR_MESSAGE = "语音服务调用失败"


def _safe_error_message(code: str) -> str:
    """Map a stable error code to fixed safe copy; never provider text."""
    return _SAFE_ERROR_MESSAGES.get(code, _DEFAULT_SAFE_ERROR_MESSAGE)


class VoiceGateway:
    """Schema-checking, safety-checking voice provider gateway.

    Mirrors ``AIGateway``: the provider is resolved from settings, schema
    validation turns provider output violations into a non-retryable
    ``AI_INPUT_INVALID``, and ``ProviderError`` is mapped to fixed safe copy.
    """

    def __init__(
        self,
        provider: VoiceProvider | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        provider_name = settings.ai_voice_provider
        self._provider = provider or get_voice_provider(provider_name)
        self._timeout_seconds = timeout_seconds

    def set_provider(self, provider: VoiceProvider) -> None:
        """Swap the provider at runtime (used by tests and future config)."""
        self._provider = provider

    async def invoke(
        self,
        context: AITaskContext,
        method: str,
        *args: Any,
        response_type: type[T] | None = None,
    ) -> VoiceInvokeOutcome[T]:
        """Run one voice provider call and normalise the outcome.

        ``method`` must be ``transcribe`` or ``synthesize``.  The provider's
        typed result is validated with ``response_type`` when provided, turning
        schema violations into a non-retryable ``AI_INPUT_INVALID``.
        """
        started = time.monotonic()
        try:
            handler = getattr(self._provider, method)
            raw_result = await handler(*args)
            # provider 返回 None 表示异常（schema 违例或内部错误），让 Gateway 拒收。
            if raw_result is None:
                raise ProviderError(
                    code=_SCHEMA_VIOLATION_CODE,
                    message="voice provider 返回 None（内部错误或 schema 违例）",
                    kind=ProviderErrorKind.NON_RETRYABLE,
                )
            record = self._record(
                context, method, started, error_code=None, succeeded=True
            )
            if response_type is not None:
                if not isinstance(raw_result, BaseModel):
                    raise ProviderError(
                        code=_SCHEMA_VIOLATION_CODE,
                        message="voice provider 返回类型必须经过 Pydantic 验证",
                        kind=ProviderErrorKind.NON_RETRYABLE,
                    )
                raw_result = response_type.model_validate(raw_result.model_dump())
            await self._log_audit(record)
            return VoiceInvokeOutcome(result=raw_result)
        except ProviderError as exc:
            logger.debug(
                "voice_gateway_provider_error method=%s request_id=%s code=%s",
                method,
                context.request_id,
                exc.code,
            )
            record = self._record(
                context, method, started, error_code=exc.code, succeeded=False
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
                error_code=exc.code,
                error_message=_safe_error_message(exc.code),
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
            )
        except (ValidationError, ValueError):
            record = self._record(
                context, method, started, error_code=_SCHEMA_VIOLATION_CODE,
                succeeded=False,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
                error_code=_SCHEMA_VIOLATION_CODE,
                error_message=_safe_error_message(_SCHEMA_VIOLATION_CODE),
                retryable=False,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning(
                "voice_gateway_retryable_failure method=%s request_id=%s err=%s",
                method,
                context.request_id,
                type(exc).__name__,
            )
            record = self._record(
                context, method, started,
                error_code="AI_TEMPORARILY_UNAVAILABLE", succeeded=False,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                error_message=_safe_error_message("AI_TEMPORARILY_UNAVAILABLE"),
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 - boundary conversion
            logger.warning(
                "voice_gateway_unhandled method=%s request_id=%s err=%s",
                method,
                context.request_id,
                type(exc).__name__,
            )
            record = self._record(
                context, method, started,
                error_code="AI_TEMPORARILY_UNAVAILABLE", succeeded=False,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
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
        """Log and persist minimal metadata; never payloads or secrets."""
        logger.info(
            "voice_generation request_id=%s task_id=%s scene=%s provider=%s "
            "model=%s duration_ms=%d error=%s",
            record.request_id,
            record.task_id,
            record.scene,
            record.provider,
            record.model,
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
    async def transcribe(
        self, context: AITaskContext, request: Any
    ) -> VoiceInvokeOutcome[TranscribeResult]:
        return await self.invoke(
            context, "transcribe", request,
            response_type=TranscribeResult,
        )

    async def synthesize(
        self, context: AITaskContext, request: Any
    ) -> VoiceInvokeOutcome[SynthesizeResult]:
        return await self.invoke(
            context, "synthesize", request,
            response_type=SynthesizeResult,
        )

    async def stream_transcribe(
        self,
        context: AITaskContext,
        request: StreamTranscribeRequest,
        on_partial: Any = None,
    ) -> VoiceInvokeOutcome[Any]:
        """启动一次实时流式 ASR 会话，返回 provider 的 client/result。

        与 ``transcribe``/``synthesize`` 不同：流式识别跨多次 ``send_chunk``，
        无法在单一 ``invoke`` 边界内完成 schema 校验。这里只对"创建 stream
        client"这一步做审计，部分结果与最终文本的校验由 WS 路由层负责。
        """
        started = time.monotonic()
        try:
            handler = getattr(self._provider, "stream_transcribe")
            raw_result = await handler(request, on_partial)
            record = self._record(
                context, "stream_transcribe", started,
                error_code=None, succeeded=True,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(result=raw_result)
        except ProviderError as exc:
            record = self._record(
                context, "stream_transcribe", started,
                error_code=exc.code, succeeded=False,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
                error_code=exc.code,
                error_message=_safe_error_message(exc.code),
                retryable=exc.retryable,
                retry_after_ms=exc.retry_after_ms,
            )
        except Exception as exc:  # noqa: BLE001 - boundary conversion
            logger.warning(
                "voice_gateway_stream_unhandled method=stream_transcribe "
                "request_id=%s err=%s",
                context.request_id,
                type(exc).__name__,
            )
            record = self._record(
                context, "stream_transcribe", started,
                error_code="AI_TEMPORARILY_UNAVAILABLE", succeeded=False,
            )
            await self._log_audit(record)
            return VoiceInvokeOutcome(
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                error_message=_safe_error_message("AI_TEMPORARILY_UNAVAILABLE"),
                retryable=False,
            )


__all__ = ["VoiceGateway", "VoiceInvokeOutcome"]
