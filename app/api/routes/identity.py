"""注册身份和红娘申请接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, CurrentUser, get_current_user, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.auth import (
    MatchmakerApplicationCreate,
    MatchmakerApplicationResponse,
    MatchmakerReviewRequest,
    RegistrationIntentResponse,
    RegistrationIntentUpdate,
)
from app.services.identity import (
    APPLICATION_OPTIONS,
    INTENT_OPTIONS,
    create_matchmaker_application,
    get_registration_intent,
    list_my_applications,
    review_matchmaker_application,
    update_registration_intent,
)

router = APIRouter()


@router.get("/registration/intents", response_model=list[RegistrationIntentResponse], summary="查询注册身份选项")
async def registration_intents() -> list[dict[str, str]]:
    """返回自己找、父母帮找和找搭子三种注册身份。"""
    return [{"intent_type": key, "label": value[0], "description": value[1]} for key, value in INTENT_OPTIONS.items()]


@router.put("/auth/registration-intent", response_model=RegistrationIntentResponse, summary="提交注册身份")
async def set_registration_intent(
    body: RegistrationIntentUpdate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """保存当前用户的注册意图，重复提交具有幂等性。"""
    return await update_registration_intent(db, current.id, body)


@router.get("/auth/registration-intent", response_model=RegistrationIntentResponse | None, summary="查询当前注册身份")
async def current_registration_intent(
    current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, str] | None:
    """返回当前用户已选择的注册意图。"""
    return await get_registration_intent(db, current.id)


@router.get("/matchmaker/application-types", summary="查询红娘申请类型")
async def matchmaker_application_types() -> list[dict[str, str]]:
    """返回推广红娘、合伙人和服务红娘申请类型。"""
    return [{"application_type": key, "label": label} for key, label in APPLICATION_OPTIONS.items()]


@router.post("/matchmaker/applications", response_model=MatchmakerApplicationResponse, status_code=201, summary="提交红娘申请")
async def submit_matchmaker_application(
    body: MatchmakerApplicationCreate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """提交指定类型的红娘或合伙人申请。"""
    return await create_matchmaker_application(db, current, body)


@router.get("/matchmaker/applications/mine", response_model=list[MatchmakerApplicationResponse], summary="查询我的红娘申请")
async def my_matchmaker_applications(
    current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """返回当前用户提交的全部红娘和合伙人申请。"""
    return await list_my_applications(db, current.id)


@router.patch("/admin/matchmaker/applications/{application_id}", response_model=MatchmakerApplicationResponse, summary="审核红娘申请")
async def review_matchmaker_application_endpoint(
    application_id: int,
    body: MatchmakerReviewRequest,
    admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """管理员审核、驳回或暂停红娘及合伙人申请。"""
    return await review_matchmaker_application(db, admin.account.id, application_id, body)
