"""Task6 G3-A 加法兼容契约测试（TDD 结构，本轮不运行）。

覆盖：
- ``ProfileSessionRead`` 含可空 ``draft_id``（加法字段）。
- ``ProfileQuestion`` 含稳定 ``field_key``（加法字段）。
- ``AiErrorDetail`` envelope 字段：``code/message/request_id/retryable/retry_after_ms``。
- publish 路由 ``expected_revision`` 作为 query 参数（非 body）。
- ``Idempotency-Key`` 在 OpenAPI 中为 required header。

这些断言是结构契约门禁，确保 schema/路由改动不回退加法字段。真实运行由
主控在通过 G2 安全门禁后统一执行。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.routes.ai_profile import router as profile_router
from app.schemas.ai_common import AiErrorDetail, AiErrorResponse
from app.schemas.ai_profile import (
    ProfileProgress,
    ProfileQuestion,
    ProfileSessionRead,
    ProfileSessionStatus,
    ProfileSubject,
)


def test_profile_question_carries_stable_field_key() -> None:
    """Task6 Step2：ProfileQuestion 必须含稳定 field_key（加法字段）。"""
    question = ProfileQuestion(
        id="interest_lifestyle_v1",
        text="最近让你投入的事情是什么？",
        field_key="interest_tags",
    )
    assert question.field_key == "interest_tags"


def test_profile_question_rejects_missing_field_key() -> None:
    """field_key 是必填字段，缺失应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        ProfileQuestion(id="age_v1", text="你今年多大了？")  # type: ignore[call-arg]


def test_profile_session_read_has_nullable_draft_id() -> None:
    """Task6 Step2：ProfileSessionRead 必须含可空 draft_id（加法字段）。"""
    session = ProfileSessionRead(
        session_id="abc123",
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        input_mode="text",
        progress=ProfileProgress(basis="confirmed_field_coverage", value=0.0),
        current_question=None,
        draft_id=None,
        profile_revision=0,
        preference_revision=0,
        expires_at=None,
        created_at=datetime(2026, 8, 15, 0, 0, 0),
    )
    assert session.draft_id is None
    # 加法字段：有活动草稿时透传 draft_id
    session_with_draft = session.model_copy(update={"draft_id": "dr_abc"})
    assert session_with_draft.draft_id == "dr_abc"


def test_profile_session_read_preserves_existing_fields() -> None:
    """加法兼容：原有字段不丢失。"""
    session = ProfileSessionRead(
        session_id="abc123",
        subject=ProfileSubject.IDEAL_PARTNER,
        status=ProfileSessionStatus.AWAITING_CONFIRMATION,
        input_mode="text",
        progress=ProfileProgress(basis="confirmed_field_coverage", value=0.2),
        current_question={"id": "q1", "text": "问题", "field_key": "age"},
        draft_id="dr_def",
        profile_revision=3,
        preference_revision=1,
        expires_at=datetime(2026, 8, 20, 0, 0, 0),
        created_at=datetime(2026, 8, 15, 0, 0, 0),
    )
    assert session.subject == ProfileSubject.IDEAL_PARTNER
    assert session.status == ProfileSessionStatus.AWAITING_CONFIRMATION
    assert session.current_question is not None
    assert session.current_question["field_key"] == "age"
    assert session.draft_id == "dr_def"


def test_ai_error_detail_envelope_has_all_required_fields() -> None:
    """Task6 Step3：AiErrorDetail envelope 必须含 code/message/request_id/
    retryable/retry_after_ms。"""
    envelope = AiErrorDetail(
        detail=AiErrorResponse(
            code="AI_INPUT_INVALID",
            message="publish 必须携带 expected_revision 查询参数",
            request_id="req_abc",
            retryable=False,
            retry_after_ms=0,
        )
    )
    inner = envelope.detail
    assert inner.code == "AI_INPUT_INVALID"
    assert inner.message == "publish 必须携带 expected_revision 查询参数"
    assert inner.request_id == "req_abc"
    assert inner.retryable is False
    assert inner.retry_after_ms == 0


def _find_route(path: str) -> APIRoute:
    for route in profile_router.routes:
        if getattr(route, "path", "") == path:
            return route  # type: ignore[return-value]
    raise AssertionError(f"route {path} not found in profile_router")


def test_publish_route_expected_revision_is_query_parameter() -> None:
    """Task6 Step3：publish 的 expected_revision 必须是 query 参数。"""
    route = _find_route("/profile-drafts/{draft_id}/publish")
    param_names = {p.name for p in route.dependant.query_params}
    assert "expected_revision" in param_names
    # body 参数不应包含 expected_revision（那是 draft PATCH 的字段）
    body_param_names = {p.name for p in route.dependant.body_params}
    assert "expected_revision" not in body_param_names


def test_publish_route_idempotency_key_declared_in_openapi() -> None:
    """Task6 Step3：publish 路由 OpenAPI 显式声明 Idempotency-Key 为 required header。"""
    route = _find_route("/profile-drafts/{draft_id}/publish")
    openapi_extra = getattr(route, "openapi_extra", None) or {}
    parameters = openapi_extra.get("parameters", [])
    idem_header = next(
        (p for p in parameters if p.get("name") == "Idempotency-Key"), None
    )
    assert idem_header is not None, "publish 路由必须在 openapi_extra 声明 Idempotency-Key"
    assert idem_header.get("in") == "header"
    assert idem_header.get("required") is True


def test_publish_route_expected_revision_declared_required_in_openapi() -> None:
    """Task6 Step3：publish 路由 OpenAPI 显式声明 expected_revision 为 required query。"""
    route = _find_route("/profile-drafts/{draft_id}/publish")
    openapi_extra = getattr(route, "openapi_extra", None) or {}
    parameters = openapi_extra.get("parameters", [])
    rev_param = next(
        (p for p in parameters if p.get("name") == "expected_revision"), None
    )
    assert rev_param is not None, "publish 路由必须在 openapi_extra 声明 expected_revision"
    assert rev_param.get("in") == "query"
    assert rev_param.get("required") is True


def test_question_bank_field_keys_are_within_allowlist() -> None:
    """Task6 Step2：question bank 的 field_key 必须属于 AI_FIELD_ALLOWLIST。"""
    from app.schemas.ai_common import AI_FIELD_ALLOWLIST
    from app.services.ai.profile import _PROFILE_QUESTION_BANK

    for field_key, question in _PROFILE_QUESTION_BANK.items():
        assert question.field_key == field_key, (
            f"question bank key {field_key} 与 field_key 不匹配"
        )
        assert field_key in AI_FIELD_ALLOWLIST, (
            f"question bank field_key {field_key} 不在 AI_FIELD_ALLOWLIST"
        )
