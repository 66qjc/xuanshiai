"""AI 通用任务查询与取消接口 (统一方案 §11.3, 执行计划 §3.2).

- ``GET /api/v1/ai/tasks/{task_id}``: 200 TaskPollState + 安全 result ref；
  任务不存在或非本人 → 404 TASK_NOT_FOUND。
- ``POST /api/v1/ai/tasks/{task_id}/cancel``: 202 cancel_requested；
  已完成/不可取消 → 409 TASK_NOT_CANCELLABLE。

普通响应不携带 provider trace、原文或密钥；错误统一为
``AiErrorDetail`` 形状并携带 request_id。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse, AiTaskStatus, TaskPollState
from app.services.ai.tasks import (
    TaskError,
    get_task,
    request_cancel,
)

router = APIRouter()


class TaskDetailResponse(TaskPollState):
    """Task detail: poll state plus safe result reference."""

    result_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class CancelAcceptedResponse(BaseModel):
    """202 cancel body (统一方案 §11.3 ``cancel_requested``)."""

    task_id: str
    status: AiTaskStatus
    cancel_requested: bool = True


def _request_id() -> str:
    supplied = request_id_context.get()
    if supplied and supplied != "-":
        return supplied
    return uuid4().hex


def _error_response(exc: TaskError) -> HTTPException:
    # FastAPI serialises the ``detail`` kwarg under a top-level "detail" key,
    # so the inner payload is the AiErrorResponse dict (统一方案 §11.1 shape).
    detail = AiErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=_request_id(),
        retryable=exc.retryable,
        retry_after_ms=exc.retry_after_ms,
    )
    return HTTPException(
        status_code=exc.status_code,
        detail=detail.model_dump(),
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="查询 AI 通用任务状态与结果引用",
)
async def get_ai_task(
    task_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskDetailResponse:
    """Return the task poll state for the current owner.

    任务只允许 owner 读取（管理员端不纳入本轮 C 端范围）；task 不存在或
    非本人统一返回 ``404 TASK_NOT_FOUND``，不泄露任务归属。
    """
    task = await get_task(db, task_id)
    if task is None or task.owner_user_id != current.id:
        raise _error_response(
            TaskError(
                code="TASK_NOT_FOUND",
                message="任务不存在",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        )
    return TaskDetailResponse(
        task_id=task.task_id,
        status=task.status,
        stage=task.stage,
        poll_after_ms=1000 if task.status in {AiTaskStatus.QUEUED, AiTaskStatus.LEASED, AiTaskStatus.RUNNING, AiTaskStatus.RETRY_WAIT} else 0,
        expires_at=task.lease_until if task.lease_until is not None else None,
        result_ref=task.result_ref,
        error_code=task.error_code,
        error_message=task.error_message,
    )


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=CancelAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="取消 AI 通用任务",
)
async def cancel_ai_task(
    task_id: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelAcceptedResponse:
    """Request cancellation inside the cancellable window.

    已完成（succeeded/failed）或不可取消阶段返回
    ``409 TASK_NOT_CANCELLABLE``；不存在/非本人返回
    ``404 TASK_NOT_FOUND``。
    """
    try:
        task = await request_cancel(db, task_id, current.id)
    except TaskError as exc:
        raise _error_response(exc) from exc
    return CancelAcceptedResponse(task_id=task.task_id, status=task.status)
