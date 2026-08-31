"""M07 三类推荐路由（WP-P6e，方案 §四 WP-P6 / §六 WP-C3 第一步）。

前缀 ``/api/v1/ai``（由 ``app/api/router.py`` 注册），共 1 个路径：

- ``GET /recommendations?view=i_like|likes_me|similar``：读推荐快照；
  无可用快照（缺失/过期）时入队重建任务并返回 ``regenerating=true``（同日
  幂等收敛）；门禁关闭 503 ``AI_FEATURE_DISABLED``。

脱敏边界：出参仅含对方用户 ID、分数与理由码，不下发对方画像原文；卡片
基本资料由前端用既有候选名片接口拼装。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse
from app.schemas.ai_recommend import RecommendationPage, RecommendationView
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.recommend import (
    enqueue_recommendation_rebuild,
    read_recommendations,
)
from app.services.ai.tasks import TaskError

router = APIRouter()


def _request_id() -> str:
    supplied = request_id_context.get()
    if supplied and supplied != "-":
        return supplied
    return uuid4().hex


def _error_response(
    code: str, message: str, status_code: int, *, retryable: bool = False
) -> HTTPException:
    from fastapi import HTTPException

    detail = AiErrorResponse(
        code=code,
        message=message,
        request_id=_request_id(),
        retryable=retryable,
        retry_after_ms=0,
    )
    return HTTPException(status_code=status_code, detail=detail.model_dump())


def _require_recommend_feature() -> None:
    try:
        require_ai_feature(AiFeature.RECOMMEND, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "推荐功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


@router.get(
    "/recommendations",
    response_model=RecommendationPage,
    status_code=status.HTTP_200_OK,
    summary="查询三类推荐（我会喜欢/会喜欢我/相似的人）",
)
async def get_recommendations_route(
    view: RecommendationView = Query(...),
    limit: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecommendationPage:
    """读取当前用户的推荐快照；miss 时触发后台重建（下次读取可得）。

    读取面只见 ``status='ready'`` 且未过期的最新 generation；miss 入队为
    同日幂等任务（``recommend-view-{user}-{日期}``），重复 GET 不重复入队。
    """
    _require_recommend_feature()
    items = await read_recommendations(db, current.id, view, limit)
    if items:
        return RecommendationPage(view=view, items=items, regenerating=False)
    try:
        task = await enqueue_recommendation_rebuild(db, current.id)
    except TaskError as exc:
        raise _error_response(
            exc.code, exc.message, exc.status_code, retryable=exc.retryable
        ) from exc
    # miss 入队是本读路径唯一的未决写入；get_db 只关闭不提交，须显式提交。
    await db.commit()
    # regenerating 语义 = 确有在途重建。无授权/无投影（未入队）或同日任务已
    # 终态（succeeded/failed/cancelled）时必须如实返回 false，否则客户端会
    # 对一个永远不会产出结果的请求无限轮询。
    task_status = str(getattr(task.status, "value", task.status)) if task else ""
    regenerating = task_status in {"queued", "leased", "running", "retry_wait"}
    return RecommendationPage(view=view, items=[], regenerating=regenerating)
