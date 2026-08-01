"""Homepage discovery, card interactions and public profile routes."""

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_browsable_user,
    get_current_user,
    get_realname_verified_user,
    get_verified_user,
)
from app.db.session import get_db
from app.schemas.discovery import (
    ApplicationCreateRequest,
    ApplicationRejectRequest,
    ApplicationResponse,
    ApplicationPage,
    BrowseHistoryPage,
    DiscoveryFilters,
    DiscoveryPage,
    DiscoverySearch,
    FavoriteResponse,
    FavoritePage,
    FavoriteReceivedPage,
    FilterOptionsResponse,
    PublicProfileResponse,
    SavedFilterResponse,
    SuperLikeResponse,
    SuperLikePage,
    VisitorPage,
)
from app.services.discovery import (
    browse_history,
    create_application,
    create_poster,
    create_superlike,
    get_discovery_page,
    get_filter_options,
    get_saved_filter,
    search_discovery,
    list_applications,
    list_favorites,
    list_received_favorites,
    list_superlikes,
    respond_application,
    set_favorite,
    save_filter,
    view_profile,
    visitors,
)

router = APIRouter(prefix="/discovery")
users_router = APIRouter(prefix="/users")


@router.get("/filter-options", response_model=FilterOptionsResponse, summary="查询首页筛选选项")
async def filter_options() -> FilterOptionsResponse:
    return await get_filter_options()


@router.get("/recommendations", response_model=DiscoveryPage, summary="查询推荐名片流")
async def recommendations(filters: DiscoveryFilters = Depends(), current: CurrentUser = Depends(get_browsable_user), db: AsyncSession = Depends(get_db)) -> DiscoveryPage:
    return await get_discovery_page(db, current.id, filters, plaza=False)


@router.get("/plaza", response_model=DiscoveryPage, summary="查询广场名片流")
async def plaza(filters: DiscoveryFilters = Depends(), current: CurrentUser = Depends(get_browsable_user), db: AsyncSession = Depends(get_db)) -> DiscoveryPage:
    return await get_discovery_page(db, current.id, filters, plaza=True)


@router.get("/search", response_model=DiscoveryPage, summary="按昵称或标签搜索用户")
async def search(query: DiscoverySearch = Depends(), current: CurrentUser = Depends(get_browsable_user), db: AsyncSession = Depends(get_db)) -> DiscoveryPage:
    return await search_discovery(db, current.id, query)


@router.get("/filters/saved", response_model=SavedFilterResponse, summary="获取已保存筛选条件")
async def saved_filter(current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> SavedFilterResponse:
    return await get_saved_filter(db, current.id)


@router.put("/filters/saved", response_model=SavedFilterResponse, summary="保存筛选条件")
async def update_saved_filter(body: DiscoveryFilters, current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> SavedFilterResponse:
    return await save_filter(db, current.id, body)


@router.get("/browse-history", response_model=BrowseHistoryPage, summary="查询浏览记录")
async def history(page: int = Query(default=1, ge=1, le=1000), page_size: int = Query(default=20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> BrowseHistoryPage:
    return await browse_history(db, current.id, page, page_size)


@router.get("/visitors", response_model=VisitorPage, summary="查询谁看过我")
async def visitor_list(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> VisitorPage:
    return await visitors(db, current.id, page, page_size)


@router.get("/favorites", response_model=FavoritePage, summary="查询我的收藏")
async def favorites(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FavoritePage:
    return await list_favorites(db, current.id, page, page_size)


@router.get("/favorites/received", response_model=FavoriteReceivedPage, summary="查询收到的收藏")
async def received_favorites(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FavoriteReceivedPage:
    return await list_received_favorites(db, current.id, page, page_size)


@router.put("/favorites/{target_id}", response_model=FavoriteResponse, summary="收藏名片")
async def add_favorite(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FavoriteResponse:
    return await set_favorite(db, current.id, target_id, True)


@router.delete("/favorites/{target_id}", response_model=FavoriteResponse, summary="取消收藏")
async def remove_favorite(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FavoriteResponse:
    return await set_favorite(db, current.id, target_id, False)


@router.post("/applications/{target_id}", response_model=ApplicationResponse, status_code=201, summary="申请认识")
async def apply_to_user(target_id: int = Path(..., ge=1), body: ApplicationCreateRequest = Body(...), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> ApplicationResponse:
    return await create_application(db, current.id, target_id, body)


@router.get("/applications/incoming", response_model=ApplicationPage, summary="查询收到的认识申请")
async def incoming_applications(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ApplicationPage:
    return await list_applications(db, current.id, incoming=True, page=page, page_size=page_size)


@router.get("/applications/outgoing", response_model=ApplicationPage, summary="查询发出的认识申请")
async def outgoing_applications(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ApplicationPage:
    return await list_applications(db, current.id, incoming=False, page=page, page_size=page_size)


@router.post("/applications/{application_id}/accept", response_model=ApplicationResponse, summary="同意认识申请")
async def accept_application(application_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> ApplicationResponse:
    return await respond_application(db, current.id, application_id, True)


@router.post("/applications/{application_id}/reject", response_model=ApplicationResponse, summary="拒绝认识申请")
async def reject_application(application_id: int = Path(..., ge=1), body: ApplicationRejectRequest | None = None, current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> ApplicationResponse:
    return await respond_application(db, current.id, application_id, False, body)


@router.post("/superlikes/{target_id}", response_model=SuperLikeResponse, status_code=201, summary="爆灯")
async def superlike(target_id: int = Path(..., ge=1), idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> SuperLikeResponse:
    return await create_superlike(db, current.id, target_id, idempotency_key)


@router.get("/superlikes/sent", response_model=SuperLikePage, summary="查询我发出的爆灯")
async def sent_superlikes(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> SuperLikePage:
    return await list_superlikes(db, current.id, "sent", page, page_size)


@router.get("/superlikes/received", response_model=SuperLikePage, summary="查询收到的爆灯")
async def received_superlikes(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> SuperLikePage:
    return await list_superlikes(db, current.id, "received", page, page_size)


@users_router.get("/{user_id}/profile", response_model=PublicProfileResponse, summary="查看他人主页")
async def public_profile(user_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PublicProfileResponse:
    return await view_profile(db, current.id, user_id)


@users_router.get("/{user_id}/poster", response_class=Response, summary="生成分享海报")
async def poster(user_id: int = Path(..., ge=1), template: int = Query(default=1, ge=1, le=25), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    return await create_poster(db, current.id, user_id, template)
