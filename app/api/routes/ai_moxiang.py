"""墨相师四阶段融合 Phase 2 REST 路由 (P2-01 / P2-04)。

挂载前缀 ``/ai``(见 ``app/api/router.py``)。

路径:

- ``GET /ai/moxiang/state`` —— 装配 Contract v1.1 §6.1 响应;Phase 2 增加
  ``can_start_ideal_partner`` 顶层字段供前端"双阶段卡"使用。
- ``POST /ai/moxiang/journey/start?subject=ideal_partner`` —— Phase 2 P2-04:
  用户从档案入口主动开启/恢复 ideal_partner master session。
  前置条件:

  1) ``profile_text_extract`` 授权存在;
  2) personal 已发布(``published_revision_id`` 非空)或该用户已有 ideal_partner
     任何历史记录(老用户恢复路径,P2-01);
  3) subject 必须是 ``ideal_partner``;personal 不通过此入口开启(只走自然聊天)。

未授权 → 200 + ``consent_granted=false``(契约 §6.1)。其他错误统一使用
``AiErrorResponse`` 形状,request_id 由 logging 上下文提供。

不引入 alembic/新依赖;不重复写 profile.py 已有的事务/快照逻辑。
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse
from app.schemas.ai_moxiang import MoxiangStateResponse
from app.schemas.ai_profile import ProfileSubject
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.moxiang_state import build_state_response
from app.services.ai.moxiang_state_db import MoxiangStateSqlRepository
from app.services.ai.profile import (
    AIConsentRequired,
    AIInputError,
    create_master_session,
)

router = APIRouter()

_SUBJECT_PATTERN = re.compile(r"^(personal|ideal_partner)$")


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


def _require_journey_feature() -> None:
    """Phase 2 P1-C 沿用 ``ai_moxiang_journey_enabled`` 开关(契约 v1.1 §10)。

    默认关闭:关闭时返回 503,前端显示"暂不可用";旧 ``profile_build`` 不受影响。
    """
    if not bool(getattr(settings, "ai_moxiang_journey_enabled", False)):
        raise _error_response(
            "AI_FEATURE_DISABLED",
            "墨相师连续旅程当前未启用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    # 额外: profile 主开关也必须开(否则 consent 检查 + 任务写入都会失败)
    try:
        require_ai_feature(AiFeature.PROFILE, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "AI 画像功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


@router.get("/moxiang/state", response_model=MoxiangStateResponse)
async def get_moxiang_state(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MoxiangStateResponse:
    """``GET /api/v1/ai/moxiang/state``(契约 v1.1 §6.1)。

    未登录返回 401(由 ``get_current_user`` 负责)。已登录但未授权返回 200 +
    ``consent_granted=false``,绝不抛错。
    """
    repo = MoxiangStateSqlRepository(db)
    return await build_state_response(user_id=current_user, repo=repo)


@router.post("/moxiang/journey/start")
async def start_journey(
    subject: str = Query(
        ...,
        description="开启/恢复的画像主体;Phase 2 仅允许 ideal_partner。",
        pattern=r"^(personal|ideal_partner)$",
    ),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """``POST /api/v1/ai/moxiang/journey/start`` —— Phase 2 P2-04。

    行为:

    - subject == ``personal``: 返回 400 ``AI_INPUT_INVALID``
      (个人画像只能从墨相师页自然聊天开始,不允许显式开启)。
    - subject == ``ideal_partner``:

      - 调用 ``build_state_response`` 计算 ``can_start_ideal_partner``;
      - 若 ``can_start_ideal_partner=false``(personal 未发布且无 ideal_partner
        历史)→ 返回 403 ``AI_INPUT_INVALID``,文案明确提示用户先完成我的墨相。
      - 否则调用 ``create_master_session`` 创建/恢复 master session;
      - 返回 ``{ subject, session_id, journey_stage, already_existed }``。
    """
    _require_journey_feature()
    if subject != ProfileSubject.IDEAL_PARTNER.value:
        raise _error_response(
            "AI_INPUT_INVALID",
            "此接口只用于开启愿遇之相;我的墨相请从墨相师页面进入",
            status.HTTP_400_BAD_REQUEST,
        )

    repo = MoxiangStateSqlRepository(db)
    state = await build_state_response(user_id=current_user, repo=repo)
    if not state.can_start_ideal_partner:
        raise _error_response(
            "AI_INPUT_INVALID",
            "请先完成我的墨相再开启愿遇之相",
            status.HTTP_403_FORBIDDEN,
        )

    try:
        session = await create_master_session(
            db,
            owner_user_id=current_user,
            subject=ProfileSubject.IDEAL_PARTNER,
            consent_version="profile-text-v1",
        )
    except AIConsentRequired as exc:
        raise _error_response(
            "AI_CONSENT_REQUIRED",
            "请先完成画像文本抽取授权",
            status.HTTP_403_FORBIDDEN,
        ) from exc
    except AIInputError as exc:
        raise _error_response(
            "AI_INPUT_INVALID",
            str(exc) or "参数不合法",
            status.HTTP_400_BAD_REQUEST,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _error_response(
            "AI_TEMPORARILY_UNAVAILABLE",
            "墨相师暂时无法开启愿遇之相,请稍后再试",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        ) from exc

    await db.commit()
    return {
        "subject": subject,
        "session_id": session.session_id,
        "journey_stage": "chatting",
        "already_existed": False,
    }


@router.get(
    "/moxiang/archive",
    summary="我的墨相档案聚合(Phase 3 P3-03)",
)
async def get_moxiang_archive(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """``GET /api/v1/ai/moxiang/archive`` —— 双主体归档聚合。

    永远 200(只要登录);未授权 / 无任何画像 → 字段全空 + fallback_available=False。
    路径不存在单独 404,避免暴露「是否有 profile」。
    """
    try:
        from app.services.ai.archive import (
            SqlArchiveRepository,
            build_archive,
        )

        repo = SqlArchiveRepository(db)
        archive = await build_archive(user_id=current_user, repo=repo)
    except Exception as exc:  # noqa: BLE001
        raise _error_response(
            "AI_TEMPORARILY_UNAVAILABLE",
            "档案暂时无法读取,请稍后再试",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        ) from exc
    return archive.to_dict()
