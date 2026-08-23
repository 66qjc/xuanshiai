"""Administrator home APIs and read-only compatibility routes from the captured live UI."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.admin_home import AdminBootstrap, AdminDashboard, AnnouncementPage, LegacyResponse, RechargeItem
from app.services import admin_home

router = APIRouter(prefix="/admin")
legacy_router = APIRouter()


@router.get("/bootstrap", response_model=AdminBootstrap, summary="查询管理端首页初始化数据")
async def get_bootstrap(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AdminBootstrap:
    return await admin_home.bootstrap(db, admin)


@router.get("/dashboard", response_model=AdminDashboard, summary="查询管理端首页统计")
async def get_dashboard(from_date: date | None = Query(None, alias="from"), to_date: date | None = Query(None, alias="to"), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AdminDashboard:
    admin.require("dashboard.read")
    end = to_date or date.today()
    return await admin_home.dashboard(db, admin, from_date or end - timedelta(days=14), end)


@router.get("/announcements", response_model=AnnouncementPage, summary="分页查询更新公告")
async def get_announcements(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), category: str | None = Query(None, max_length=64), keyword: str | None = Query(None, max_length=100), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AnnouncementPage:
    return await admin_home.announcements(db, admin, page, page_size, category, keyword)


@router.get("/academy/categories", summary="查询婚创学苑栏目树")
async def get_academy_categories(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await admin_home.academy_categories(db)


@router.get("/finance/recharge-items", response_model=list[RechargeItem], summary="查询充值资源包")
async def get_recharge_items(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[RechargeItem]:
    return await admin_home.recharge_items(db)


def _legacy(data):
    return {"code": 200, "data": data, "msg": "success", "success": True}


def _legacy_page(records: list[dict], page: int, limit: int) -> dict:
    total = len(records)
    return {
        "current": page,
        "size": limit,
        "total": total,
        "pages": (total + limit - 1) // limit,
        "records": records[(page - 1) * limit: page * limit],
        "countId": None,
        "maxLimit": None,
        "optimizeCountSql": True,
        "orders": [],
        "searchCount": True,
    }


def _order_records(report: AdminDashboard, granularity: str) -> list[dict]:
    buckets: dict[tuple[int, int, int | None], dict] = {}
    for trend in report.trends:
        key = (trend.date.year, trend.date.month, trend.date.day if granularity == "Day" else None)
        bucket = buckets.setdefault(key, {"year": str(trend.date.year), "month": str(trend.date.month),
            "day": str(trend.date.day) if granularity == "Day" else None, "totalAmount": 0.0,
            "totalQty": 0, "refundAmount": 0.0, "refundQty": 0, "refundedAmount": 0.0,
            "refundedQty": 0, "netAmount": 0.0})
        bucket["totalAmount"] += float(trend.paid_amount)
        bucket["totalQty"] += trend.paid_count
        bucket["refundAmount"] += float(trend.completed_refund_amount)
        bucket["refundQty"] += trend.completed_refund_count
        bucket["refundedAmount"] += float(trend.completed_refund_amount)
        bucket["refundedQty"] += trend.completed_refund_count
        bucket["netAmount"] += float(trend.net_amount)
    return list(buckets.values())


@legacy_router.get("/common/api/image/getConfig", response_model=LegacyResponse[dict])
async def legacy_image_config(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin)):
    # Upload credentials are intentionally absent; uploads use the existing authenticated endpoint.
    return _legacy({"bucket": "", "fileDomain": "", "uploadDomain": "/api/v1/media", "imageWmType": "text", "videoPrivateQueue": "", "wmContent": "", "wmFont": "", "wmFontColor": "", "wmFontSize": 0, "wmGravity": "SouthEast", "wmOpen": False, "wmResize": 0, "wmRotate": 0, "wmTransparency": 0, "wmUnitH": 0, "wmUnitW": 0, "wmXdistance": 0, "wmYdistance": 0})


@legacy_router.get("/common/api/system/getTenantDetail", response_model=LegacyResponse[dict])
async def legacy_tenant_detail(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin)):
    return _legacy({"id": 0, "name": "", "alias": "", "customerName": "", "bindingDomain": "", "bindingDomainWithHttps": "", "certificationLocked": False, "concurrentNumLimit": 0, "faceProvider": None, "grantAuthDeadline": None, "grantPluginIds": "", "h5UsingFaceId": False, "maritalStatusLocked": False, "maritalStatusProvider": None, "phone": None, "regionDataMode": "", "signLocked": False, "smsChannel": "", "smsLocked": False, "smsSignature": "", "whetherLock": admin.account.status != 1})


@legacy_router.get("/common/api/config/getSystemBaseConfig", response_model=LegacyResponse[dict])
async def legacy_system_config(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin)):
    return _legacy({"id": 0, "name": "", "adminLogo": "", "customerLogo": "", "bindingDomain": "", "customerWords": "", "customerServicePhone": None, "customerServiceWechat": None, "customerServiceWechatQrCode": None, "whetherOpenLink": False})


@legacy_router.get("/commonadmin/api/system/getTenantAuth", response_model=LegacyResponse[dict])
async def legacy_tenant_auth(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    sms = await admin_home.sms_statistics(db)
    return _legacy({"id": 0, "name": "", "alias": "", "whetherLock": admin.account.status != 1, "grantAuthDeadline": None, "smsLocked": False, "smsSurplusNum": sms.remaining_count, "realNameSurplusNum": 0, "signSurplusNum": 0, "maritalStatusSurplusNum": 0, "whetherProtect": False, "whetherOpenLink": False})


@legacy_router.get("/commonadmin/api/adminUser/info", response_model=LegacyResponse[dict])
async def legacy_admin_info(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin)):
    return _legacy({"id": admin.account.id, "account": admin.account.username, "name": admin.account.display_name, "groupId": 0, "permissions": sorted(admin.permissions), "whetherLock": admin.account.status != 1, "whetherOrdinaryPage": False})


@legacy_router.get("/commonadmin/api/system/getTenantData", response_model=LegacyResponse[dict])
async def legacy_tenant_data(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    dashboard = await admin_home.dashboard(db, admin, date.today().replace(day=1), date.today())
    m = dashboard.metrics
    previous_month_end = date.today().replace(day=1) - timedelta(days=1)
    previous_month = await admin_home.dashboard(db, admin, previous_month_end.replace(day=1), previous_month_end)
    current_income = sum((item.net_amount for item in dashboard.trends), start=0)
    previous_income = sum((item.net_amount for item in previous_month.trends), start=0)
    return _legacy({"tenantId": 1, "regUserNums": m.member_count, "loveUserNums": m.member_count, "totalIncome": float(m.online_income), "curMonthIncome": float(current_income), "lastMonthIncome": float(previous_income), "detailsViews": 0, "indexViews": 0, "lastLoginTime": None, "wechatFans": 0})


@legacy_router.get("/commonadmin/api/system/getIndexTopStatistics", response_model=LegacyResponse[dict])
async def legacy_top_statistics(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    dashboard = await admin_home.dashboard(db, admin, date.today(), date.today())
    return _legacy({"activeSignUpAuditingNum": 0, "finCashoutAuditingNum": dashboard.metrics.pending_withdrawal_count, "giftExchangeAuditingNum": 0, "onlineDays": 0, "onlineIncome": float(dashboard.metrics.online_income), "regUserNum": dashboard.metrics.member_count, "shortVideoAuditingNum": 0, "wechatFansNum": 0})


@legacy_router.get("/loveadmin/api/loveUser/getAdminIndexStatistic", response_model=LegacyResponse[dict])
async def legacy_member_statistics(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    m = (await admin_home.dashboard(db, admin, date.today(), date.today())).metrics
    return _legacy({"appointmentAuditingNum": 0, "commitmentAuditingNum": 0, "educationAuditingNum": 0, "femaleNums": 0, "houseAuditingNum": 0, "lineAuditingNum": 0, "loveCustomerNums": m.lead_count, "loveUserAuditingNum": 0, "maleNums": 0, "matchmakerNums": m.matchmaker_count, "noSingleNums": 0, "offlineIncome": float(m.offline_income), "offlineVipNums": 0, "otherAuditingNum": 0, "popMatchmakerNums": 0, "popularizeAuditingNum": 0, "reportAuditingNum": 0, "total": m.member_count, "vipNums": m.vip_count})


@legacy_router.get("/commonadmin/api/system/getIncomeRank", response_model=LegacyResponse[list[dict]])
async def legacy_income_rank(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(text("""SELECT product_type, COALESCE(SUM(amount), 0) income
        FROM payment_order WHERE status = 1 GROUP BY product_type ORDER BY income DESC, product_type LIMIT 5"""))
    result = rows.mappings().all()
    total = sum((float(row["income"] or 0) for row in result), 0.0)
    return _legacy([{"serviceType": row["product_type"], "serviceTypeCode": 0, "income": float(row["income"] or 0), "proportion": round(float(row["income"] or 0) * 100 / total, 2) if total else 0} for row in result])


@legacy_router.get("/loveadmin/api/loveUser/getAdminIndexLoveUserStatisticByDay", response_model=LegacyResponse[list[dict]])
async def legacy_member_trend(page: int = Query(1, ge=1), limit: int = Query(15, ge=1, le=366), createFromTime: datetime | None = None, createToTime: datetime | None = None, admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    end = createToTime.date() if createToTime else date.today()
    start = createFromTime.date() if createFromTime else end - timedelta(days=limit - 1)
    report = await admin_home.dashboard(db, admin, start, end)
    records = [{"date": str(item.date), "memberCount": item.member_count, "leadCount": item.lead_count} for item in report.trends]
    return _legacy(records[(page - 1) * limit: page * limit])


@legacy_router.get("/commonadmin/api/finOrder/getOrderStatics", response_model=LegacyResponse[dict])
async def legacy_order_statistics(dateStatisticType: str = Query("Day", pattern="^(Day|Month)$"), whetherDesc: bool = False, page: int = Query(1, ge=1), limit: int = Query(15, ge=1, le=366), fromTime: datetime | None = None, endTime: datetime | None = None, admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    end = endTime.date() if endTime else date.today()
    start = fromTime.date() if fromTime else end - timedelta(days=limit - 1)
    report = await admin_home.dashboard(db, admin, start, end)
    records = _order_records(report, dateStatisticType)
    if whetherDesc:
        records.reverse()
    return _legacy(_legacy_page(records, page, limit))


@legacy_router.get("/commonadmin/api/adv/getPlatformCategoryList", response_model=LegacyResponse[list[dict]])
async def legacy_categories(type: str = Query("Guides", pattern="^Guides$"), whetherOpen: bool = True, admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    categories = await admin_home.academy_categories(db, whetherOpen)
    def item(x): return {"id": x.id, "parentId": x.parent_id or 0, "name": x.name, "desc": x.description, "image": x.image, "categoryType": x.category_type, "sort": x.sort, "whetherOpen": x.enabled, "whetherMatchmakerClassOpen": x.matchmaker_class_enabled, "secondDicCategoryList": [item(c) for c in x.children]}
    return _legacy([item(x) for x in categories])


@legacy_router.get("/commonadmin/api/recharge/getFinItems", response_model=LegacyResponse[list[dict]])
async def legacy_recharge_items(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return _legacy([{"id": x.id, "name": x.name, "type": x.resource_type, "numTimes": x.quantity, "price": float(x.price)} for x in await admin_home.recharge_items(db)])


@legacy_router.get("/commonadmin/api/sms/getStastics", response_model=LegacyResponse[dict])
async def legacy_sms_statistics(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    sms = await admin_home.sms_statistics(db)
    return _legacy({"sendSucess": sms.success_count, "sendFail": sms.failed_count, "smsSurplusNum": sms.remaining_count})


@legacy_router.get("/commonadmin/api/feedback/checkFeedbackWhetherView", response_model=LegacyResponse[bool])
async def legacy_feedback(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return _legacy(await admin_home.has_unread_feedback(db, admin.account.id))


async def _unread_count(db: AsyncSession, account_id: int) -> int:
    return int((await db.execute(text("""SELECT COUNT(*) FROM admin_announcement a
        LEFT JOIN admin_announcement_read r ON r.announcement_id = a.id AND r.account_id = :id
        WHERE a.published_at IS NOT NULL AND r.id IS NULL"""), {"id": account_id})).scalar() or 0)


@legacy_router.get("/commonadmin/api/mUpdRep/getUnreadNum", response_model=LegacyResponse[int])
async def legacy_unread_count(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return _legacy(await _unread_count(db, admin.account.id))


@legacy_router.get("/commonadmin/api/mUpdRep/getWhetherNewReport", response_model=LegacyResponse[bool])
async def legacy_has_unread(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return _legacy((await _unread_count(db, admin.account.id)) > 0)


@legacy_router.get("/commonadmin/api/mUpdRep/getFirstVersion", response_model=LegacyResponse[dict | None])
async def legacy_first_version(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("SELECT id, name FROM admin_announcement_version WHERE published_at IS NOT NULL ORDER BY is_first DESC, published_at, id LIMIT 1"))).mappings().first()
    return _legacy({"id": row["id"], "isFirst": True, "name": row["name"]} if row else None)


@legacy_router.get("/commonadmin/api/mUpdRep/getAllVersions", response_model=LegacyResponse[list[dict]])
async def legacy_versions(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(text("SELECT id, name, is_first FROM admin_announcement_version WHERE published_at IS NOT NULL ORDER BY published_at, id"))
    return _legacy([{"id": x["id"], "isFirst": bool(x["is_first"]), "name": x["name"]} for x in rows.mappings().all()])


@legacy_router.get("/commonadmin/api/mUpdRep/pageUpdReps", response_model=LegacyResponse[dict])
async def legacy_announcements(page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=100), category: str | None = Query(None, max_length=64), updRepTitleOrId: str | None = Query(None, max_length=100), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    result = await admin_home.announcements(db, admin, page, limit, category or None, updRepTitleOrId or None)
    records = [{"id": x.id, "versionId": x.version_id, "category": x.category, "title": x.title, "titleColor": x.title_color, "titleBold": x.title_bold, "top": x.top, "intOrder": x.sort_order, "linkTo": x.link_to, "createTime": x.created_at, "whetherRead": x.read} for x in result.items]
    return _legacy({"current": page, "size": limit, "total": result.total, "pages": (result.total + limit - 1) // limit, "records": records, "countId": None, "maxLimit": None, "optimizeCountSql": True, "orders": [], "searchCount": True})
