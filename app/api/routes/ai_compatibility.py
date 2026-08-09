"""M06 AI 匹配度路由（统一方案 §9.4/§11.1/§11.2，执行计划 §3.1/§3.2）。

前缀 `/api/v1/ai`（由 ``app/api/router.py`` 注册），共 2 个路径：

- ``GET /compatibility/{target_user_id}``：200 ready/stale/blocked/
  coverage_insufficient 或稳定禁用；每次读取重过可见性门禁与 revision 校验；
  不可见统一 ``404 CANDIDATE_NOT_VISIBLE``。
- ``POST /compatibility/{target_user_id}/recompute``：202 prediction+task；
  仅当前用户和当前可见目标；版本向量/幂等校验；写操作要求 ``Idempotency-Key``。

错误映射固定：``CandidateNotVisible`` → 404 ``CANDIDATE_NOT_VISIBLE``（不返回
具体拒绝原因）；``CompatibilityResultStale`` → 409 ``RESULT_STALE``；
``CompatibilityConsentRequired`` → 403 ``AI_CONSENT_REQUIRED``；
``CompatibilityInputInvalid`` → 400 ``AI_INPUT_INVALID``；任务冲突 → 409
``TASK_IDEMPOTENCY_CONFLICT``；开关关闭 503 ``AI_FEATURE_DISABLED``。
普通响应不携带原文、provider trace 或密钥。
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse
from app.schemas.ai_compatibility import (
    CompatibilityRecomputeRequest,
    CompatibilitySnapshotRead,
    CompatibilitySnapshotRecomputeRead,
    CompatibilitySnapshotStatus,
)
from app.services.ai.compatibility import (
    CandidateNotVisible,
    CompatibilityConsentRequired,
    CompatibilityInputInvalid,
    CompatibilityResultStale,
    read_compatibility_snapshot,
    request_compatibility_recompute,
)
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.tasks import TaskError

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


def _require_compatibility_feature() -> None:
    try:
        require_ai_feature(AiFeature.COMPATIBILITY_SHADOW, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "资料合拍参考功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


def _check_idempotency_key(idempotency_key: str | None) -> None:
    if not idempotency_key or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise _error_response(
            "AI_INPUT_INVALID",
            "Idempotency-Key 必须为 8-128 位 ASCII 字符",
            status.HTTP_400_BAD_REQUEST,
        )


@router.get(
    "/compatibility/{target_user_id}",
    response_model=CompatibilitySnapshotRead,
    status_code=status.HTTP_200_OK,
    summary="查询与目标用户的资料合拍参考",
)
async def get_compatibility_route(
    target_user_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompatibilitySnapshotRead:
    """返回当前用户对目标用户的 shadow 资料合拍参考。

    每次读取重过可见性门禁与 revision 校验；不可见统一 404
    ``CANDIDATE_NOT_VISIBLE``；版本/隐私变化或过期 → ``stale``。
    """
    _require_compatibility_feature()
    try:
        result = await read_compatibility_snapshot(db, current.id, target_user_id)
    except CandidateNotVisible as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    # 读取路径的 stale 落库标记（read_compatibility_snapshot → _mark_snapshot_stale
    # 的 UPDATE）在同一事务内执行；get_db 在请求结束时只关闭回滚、不提交。若不在此
    # 处显式 commit，文档承诺的「读取时同时将该快照落库标记为 stale」在生产永不生效
    # （审查 I-1）。读路径仅有 SELECT 与该 stale UPDATE，无其它未决写入会被意外固化。
    await db.commit()
    return result


@router.post(
    "/compatibility/{target_user_id}/recompute",
    response_model=CompatibilitySnapshotRecomputeRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重算与目标用户的资料合拍参考",
)
async def recompute_compatibility_route(
    body: CompatibilityRecomputeRequest,
    target_user_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=128
    ),
) -> CompatibilitySnapshotRecomputeRead:
    """重算 shadow：可见性门禁 → expected revision 校验 → 入队任务（202）。"""
    _require_compatibility_feature()
    _check_idempotency_key(idempotency_key)
    try:
        accepted = await request_compatibility_recompute(
            db,
            current.id,
            target_user_id,
            body.expected_viewer_profile_revision,
            body.expected_target_profile_revision,
            idempotency_key,
        )
    except CandidateNotVisible as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except CompatibilityResultStale as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except CompatibilityConsentRequired as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except CompatibilityInputInvalid as exc:
        raise _error_response(exc.code, exc.message, exc.status_code) from exc
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    await db.commit()
    # 202 的 status 是「快照预测状态」：任务尚未执行完成，尚无快照可读，统一按
    # coverage_insufficient（与 GET 无快照时的表示一致）；实际结果以 GET 为准。
    return CompatibilitySnapshotRecomputeRead(
        snapshot_id=accepted.snapshot_id,
        task_id=accepted.task_id,
        status=CompatibilitySnapshotStatus.COVERAGE_INSUFFICIENT,
        poll_after_ms=accepted.poll_after_ms,
        expires_at=accepted.expires_at,
    )
