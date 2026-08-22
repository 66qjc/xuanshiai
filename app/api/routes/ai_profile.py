"""M04 AI 画像路由（统一方案 §7.5/§7.6，执行计划 §3.2）。

前缀 `/api/v1/ai`（由 ``app/api/router.py`` 注册），共 13 个路径：

- ``POST /profile-sessions``：201 创建/复用会话（要求 profile_text_extract 授权）
- ``GET /profile-sessions/{session_id}``：200 仅本人
- ``POST /profile-sessions/{session_id}/turns``：202 turn+task
- ``POST /profile-sessions/{session_id}/pause`` / ``resume``：200 会话状态
- ``DELETE /profile-sessions/{session_id}``：202 cleanup task（软删除幂等）
- ``GET /profile-drafts/{draft_id}``：200 字段草稿（仅本人）
- ``PATCH /profile-drafts/{draft_id}``：200 新草稿 revision（乐观锁）
- ``POST /profile-drafts/{draft_id}/publish``：202 publish task（confirmed-only）
- ``GET /profile-revisions``：200 游标历史（仅本人，只读）
- ``POST /profile-revisions/{revision_id}/restore``：201 新 draft（旧行只读）
- ``DELETE /profiles/{subject}``：202 cleanup task（同步隐藏 + 异步清理）
- ``DELETE /profiles/{subject}/fields/{field_key}``：202 invalidation task

所有写操作要求 ``Idempotency-Key`` header；错误统一为 ``AiErrorDetail`` 形状并
携带 request_id。普通响应不携带原文、provider trace 或密钥。
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse
from app.schemas.ai_profile import (
    CleanupTaskAccepted,
    ProfileDraftPatchRequest,
    ProfileDraftRead,
    ProfileNarrativeRead,
    ProfileProgress,
    ProfilePublishAccepted,
    ProfileRevisionPage,
    ProfileSessionCreateRequest,
    ProfileSessionRead,
    ProfileSubject,
    ProfileTurnCreateRequest,
    ProfileTurnSubmissionRead,
)
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.profile import (
    AIConsentRequired,
    AIInputError,
    DraftStatusConflict,
    DraftVersionConflict,
    ProfileDraft,
    ProfileDraftNotFound,
    ProfileRevisionNotFound,
    ProfileSession,
    ProfileSessionNotFound,
    ProfileSessionStale,
    confirm_profile_draft,
    create_profile_session,
    delete_ai_profile,
    delete_ai_profile_field,
    delete_profile_session,
    list_profile_revisions,
    load_owned_draft,
    load_owned_session,
    load_published_narrative,
    pause_profile_session,
    progress_value,
    publish_profile_draft,
    restore_profile_revision,
    resume_profile_session,
    submit_profile_turn,
)
from app.services.ai.tasks import TaskError

router = APIRouter()

# Idempotency-Key 契约（§7.5）：8-128 位 ASCII，禁止空白。
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _request_id() -> str:
    supplied = request_id_context.get()
    if supplied and supplied != "-":
        return supplied
    return uuid4().hex


def _error_response(
    code: str, message: str, status_code: int, *, retryable: bool = False
) -> HTTPException:
    detail = AiErrorResponse(
        code=code,
        message=message,
        request_id=_request_id(),
        retryable=retryable,
        retry_after_ms=0,
    )
    return HTTPException(status_code=status_code, detail=detail.model_dump())


def _require_profile_feature() -> None:
    try:
        require_ai_feature(AiFeature.PROFILE, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "AI 画像功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


def _check_idempotency_key(idempotency_key: str | None) -> None:
    if not idempotency_key or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise _error_response(
            "AI_INPUT_INVALID",
            "Idempotency-Key 必须为 8-128 位 ASCII 字符",
            status.HTTP_400_BAD_REQUEST,
        )


def _to_session_read(session: ProfileSession) -> ProfileSessionRead:
    # Task6 Step2：current_question 透传稳定 field_key（加法，保留 id/text）；
    # draft_id 透传活动草稿 ID（无活动草稿为 None）。两者均为加法字段，旧客户端
    # 忽略新字段不受影响。
    current_question: dict[str, str] | None = None
    if session.current_question is not None:
        current_question = {
            "id": session.current_question.id,
            "text": session.current_question.text,
            "field_key": session.current_question.field_key,
        }
    return ProfileSessionRead(
        session_id=session.session_id,
        subject=session.subject,
        status=session.status,
        input_mode=session.input_mode,
        progress=ProfileProgress(
            basis="confirmed_field_coverage",
            value=progress_value(session.confirmed_keys),
        ),
        current_question=current_question,
        draft_id=session.draft_id,
        profile_revision=session.profile_revision,
        preference_revision=session.preference_revision,
        expires_at=session.expires_at,
        created_at=session.created_at,
    )


def _to_draft_read(draft: ProfileDraft) -> ProfileDraftRead:
    from app.schemas.ai_profile import (
        ProfileDraftFieldRead,
        ProfileFieldConfirmationStatus,
    )

    return ProfileDraftRead(
        draft_id=draft.draft_id,
        subject=ProfileSubject(draft.subject),
        status=draft.status,
        expected_revision=draft.revision,
        policy_revision=draft.policy_revision,
        schema_version=draft.schema_version,
        fields=[
            ProfileDraftFieldRead(
                field_key=field.field_key,
                subject=ProfileSubject(field.subject),
                value=field.value,
                display_value=field.display_value,
                source_quote=field.source_span,
                confidence=field.confidence,
                needs_confirmation=(
                    field.confirmation_status
                    != ProfileFieldConfirmationStatus.CONFIRMED.value
                ),
                confirmation_status=ProfileFieldConfirmationStatus(
                    field.confirmation_status
                ),
                content_hash=field.content_hash,
            )
            for field in draft.fields
        ],
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


@router.post(
    "/profile-sessions",
    response_model=ProfileSessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建或复用 AI 画像文字会话",
)
async def create_profile_session_route(
    body: ProfileSessionCreateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileSessionRead:
    """Create or reuse the single active session for user+subject."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        session = await create_profile_session(
            db, current.id, body.subject, body.consent_version, idempotency_key
        )
    except AIConsentRequired as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code
        ) from exc
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileSessionStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return _to_session_read(session)


