"""M03 AI 搜索路由（统一方案 §8.3/§11.1/§11.2，执行计划 §3.2）。

前缀 `/api/v1/ai`（由 ``app/api/router.py`` 注册），共 7 个路径：

- ``POST /search-drafts``：202 draft+parse task
- ``GET /search-drafts/{draft_id}``：200 AST/未知/冲突（仅本人）
- ``PATCH /search-drafts/{draft_id}``：200 新 condition revision（乐观锁）
- ``POST /search-drafts/{draft_id}/confirm``：202 snapshot+task
- ``GET /search-suggestions``：200 可编辑词建议
- ``GET /search-snapshots/{snapshot_id}/results``：200 cursor 结果（每次重新门禁）
- ``DELETE /search-snapshots/{snapshot_id}``：202 cleanup task（立即不可读）

错误映射固定：``SearchPolicyDenied`` → 422 ``AI_POLICY_DENIED``；
``SearchInputInvalid`` → 400 ``AI_INPUT_INVALID``；这两个异常都由纯编译/输入
校验产生，异常路径不触发数据库查询。畸形/跨查询 cursor → 400
``INVALID_CANDIDATE_CURSOR``（与 discovery 路由一致）。未登录 401、非本人/
不存在统一 404、开关关闭 503 ``AI_FEATURE_DISABLED``。普通响应不携带原文、
provider trace 或密钥。
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse
from app.schemas.ai_profile import CleanupTaskAccepted
from app.schemas.ai_search import (
    SearchConditionPatchRequest,
    SearchDraftCreateRequest,
    SearchDraftParseRead,
    SearchDraftRead,
    SearchResultPageRead,
    SearchSnapshotAccepted,
    SearchSuggestionRead,
)
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.profile import DraftVersionConflict
from app.services.ai.search import (
    SearchConsentRequired,
    SearchDraftNotConfirmed,
    SearchDraftNotFound,
    SearchInputInvalid,
    SearchPolicyDenied,
    SearchQuotaExceeded,
    SearchResultStale,
    SearchSnapshotNotFound,
    confirm_search_draft,
    create_search_draft,
    delete_search_snapshot,
    execute_search_snapshot,
    get_search_suggestions,
    load_search_draft,
    patch_search_draft,
)
from app.services.ai.tasks import TaskError
from app.services.candidate_query import InvalidCandidateCursor

router = APIRouter()

# Idempotency-Key 契约（§11.1）：8-128 位 ASCII，禁止空白。
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


def _require_search_feature() -> None:
    try:
        require_ai_feature(AiFeature.SEARCH, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "AI 搜索功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


def _check_idempotency_key(idempotency_key: str | None) -> None:
    if not idempotency_key or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise _error_response(
            "AI_INPUT_INVALID",
            "Idempotency-Key 必须为 8-128 位 ASCII 字符",
            status.HTTP_400_BAD_REQUEST,
        )


@router.post(
    "/search-drafts",
    response_model=SearchDraftParseRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建 AI 搜索草稿并创建解析任务",
)
async def create_search_draft_route(
    body: SearchDraftCreateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SearchDraftParseRead:
    """202 draft+parse task；query_text 1-1000，每分钟 5 次限流。"""
    _require_search_feature()
    _check_idempotency_key(idempotency_key)
    try:
        draft = await create_search_draft(
            db,
            current.id,
            body.query_text,
            body.source,
            body.locale,
            idempotency_key,
        )
    except SearchInputInvalid as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code
        ) from exc
    except SearchConsentRequired as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchQuotaExceeded as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return SearchDraftParseRead(
        draft_id=draft.draft_id,
        status=draft.status,
        task_id=draft.task_id,
        condition_schema_version=draft.condition_schema_version,
        expires_at=draft.expires_at,
    )


@router.get(
    "/search-drafts/{draft_id}",
    response_model=SearchDraftRead,
    status_code=status.HTTP_200_OK,
    summary="查询本人的 AI 搜索草稿",
)
async def get_search_draft_route(
    draft_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchDraftRead:
    """返回仅本人草稿；不存在/非本人统一 404。"""
    _require_search_feature()
    try:
        return await load_search_draft(db, draft_id, current.id)
    except SearchDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc


@router.patch(
    "/search-drafts/{draft_id}",
    response_model=SearchDraftRead,
    status_code=status.HTTP_200_OK,
    summary="显式确认/修改/删除搜索条件",
)
async def patch_search_draft_route(
    draft_id: str,
    body: list[SearchConditionPatchRequest],
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_condition_revision: int | None = Query(
        default=None, ge=0, alias="expected_condition_revision"
    ),
) -> SearchDraftRead:
    """应用 confirm/edit/remove；condition_revision 乐观锁。"""
    _require_search_feature()
    _check_idempotency_key(idempotency_key)
    if expected_condition_revision is None:
        raise _error_response(
            "AI_INPUT_INVALID",
            "PATCH 必须携带 expected_condition_revision 查询参数",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        return await patch_search_draft(
            db,
            draft_id,
            current.id,
            body,
            expected_condition_revision,
        )
    except SearchInputInvalid as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotConfirmed as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except DraftVersionConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    await db.commit()


@router.post(
    "/search-drafts/{draft_id}/confirm",
    response_model=SearchSnapshotAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="确认搜索草稿并创建不可变快照与执行任务",
)
async def confirm_search_draft_route(
    draft_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_condition_revision: int | None = Query(
        default=None, ge=0, alias="expected_condition_revision"
    ),
) -> SearchSnapshotAccepted:
    """全部 hard 条件确认且无冲突才创建快照；编译失败不创建候选任务。"""
    _require_search_feature()
    _check_idempotency_key(idempotency_key)
    if expected_condition_revision is None:
        raise _error_response(
            "AI_INPUT_INVALID",
            "confirm 必须携带 expected_condition_revision 查询参数",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        snapshot = await confirm_search_draft(
            db,
            draft_id,
            current.id,
            expected_condition_revision,
            idempotency_key,
        )
    except SearchPolicyDenied as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchInputInvalid as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchDraftNotConfirmed as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except DraftVersionConflict as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return SearchSnapshotAccepted(
        snapshot_id=snapshot.snapshot_id,
        task_id=snapshot.task_id,
        status=snapshot.status,
        stage=None,
        poll_after_ms=1000 if not snapshot.replayed else 0,
        expires_at=None,
        condition_schema_version=snapshot.condition_schema_version,
        degraded=snapshot.degraded,
    )


@router.get(
    "/search-suggestions",
    response_model=SearchSuggestionRead,
    status_code=status.HTTP_200_OK,
    summary="查询本人可编辑的搜索标签建议",
)
async def get_search_suggestions_route(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchSuggestionRead:
    """只读本人已确认且允许搜索的标签；不足返回空数组。"""
    _require_search_feature()
    return await get_search_suggestions(db, current.id)


@router.get(
    "/search-snapshots/{snapshot_id}/results",
    response_model=SearchResultPageRead,
    status_code=status.HTTP_200_OK,
    summary="查询搜索快照的 cursor 结果页",
)
async def get_search_results_route(
    snapshot_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    cursor: str | None = Query(default=None, max_length=512),
    page_size: int = Query(default=20, ge=1, le=20),
) -> SearchResultPageRead:
    """每次读取重新过可见性门禁；结果过期标 stale。"""
    _require_search_feature()
    try:
        return await execute_search_snapshot(
            db, snapshot_id, current.id, cursor, page_size
        )
    except SearchSnapshotNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except SearchResultStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except InvalidCandidateCursor as exc:
        # 文档 §6：跨查询/伪造/过长的 cursor → 400 INVALID_CANDIDATE_CURSOR，
        # 与手工 discovery 路由（app/services/discovery.py）的映射一致。
        raise _error_response(
            "INVALID_CANDIDATE_CURSOR",
            "cursor 无效或与当前查询不匹配，请丢弃后从第一页重新请求",
            status.HTTP_400_BAD_REQUEST,
        ) from exc


@router.delete(
    "/search-snapshots/{snapshot_id}",
    response_model=CleanupTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="删除搜索快照并创建清理任务（立即不可读）",
)
async def delete_search_snapshot_route(
    snapshot_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CleanupTaskAccepted:
    """软删除快照：同步不可读 + cleanup 任务。"""
    _require_search_feature()
    _check_idempotency_key(idempotency_key)
    try:
        task = await delete_search_snapshot(
            db, snapshot_id, current.id, idempotency_key
        )
    except SearchSnapshotNotFound as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    return CleanupTaskAccepted(
        task_id=task.task_id, status=task.status, cleanup_requested=True
    )
