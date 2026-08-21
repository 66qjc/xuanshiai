"""AI 通用任务查询与取消接口 (统一方案 §11.3, 执行计划 §3.2).

- ``GET /api/v1/ai/tasks/{task_id}``: 200 TaskPollState + 安全 result ref；
  任务不存在或非本人 → 404 TASK_NOT_FOUND。
- ``POST /api/v1/ai/tasks/{task_id}/cancel``: 202 cancel_requested；
  已完成/不可取消 → 409 TASK_NOT_CANCELLABLE。

普通响应不携带 provider trace、原文或密钥；错误统一为
``AiErrorDetail`` 形状并携带 request_id。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
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
    """Task detail: poll state plus safe result reference.

    ``result_payload`` carries task-type-specific result data (e.g.
    ``voice_transcribe`` returns ``{"transcript": "..."}``); it is ``None``
    for task types whose result is consumed elsewhere (e.g. ``profile_extract``
    writes to a draft fetched separately).  Only allowlisted keys from
    ``payload_summary`` are surfaced — never raw audio or secrets.
    """

    result_ref: str | None = None
    result_payload: dict[str, Any] | None = None
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


_NON_TERMINAL_POLLABLE = {
    AiTaskStatus.QUEUED,
    AiTaskStatus.LEASED,
    AiTaskStatus.RUNNING,
    AiTaskStatus.RETRY_WAIT,
}

# poll_after_ms 下限：避免客户端在退避窗口内空转轮询。
_MIN_POLL_AFTER_MS = 1000


def _compute_poll_after_ms(status: AiTaskStatus, next_run_at: datetime | None) -> int:
    """Derive the client poll interval from the task state.

    终态返回 0；``retry_wait`` 用 ``next_run_at - now`` 推导（至少
    ``_MIN_POLL_AFTER_MS``），让客户端在退避窗口内不空转；其余非终态
    状态用固定下限（缺陷 35）。
    """
    if status not in _NON_TERMINAL_POLLABLE:
        return 0
    if status is AiTaskStatus.RETRY_WAIT and next_run_at is not None:
        # next_run_at 来自 MySQL DATETIME（naive UTC），用 naive UTC now 比较。
        now = datetime.now(UTC).replace(tzinfo=None)
        delta_ms = max(0, int((next_run_at - now).total_seconds() * 1000))
        return max(delta_ms, _MIN_POLL_AFTER_MS)
    return _MIN_POLL_AFTER_MS


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
    # voice_transcribe 任务的转写文本存于独立的 voice_transcript 表，
    # 仅在终态 succeeded 时通过 result_ref（即 task_id）关联查询并透传；
    # 其余任务类型 result_payload 为 None。
    result_payload: dict[str, Any] | None = None
    if (
        task.task_type == "voice_transcribe"
        and task.status is AiTaskStatus.SUCCEEDED
        and task.result_ref
    ):
        row = await db.execute(
            text(
                "SELECT transcript FROM voice_transcript "
                "WHERE task_id = :task_id"
            ),
            {"task_id": task.result_ref},
        )
        transcript_row = row.mappings().first()
        if transcript_row and transcript_row.get("transcript"):
            result_payload = {"transcript": str(transcript_row["transcript"])}
    return TaskDetailResponse(
        task_id=task.task_id,
        status=task.status,
        stage=task.stage,
        poll_after_ms=_compute_poll_after_ms(task.status, task.next_run_at),
        expires_at=task.lease_until if task.lease_until is not None else None,
        result_ref=task.result_ref,
        result_payload=result_payload,
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
    await db.commit()
    return CancelAcceptedResponse(task_id=task.task_id, status=task.status)