@router.get(
    "/profile-sessions/{session_id}",
    response_model=ProfileSessionRead,
    status_code=status.HTTP_200_OK,
    summary="查询本人的 AI 画像会话",
)
async def get_profile_session_route(
    session_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileSessionRead:
    """Return the session for the owner only; foreign/missing is a uniform 404."""
    _require_profile_feature()
    try:
        session = await load_owned_session(db, session_id, current.id)
    except ProfileSessionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    return _to_session_read(session)


@router.post(
    "/profile-sessions/{session_id}/turns",
    response_model=ProfileTurnSubmissionRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交一条文字回答并创建 profile_extract 任务",
)
async def submit_profile_turn_route(
    session_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    body: ProfileTurnCreateRequest = Body(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileTurnSubmissionRead:
    """Save the original answer first, then enqueue extraction (idempotent)."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        submission = await submit_profile_turn(
            db,
            session_id,
            current.id,
            body.client_turn_id,
            body.answer_text,
            idempotency_key,
        )
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileSessionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileSessionStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except AIConsentRequired as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return ProfileTurnSubmissionRead(
        turn_id=submission.turn_id,
        session_id=submission.session_id,
        client_turn_id=submission.client_turn_id,
        turn_no=submission.turn_no,
        role="user",
        status="saved",
        replayed=submission.replayed,
        task_id=submission.task_id,
        task_status=submission.task_status,
        stage=submission.stage,
        poll_after_ms=submission.poll_after_ms,
        expires_at=submission.expires_at,
    )


@router.post(
    "/profile-sessions/{session_id}/pause",
    response_model=ProfileSessionRead,
    status_code=status.HTTP_200_OK,
    summary="暂停 AI 画像会话",
)
async def pause_profile_session_route(
    session_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileSessionRead:
    """Pause only draft/extracting/awaiting_confirmation; repeats return current."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        session = await pause_profile_session(db, session_id, current.id)
    except ProfileSessionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileSessionStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    await db.commit()
    return _to_session_read(session)


@router.post(
    "/profile-sessions/{session_id}/resume",
    response_model=ProfileSessionRead,
    status_code=status.HTTP_200_OK,
    summary="恢复 AI 画像会话",
)
async def resume_profile_session_route(
    session_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileSessionRead:
    """Resume non stale/cancelled sessions; expired sessions return 409."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        session = await resume_profile_session(db, session_id, current.id)
    except ProfileSessionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileSessionStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    await db.commit()
    return _to_session_read(session)


@router.delete(
    "/profile-sessions/{session_id}",
    response_model=CleanupTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="软删除 AI 画像会话并创建清理任务",
)
async def delete_profile_session_route(
    session_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CleanupTaskAccepted:
    """Soft-delete the session; published revisions are never implicitly deleted."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        submission = await delete_profile_session(db, session_id, current.id, idempotency_key)
    except ProfileSessionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return CleanupTaskAccepted(
        task_id=submission.task_id,
        status=submission.status,
        cleanup_requested=True,
    )


# ----------------------------------------------------------------------
# Task 8：草稿确认、发布、历史与删除传播
# ----------------------------------------------------------------------


@router.get(
    "/profile-drafts/{draft_id}",
    response_model=ProfileDraftRead,
    status_code=status.HTTP_200_OK,
    summary="查询本人的 AI 画像字段草稿",
)
async def get_profile_draft_route(
    draft_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileDraftRead:
    """Return the draft for the owner only; missing/foreign is a uniform 404."""
    _require_profile_feature()
    try:
        draft = await load_owned_draft(db, draft_id, current.id)
    except ProfileDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    return _to_draft_read(draft)


@router.patch(
    "/profile-drafts/{draft_id}",
    response_model=ProfileDraftRead,
    status_code=status.HTTP_200_OK,
    summary="逐项确认/修改/拒绝/删除草稿字段",
)
async def patch_profile_draft_route(
    draft_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    body: ProfileDraftPatchRequest = Body(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileDraftRead:
    """Apply per-field confirm/replace/reject/delete under the optimistic lock."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        draft = await confirm_profile_draft(
            db,
            draft_id,
            current.id,
            body.actions,
            body.expected_revision,
            idempotency_key,
        )
    except DraftVersionConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except DraftStatusConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return _to_draft_read(draft)


@router.post(
    "/profile-drafts/{draft_id}/publish",
    response_model=ProfilePublishAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="发布已确认字段并创建投影任务",
    openapi_extra={
        # Task6 Step3：明确 expected_revision 是 query 参数（非 body），
        # Idempotency-Key 是 required header。两者均在 OpenAPI 中显式声明。
        "parameters": [
            {
                "name": "expected_revision",
                "in": "query",
                "required": True,
                "schema": {"type": "integer", "minimum": 0},
                "description": "草稿乐观锁版本；必须等于当前 draft 的 expected_revision",
            },
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"},
                "description": "幂等键；同 key 同 payload 回放同一任务",
            },
        ],
    },
)
async def publish_profile_draft_route(
    draft_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Query(default=None, ge=0, alias="expected_revision"),
) -> ProfilePublishAccepted:
    """Publish only confirmed fields into an immutable revision + projection task.

    Task6 Step3：``expected_revision`` 作为 **query 参数** 传递（非 body），
    与 draft PATCH body 内的 ``expected_revision``（逐项乐观锁）语义分离、互不冲突。
    缺失 query 参数返回 ``400 AI_INPUT_INVALID``。
    """
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    if expected_revision is None:
        raise _error_response(
            "AI_INPUT_INVALID",
            "publish 必须携带 expected_revision 查询参数",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        submission = await publish_profile_draft(
            db, draft_id, current.id, expected_revision, idempotency_key
        )
    except DraftVersionConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except DraftStatusConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except ProfileDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    revision = submission.revision
    return ProfilePublishAccepted(
        task_id=submission.task_id,
        status=submission.status,
        stage=None,
        poll_after_ms=1000 if not submission.replayed else 0,
        expires_at=None,
        replayed=submission.replayed,
        revision_id=revision.revision_id if revision else None,
        revision_no=revision.revision_no if revision else None,
        subject=ProfileSubject(revision.subject) if revision else None,
        field_count=len(revision.changed_field_keys) if revision else None,
    )


@router.get(
    "/profile-revisions",
    response_model=ProfileRevisionPage,
    status_code=status.HTTP_200_OK,
    summary="查询本人的发布版本历史（游标，只读）",
)
async def list_profile_revisions_route(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> ProfileRevisionPage:
    """Return the owner's immutable revision history; nothing is leaked."""
    _require_profile_feature()
    page = await list_profile_revisions(db, current.id, cursor, limit)
    return page


@router.post(
    "/profile-revisions/{revision_id}/restore",
    response_model=ProfileDraftRead,
    status_code=status.HTTP_201_CREATED,
    summary="从历史版本恢复为新的可编辑草稿",
)
async def restore_profile_revision_route(
    revision_id: int,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProfileDraftRead:
    """Restore a snapshot into a new draft; the old revision stays read-only."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        draft = await restore_profile_revision(
            db, revision_id, current.id, idempotency_key or ""
        )
    except ProfileRevisionNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    await db.commit()
    return _to_draft_read(draft)


@router.delete(
    "/profiles/{subject}",
    response_model=CleanupTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="删除 AI 画像并创建清理任务（同步隐藏）",
)
async def delete_ai_profile_route(
    subject: ProfileSubject,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CleanupTaskAccepted:
    """Delete one subject's AI profile: drafts/results hidden synchronously."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        task = await delete_ai_profile(db, current.id, subject, idempotency_key)
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return CleanupTaskAccepted(
        task_id=task.task_id, status=task.status, cleanup_requested=True
    )


@router.delete(
    "/profiles/{subject}/fields/{field_key}",
    response_model=CleanupTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="删除画像单个字段并创建失效任务",
)
async def delete_ai_profile_field_route(
    subject: ProfileSubject,
    field_key: str = Path(..., min_length=1, max_length=64),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CleanupTaskAccepted:
    """Hide one field synchronously and enqueue its invalidation task."""
    _require_profile_feature()
    _check_idempotency_key(idempotency_key)
    try:
        task = await delete_ai_profile_field(
            db, current.id, subject, field_key, idempotency_key
        )
    except AIInputError as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return CleanupTaskAccepted(
        task_id=task.task_id, status=task.status, cleanup_requested=True
    )


@router.get(
    "/profiles/{subject}/narrative",
    response_model=ProfileNarrativeRead,
    status_code=status.HTTP_200_OK,
    summary="获取画像叙事层（AI 人格画像成品）",
)
async def get_profile_narrative_route(
    subject: ProfileSubject,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileNarrativeRead:
    """返回用户最新发布的画像叙事层成品。

    叙事层在发布画像后由 Worker 异步生成（``profile_narrative`` 任务），
    包含人格标题、标签、AI 洞察、维度卡片、理想型权重和最近变化趋势。
    如果尚未发布或叙事层任务尚未完成，返回 ``status='pending'``。
    """
    _require_profile_feature()
    narrative = await load_published_narrative(db, current.id, subject.value)
    if narrative is None or narrative.get("data") is None:
        return ProfileNarrativeRead(
            subject=subject.value,
            status="pending",
        )
    data: dict = narrative["data"]
    return ProfileNarrativeRead(
        subject=subject.value,
        status=str(narrative.get("status") or "published"),
        persona_title=str(data.get("persona_title") or ""),
        persona_tags=list(data.get("persona_tags") or []),
        insight=str(data.get("insight") or ""),
        dimensions=list(data.get("dimensions") or []),
        ideal_weights=list(data.get("ideal_weights") or []),
        recent_change=data.get("recent_change"),
        history_observations=list(data.get("history_observations") or []),
        conclusion=str(data.get("conclusion") or ""),
    )
