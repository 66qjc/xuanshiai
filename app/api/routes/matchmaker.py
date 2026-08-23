"""First-phase matchmaker and paid service request routes."""

from fastapi import APIRouter, Depends, Header, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_admin, get_current_user
from app.db.session import get_db
from app.schemas.matchmaker import (
    MatchmakerAdminServiceRequestUpdate,
    MatchmakerCard,
    MatchmakerContactResponse,
    MatchmakerContactExchangeCreate,
    MatchmakerContactExchangeContactsResponse,
    MatchmakerContactExchangeResponse,
    MatchmakerContactExchangeUpdate,
    MatchmakerContactUpdate,
    MatchmakerPage,
    MatchmakerServiceOrderCreate,
    MatchmakerServiceOrderPage,
    MatchmakerServiceOrderResponse,
    MatchmakerServiceProductCreate,
    MatchmakerServiceProductResponse,
    MatchmakerServiceProductUpdate,
    MatchmakerServiceRequestCreate,
    MatchmakerServiceRequestPage,
    MatchmakerServiceRequestResponse,
    MatchmakerServiceRequestUpdate, MatchmakerRatingCreate, MatchmakerRatingPage, MatchmakerRatingResponse,
)
from app.services.matchmaker import (
    admin_list_service_requests,
    admin_update_service_request,
    admin_create_service_product,
    create_service_request,
    create_service_order,
    get_matchmaker_contact,
    get_contact_exchange_contacts,
    get_service_order,
    get_service_product,
    get_service_request,
    get_matchmaker,
    list_service_products,
    list_service_orders,
    list_matchmakers,
    list_service_requests,
    set_matchmaker_contact,
    create_contact_exchange,
    update_contact_exchange,
    get_contact_exchange,
    admin_update_service_product,
    update_service_request, create_matchmaker_rating, list_matchmaker_ratings,
)

router = APIRouter(prefix="/matchmakers")
product_router = APIRouter(prefix="/matchmaker/service-products")


