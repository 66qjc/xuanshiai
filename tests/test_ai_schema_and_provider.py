"""AI-CORE Task 5 acceptance contract: tables, gates, typed mock provider.

The three acceptance tests mirror the task brief Step 1 verbatim; the remaining
tests pin the additional requirements (Protocol methods, failure injection,
schema validation through the Gateway, stable error shapes).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.db.ai_schema import AI_TABLES
from app.schemas.ai_common import AiErrorResponse, AiTaskStatus
from app.services.ai.base import (
    AITaskContext,
    ModerationRequest,
    ModerationResult,
    SearchParseRequest,
    SearchParseResult,
    StructuredExtractRequest,
    StructuredExtractResult,
)
from app.services.ai.gateway import AIGateway
from app.services.ai.providers import MockAIProvider


def test_ai_schema_contains_the_registered_fact_tables() -> None:
    assert set(AI_TABLES) == {
        "ai_consent_grant", "ai_task", "ai_generation_audit",
        "ai_profile_session", "ai_profile_turn", "ai_profile_draft",
        "ai_profile_draft_field", "ai_profile_revision",
        "ai_profile_revision_field", "ai_profile_summary",
        "ai_search_draft", "ai_search_condition", "ai_search_snapshot",
        "ai_search_result", "ai_feature_projection", "ai_compatibility_snapshot",
        "voice_transcript",
    }


def test_production_cannot_enable_ai_without_all_approvals() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", auto_init_db=False, ai_master_enabled=True)


@pytest.mark.asyncio
async def test_mock_provider_returns_typed_draft_only() -> None:
    result = await MockAIProvider().structured_extract_fixture("profile-interest-v1")
    assert result.fields[0].confirmation_status == "suggested"
    assert result.fields[0].field_key == "interest_tags"


# ----------------------------------------------------------------------
# Settings gate details
# ----------------------------------------------------------------------


def test_production_ai_gate_fails_independently_of_wechat_mock() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            auto_init_db=False,
            wechat_payment_mode="real",
            ai_master_enabled=True,
        )


def test_production_ai_still_rejects_mock_provider_when_approvals_present() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            auto_init_db=False,
            wechat_payment_mode="real",
            ai_master_enabled=True,
            ai_policy_approved=True,
            ai_provider_approved=True,
            ai_retention_policy_version="retention-v1",
        )


def test_development_allows_mock_ai_with_disabled_defaults() -> None:
    configured = Settings(
        _env_file=None,
        environment="development",
        ai_master_enabled=True,
        ai_profile_enabled=True,
        ai_search_enabled=False,
    )

    assert configured.ai_provider == "mock"
    assert configured.ai_profile_enabled is True
    assert configured.ai_search_enabled is False
    assert configured.ai_retention_policy_version is None


# ----------------------------------------------------------------------
# Protocol contract and deterministic providers
# ----------------------------------------------------------------------


def test_mock_provider_implements_the_ai_provider_protocol() -> None:
    provider = MockAIProvider()

    for method in ("structured_extract", "parse_search_query", "moderate_text"):
        assert callable(getattr(provider, method, None))


@pytest.mark.asyncio
async def test_mock_provider_protocol_methods_return_typed_results() -> None:
    provider = MockAIProvider()

    extract = await provider.structured_extract(
        StructuredExtractRequest(
            subject="personal",
            turn_texts=("周末喜欢旅行和看展",),
            consent_version="profile-text-v1",
            policy_revision="ai-policy-2026-08-07-v1",
        )
    )
    assert isinstance(extract, StructuredExtractResult)
    assert extract.fields[0].confirmation_status == "suggested"

    parsed = await provider.parse_search_query(
        SearchParseRequest(query_text="想找26到32岁、住杭州、本科以上、周末愿意户外的人")
    )
    assert isinstance(parsed, SearchParseResult)
    assert parsed.conditions[0].field_key == "age"
    assert parsed.conditions[0].operator == "between"

    moderated = await provider.moderate_text(ModerationRequest(text="正常的自我介绍"))
    assert isinstance(moderated, ModerationResult)
    assert moderated.allowed is True


@pytest.mark.asyncio
async def test_mock_provider_blocks_sensitive_moderation_text() -> None:
    provider = MockAIProvider()

    moderated = await provider.moderate_text(
        ModerationRequest(text="可以加微信吗")
    )

    assert moderated.allowed is False
    assert moderated.action == "reject"


# ----------------------------------------------------------------------
# Failure injection and Gateway schema validation
# ----------------------------------------------------------------------


def _context(request_id: str = "req_test") -> AITaskContext:
    return AITaskContext(
        task_id="at_test",
        request_id=request_id,
        scene="profile_extract",
        provider="mock",
        model="mock-model-v1",
        prompt_version="profile-extract-prompt-v1",
        schema_version="profile-extract-v1",
    )


@pytest.mark.asyncio
async def test_mock_provider_injects_retryable_timeout_failure() -> None:
    provider = MockAIProvider(failures=["timeout"])
    gateway = AIGateway(provider=provider)
    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    outcome = await gateway.structured_extract(_context(), request)

    assert outcome.retryable is True
    assert outcome.error_code == "AI_TEMPORARILY_UNAVAILABLE"
    assert outcome.result is None


@pytest.mark.asyncio
async def test_mock_provider_injects_retryable_429_failure() -> None:
    provider = MockAIProvider(failures=["http_429"])
    gateway = AIGateway(provider=provider)
    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    outcome = await gateway.structured_extract(_context(), request)

    assert outcome.retryable is True
    assert outcome.error_code == "AI_QUOTA_EXCEEDED"
    assert outcome.retry_after_ms == 2000


@pytest.mark.asyncio
async def test_schema_invalid_result_is_non_retryable_input_error() -> None:
    provider = MockAIProvider(failures=["schema_invalid"])
    gateway = AIGateway(provider=provider)
    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    outcome = await gateway.structured_extract(_context(), request)

    assert outcome.retryable is False
    assert outcome.error_code == "AI_INPUT_INVALID"
    assert outcome.result is None


@pytest.mark.asyncio
async def test_policy_blocked_failure_is_non_retryable() -> None:
    provider = MockAIProvider(failures=["policy_blocked"])
    gateway = AIGateway(provider=provider)
    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    outcome = await gateway.structured_extract(_context(), request)

    assert outcome.retryable is False
    assert outcome.error_code == "AI_POLICY_DENIED"


@pytest.mark.asyncio
async def test_gateway_success_returns_validated_typed_result() -> None:
    provider = MockAIProvider()
    gateway = AIGateway(provider=provider)
    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    outcome = await gateway.structured_extract(_context(), request)

    assert outcome.result is not None
    assert isinstance(outcome.result, StructuredExtractResult)
    assert outcome.result.fields[0].field_key == "interest_tags"
    assert outcome.error_code is None


# ----------------------------------------------------------------------
# Stable enums and error shape
# ----------------------------------------------------------------------


def test_ai_task_status_has_the_eight_frozen_states() -> None:
    assert {status.value for status in AiTaskStatus} == {
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "superseded",
    }


def test_ai_error_response_shape_aligns_with_unified_plan() -> None:
    error = AiErrorResponse(
        code="AI_TEMPORARILY_UNAVAILABLE",
        message="AI 服务暂时不可用，请稍后重试",
        request_id="req_01J",
        retryable=True,
        retry_after_ms=2000,
    )

    assert error.retryable is True
    assert error.retry_after_ms == 2000
    dumped = error.model_dump()
    assert set(dumped) == {
        "code",
        "message",
        "request_id",
        "retryable",
        "retry_after_ms",
    }