@router.get("", response_model=MatchmakerPage, summary="查询服务红娘列表")
async def matchmaker_list(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerPage:
    return await list_matchmakers(db, page, page_size)


@router.get("/ranking", response_model=MatchmakerPage, summary="查询热心红娘排行榜")
async def matchmaker_ranking(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerPage:
    return await list_matchmakers(db, page, page_size, ranking=True)


@router.get("/{matchmaker_id}", response_model=MatchmakerCard, summary="查询服务红娘详情")
async def matchmaker_detail(
    matchmaker_id: int = Path(..., ge=1), db: AsyncSession = Depends(get_db)
) -> MatchmakerCard:
    return await get_matchmaker(db, matchmaker_id)


@product_router.get("", response_model=list[MatchmakerServiceProductResponse], summary="查询在售红娘服务商品")
async def service_products(db: AsyncSession = Depends(get_db)) -> list[MatchmakerServiceProductResponse]:
    return await list_service_products(db)


@product_router.get("/{product_id}", response_model=MatchmakerServiceProductResponse)
async def service_product_detail(
    product_id: int = Path(..., ge=1), db: AsyncSession = Depends(get_db)
) -> MatchmakerServiceProductResponse:
    return await get_service_product(db, product_id)


@product_router.post("", response_model=MatchmakerServiceProductResponse, status_code=201, summary="创建红娘服务商品")
async def create_service_product(
    body: MatchmakerServiceProductCreate,
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceProductResponse:
    return await admin_create_service_product(db, admin.id, body)


@product_router.patch("/{product_id}", response_model=MatchmakerServiceProductResponse, summary="修改或下架红娘服务商品")
async def update_service_product(
    product_id: int = Path(..., ge=1),
    body: MatchmakerServiceProductUpdate = ...,
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceProductResponse:
    return await admin_update_service_product(db, product_id, body)


requests_router = APIRouter(prefix="/matchmaker/service-requests")


@requests_router.post("/orders", response_model=MatchmakerServiceOrderResponse, status_code=201, summary="创建待支付红娘服务订单")
async def create_order(
    body: MatchmakerServiceOrderCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceOrderResponse:
    return await create_service_order(db, current, body, idempotency_key)


@requests_router.get("/orders/{order_no}", response_model=MatchmakerServiceOrderResponse, summary="查询我的红娘服务订单")
async def get_order(
    order_no: str = Path(..., min_length=8, max_length=64),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceOrderResponse:
    return await get_service_order(db, current, order_no)


@requests_router.get("/orders", response_model=MatchmakerServiceOrderPage)
async def my_orders(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceOrderPage:
    return await list_service_orders(db, current, page, page_size)


@requests_router.post("", response_model=MatchmakerServiceRequestResponse, status_code=201, summary="提交牵线服务申请")
async def create_request(
    body: MatchmakerServiceRequestCreate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestResponse:
    return await create_service_request(db, current, body)


@requests_router.get("/mine", response_model=MatchmakerServiceRequestPage, summary="查询我提交的牵线申请")
async def mine_requests(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestPage:
    return await list_service_requests(db, current, page, page_size)


@requests_router.get("/assigned", response_model=MatchmakerServiceRequestPage, summary="查询分配给我的牵线申请")
async def assigned_requests(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestPage:
    return await list_service_requests(db, current, page, page_size, assigned=True)


@requests_router.get("/{service_id}", response_model=MatchmakerServiceRequestResponse)
async def service_request_detail(
    service_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestResponse:
    return await get_service_request(db, current, service_id)


@requests_router.patch("/{service_id}", response_model=MatchmakerServiceRequestResponse, summary="处理牵线服务申请")
async def update_request(
    service_id: int = Path(..., ge=1),
    body: MatchmakerServiceRequestUpdate = ...,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestResponse:
    return await update_service_request(db, current, service_id, body)


@requests_router.patch("/{service_id}/contact", response_model=MatchmakerContactResponse, summary="提交红娘微信联系方式")
async def update_contact(
    service_id: int = Path(..., ge=1),
    body: MatchmakerContactUpdate = ...,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactResponse:
    return await set_matchmaker_contact(db, current, service_id, body)


@requests_router.get("/{service_id}/contact", response_model=MatchmakerContactResponse, summary="查看已购买服务的红娘微信")
async def contact(
    service_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactResponse:
    return await get_matchmaker_contact(db, current, service_id)


@requests_router.post("/{service_id}/contact-exchanges", response_model=MatchmakerContactExchangeResponse, status_code=201, summary="发起双方联系方式授权")
async def create_exchange(
    service_id: int = Path(..., ge=1),
    body: MatchmakerContactExchangeCreate = ...,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactExchangeResponse:
    return await create_contact_exchange(db, current, service_id, body)


@requests_router.patch("/contact-exchanges/{exchange_id}", response_model=MatchmakerContactExchangeResponse, summary="同意或撤回联系方式授权")
async def update_exchange(
    exchange_id: int = Path(..., ge=1),
    body: MatchmakerContactExchangeUpdate = ...,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactExchangeResponse:
    return await update_contact_exchange(db, current, exchange_id, body)


@requests_router.get("/contact-exchanges/{exchange_id}", response_model=MatchmakerContactExchangeResponse, summary="查询联系方式授权状态")
async def get_exchange(
    exchange_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactExchangeResponse:
    return await get_contact_exchange(db, current, exchange_id)


@requests_router.get("/contact-exchanges/{exchange_id}/contacts", response_model=MatchmakerContactExchangeContactsResponse, summary="查看已授权双方联系方式")
async def exchange_contacts(
    exchange_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerContactExchangeContactsResponse:
    return await get_contact_exchange_contacts(db, current, exchange_id)


admin_router = APIRouter(prefix="/admin/matchmaker/service-requests")


@admin_router.get("", response_model=MatchmakerServiceRequestPage, summary="管理员查询牵线服务申请")
async def admin_requests(
    status: int | None = Query(None, ge=0, le=3),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestPage:
    return await admin_list_service_requests(db, page, page_size, status)


@admin_router.patch("/{service_id}", response_model=MatchmakerServiceRequestResponse, summary="管理员分配或处理牵线服务申请")
async def admin_update_request(
    service_id: int = Path(..., ge=1),
    body: MatchmakerAdminServiceRequestUpdate = ...,
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerServiceRequestResponse:
    return await admin_update_service_request(db, admin.id, service_id, body)
