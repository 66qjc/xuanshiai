"""Read-only, data-scope-aware administrator home-page queries."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin
from app.schemas.admin_home import (
    AcademyCategory, AdminBootstrap, AdminDashboard, AnnouncementItem, AnnouncementPage,
    DailyTrend, DashboardGender, DashboardMetrics, DashboardPending, HomeAuthorization,
    HomeHeader, HomeOperator, IncomeRankItem, RechargeItem, SmsStatistics,
)


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _scope_condition(admin: CurrentMatchmakerAdmin, user_column: str) -> tuple[str, dict[str, object]]:
    """Return a parameterized resource-assignment predicate for an admin data scope."""
    account = admin.account
    if account.data_scope == "ALL":
        return "1 = 1", {}
    if account.data_scope == "SELF":
        if account.matchmaker_user_id is None:
            return "1 = 0", {}
        return ("EXISTS (SELECT 1 FROM resource_assignment scope_assignment "
                f"WHERE scope_assignment.user_id = {user_column} "
                "AND scope_assignment.matchmaker_id = :scope_matchmaker_id "
                "AND scope_assignment.status = 1)"), {"scope_matchmaker_id": account.matchmaker_user_id}
    if account.organization_id is None:
        return "1 = 0", {}
    return ("EXISTS (SELECT 1 FROM resource_assignment scope_assignment "
            f"WHERE scope_assignment.user_id = {user_column} AND scope_assignment.status = 1 "
            "AND scope_assignment.organization_id IN (SELECT id FROM organization "
            "WHERE id = :scope_organization_id OR parent_id = :scope_organization_id))"), {
        "scope_organization_id": account.organization_id,
    }


def _lead_scope_condition(admin: CurrentMatchmakerAdmin) -> tuple[str, dict[str, object]]:
    account = admin.account
    if account.data_scope == "ALL":
        return "1 = 1", {}
    if account.data_scope == "SELF":
        if account.matchmaker_user_id is None:
            return "1 = 0", {}
        return "customer_lead.matchmaker_id = :scope_matchmaker_id", {"scope_matchmaker_id": account.matchmaker_user_id}
    if account.organization_id is None:
        return "1 = 0", {}
    return ("customer_lead.organization_id IN (SELECT id FROM organization WHERE id = :scope_organization_id "
            "OR parent_id = :scope_organization_id)"), {"scope_organization_id": account.organization_id}


async def sms_statistics(db: AsyncSession) -> SmsStatistics:
    row = (await db.execute(text("""SELECT COALESCE(SUM(success_count), 0) success_count,
        COALESCE(SUM(failed_count), 0) failed_count, COALESCE(SUM(remaining_count), 0) remaining_count
        FROM admin_sms_statistics WHERE tenant_id = 1"""))).mappings().one()
    return SmsStatistics(**{key: int(row[key] or 0) for key in SmsStatistics.model_fields})


async def has_unread_feedback(db: AsyncSession, account_id: int) -> bool:
    result = await db.execute(text("""SELECT 1 FROM admin_feedback_message message
        LEFT JOIN admin_feedback_read read_state ON read_state.feedback_id = message.id
            AND read_state.account_id = :account_id
        WHERE message.tenant_id = 1 AND message.sender_type IN ('SERVICE', 'SYSTEM') AND read_state.id IS NULL
          AND message.id = (SELECT latest.id FROM admin_feedback_message latest
              WHERE latest.ticket_id = message.ticket_id ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)
        LIMIT 1"""), {"account_id": account_id})
    return result.scalar() is not None


async def _unread_announcements(db: AsyncSession, account_id: int) -> int:
    return int((await db.execute(text("""SELECT COUNT(*) FROM admin_announcement announcement
        LEFT JOIN admin_announcement_read read_state ON read_state.announcement_id = announcement.id
            AND read_state.account_id = :account_id
        WHERE announcement.published_at IS NOT NULL AND read_state.id IS NULL AND announcement.tenant_id = 1"""), {
        "account_id": account_id,
    })).scalar() or 0)


async def bootstrap(db: AsyncSession, admin: CurrentMatchmakerAdmin) -> AdminBootstrap:
    sms = await sms_statistics(db)
    return AdminBootstrap(
        operator=HomeOperator(id=admin.account.id, account=admin.account.username,
            name=admin.account.display_name, permissions=sorted(admin.permissions), locked=admin.account.status != 1),
        authorization=HomeAuthorization(sms_remaining_count=sms.remaining_count),
        header=HomeHeader(has_unread_feedback=await has_unread_feedback(db, admin.account.id),
            unread_announcement_count=await _unread_announcements(db, admin.account.id), sms=sms),
    )


async def dashboard(db: AsyncSession, admin: CurrentMatchmakerAdmin, from_date: date, to_date: date) -> AdminDashboard:
    if to_date < from_date or (to_date - from_date).days > 365:
        raise HTTPException(422, detail="统计日期范围必须为 1 至 366 天")
    user_scope, scope_params = _scope_condition(admin, "users.id")
    membership_scope, _ = _scope_condition(admin, "user_membership.user_id")
    membership_scope_alias = membership_scope.replace("user_membership.user_id", "membership.user_id")
    order_scope, _ = _scope_condition(admin, "payment_order.user_id")
    withdrawal_scope, _ = _scope_condition(admin, "withdrawal_request.account_id")
    matchmaker_scope, _ = _scope_condition(admin, "user_matchmaker_apply.user_id")
    lead_scope, _ = _lead_scope_condition(admin)
    row = (await db.execute(text(f"""SELECT
        (SELECT COUNT(*) FROM users WHERE status = 1 AND {user_scope}) member_count,
        (SELECT COUNT(*) FROM users WHERE status = 1 AND {user_scope}) platform_user_count,
        (SELECT COUNT(*) FROM users WHERE status = 1 AND openid IS NOT NULL AND openid <> '' AND {user_scope}) wechat_fan_count,
        (SELECT COUNT(DISTINCT DATE(last_login_at)) FROM users WHERE status = 1 AND last_login_at IS NOT NULL AND {user_scope}) online_days,
        (SELECT COUNT(*) FROM customer_lead WHERE {lead_scope}) lead_count,
        (SELECT COUNT(*) FROM customer_lead WHERE {lead_scope}) customer_lead_count,
        (SELECT COUNT(DISTINCT user_id) FROM user_membership WHERE status = 1 AND (end_at IS NULL OR end_at > UTC_TIMESTAMP()) AND {membership_scope}) vip_count,
        (SELECT COUNT(DISTINCT membership.user_id) FROM user_membership membership WHERE membership.status = 1 AND (membership.end_at IS NULL OR membership.end_at > UTC_TIMESTAMP()) AND NOT EXISTS (SELECT 1 FROM payment_order membership_order WHERE membership_order.order_no = membership.order_no AND membership_order.product_type = 'offline_vip') AND {membership_scope_alias}) online_vip_count,
        (SELECT COUNT(DISTINCT membership.user_id) FROM user_membership membership WHERE membership.status = 1 AND (membership.end_at IS NULL OR membership.end_at > UTC_TIMESTAMP()) AND EXISTS (SELECT 1 FROM payment_order membership_order WHERE membership_order.order_no = membership.order_no AND membership_order.product_type = 'offline_vip') AND {membership_scope_alias}) offline_vip_count,
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE application_type = 'service_matchmaker' AND status = 1 AND {matchmaker_scope}) matchmaker_count,
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE application_type = 'service_matchmaker' AND status = 1 AND {matchmaker_scope}) service_matchmaker_count,
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE application_type = 'promoter' AND status = 1 AND {matchmaker_scope}) promotion_matchmaker_count,
        (SELECT COUNT(*) FROM user_match matched_user WHERE matched_user.status IN (1, 2) AND matched_user.user_id < matched_user.target_user_id AND {user_scope.replace('users.id', 'matched_user.user_id')}) successful_match_count,
        (SELECT COUNT(*) FROM users WHERE status = 1 AND gender = 1 AND {user_scope}) male_member_count,
        (SELECT COUNT(*) FROM users WHERE status = 1 AND gender = 2 AND {user_scope}) female_member_count,
        (SELECT COUNT(*) FROM withdrawal_request WHERE status = 'PENDING_REVIEW' AND account_type = 'user' AND {withdrawal_scope}) pending_withdrawal_count,
        (SELECT COALESCE(SUM(amount), 0) FROM payment_order WHERE status = 1 AND (product_type IS NULL OR product_type <> 'offline_vip') AND {order_scope}) online_income,
        (SELECT COALESCE(SUM(amount), 0) FROM payment_order WHERE status = 1 AND product_type = 'offline_vip' AND {order_scope}) offline_income"""), scope_params)).mappings().one()
    metrics = DashboardMetrics(**{key: _money(row[key]) if "income" in key else int(row[key] or 0) for key in DashboardMetrics.model_fields})
    pending_row = (await db.execute(text(f"""SELECT
        (SELECT COUNT(*) FROM withdrawal_request WHERE status = 'PENDING_REVIEW' AND account_type = 'user' AND {withdrawal_scope}) withdrawal,
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE status = 0 AND {matchmaker_scope}) matchmaker_application,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 0 AND {user_scope.replace('users.id', 'matchmaker_service.user_id')}) matchmaker_service,
        (SELECT COUNT(*) FROM match_apply WHERE status = 0 AND {user_scope.replace('users.id', 'match_apply.to_user_id')}) match_application,
        (SELECT COUNT(*) FROM user_report WHERE status = 0 AND {user_scope.replace('users.id', 'user_report.target_user_id')}) report"""), scope_params)).mappings().one()
    pending = DashboardPending(**{key: int(pending_row[key] or 0) for key in DashboardPending.model_fields})
    gender = DashboardGender(male=metrics.male_member_count, female=metrics.female_member_count,
        unspecified=max(0, metrics.member_count - metrics.male_member_count - metrics.female_member_count))
    rank_rows = (await db.execute(text(f"""SELECT COALESCE(NULLIF(product_type, ''), 'unknown') product_type,
        COALESCE(SUM(amount), 0) income FROM payment_order WHERE status = 1 AND {order_scope}
        GROUP BY COALESCE(NULLIF(product_type, ''), 'unknown') ORDER BY income DESC, product_type LIMIT 5"""), scope_params)).mappings().all()
    rank_total = sum((_money(item["income"]) for item in rank_rows), Decimal("0.00"))
    income_rank = [IncomeRankItem(product_type=str(item["product_type"]), income=_money(item["income"]),
        proportion=(_money(item["income"]) * Decimal("100") / rank_total).quantize(Decimal("0.01")) if rank_total else Decimal("0.00")) for item in rank_rows]
    start = datetime.combine(from_date, time.min)
    end = datetime.combine(to_date + timedelta(days=1), time.min)
    rows = (await db.execute(text(f"""SELECT DATE(d) date, SUM(member_count) member_count, SUM(lead_count) lead_count,
        SUM(paid_count) paid_count, SUM(refund_count) refund_count, SUM(paid_amount) paid_amount, SUM(online_paid_amount) online_paid_amount, SUM(offline_paid_amount) offline_paid_amount, SUM(refund_amount) refund_amount FROM (
          SELECT DATE(created_at) d, COUNT(*) member_count, 0 lead_count, 0 paid_count, 0 refund_count, 0 paid_amount, 0 online_paid_amount, 0 offline_paid_amount, 0 refund_amount FROM users WHERE created_at >= :start AND created_at < :end AND {user_scope} GROUP BY DATE(created_at)
          UNION ALL SELECT DATE(created_at) d, 0, COUNT(*), 0, 0, 0, 0, 0, 0 FROM customer_lead WHERE created_at >= :start AND created_at < :end AND {lead_scope} GROUP BY DATE(created_at)
          UNION ALL SELECT DATE(pay_time) d, 0, 0, COUNT(*), 0, COALESCE(SUM(amount), 0), COALESCE(SUM(CASE WHEN product_type IS NULL OR product_type <> 'offline_vip' THEN amount ELSE 0 END), 0), COALESCE(SUM(CASE WHEN product_type = 'offline_vip' THEN amount ELSE 0 END), 0), 0 FROM payment_order WHERE status IN (1, 3) AND pay_time >= :start AND pay_time < :end AND {order_scope} GROUP BY DATE(pay_time)
          UNION ALL SELECT DATE(refund_time) d, 0, 0, 0, COUNT(*), 0, 0, 0, COALESCE(SUM(amount), 0) FROM payment_order WHERE status = 3 AND refund_time >= :start AND refund_time < :end AND {order_scope} GROUP BY DATE(refund_time)
        ) daily GROUP BY DATE(d)"""), {**scope_params, "start": start, "end": end})).mappings().all()
    values = {row["date"]: row for row in rows}
    trends: list[DailyTrend] = []
    current = from_date
    while current <= to_date:
        item = values.get(current, {})
        paid, refunded = _money(item.get("paid_amount")), _money(item.get("refund_amount"))
        trends.append(DailyTrend(date=current, member_count=int(item.get("member_count") or 0), lead_count=int(item.get("lead_count") or 0), paid_count=int(item.get("paid_count") or 0), completed_refund_count=int(item.get("refund_count") or 0), paid_amount=paid, online_paid_amount=_money(item.get("online_paid_amount")), offline_paid_amount=_money(item.get("offline_paid_amount")), completed_refund_amount=refunded, net_amount=paid - refunded))
        current += timedelta(days=1)
    return AdminDashboard(from_date=from_date, to_date=to_date, metrics=metrics, pending=pending,
        member_gender=gender, income_rank=income_rank, trends=trends)


async def announcements(db: AsyncSession, admin: CurrentMatchmakerAdmin, page: int, page_size: int, category: str | None, keyword: str | None) -> AnnouncementPage:
    where = ["announcement.published_at IS NOT NULL", "announcement.tenant_id = 1"]
    params: dict[str, object] = {"account_id": admin.account.id, "limit": page_size, "offset": (page - 1) * page_size}
    if category:
        where.append("announcement.category = :category")
        params["category"] = category
    if keyword:
        where.append("(announcement.title LIKE CONCAT('%', :keyword, '%') OR CAST(announcement.id AS CHAR) = :keyword)")
        params["keyword"] = keyword
    clause = " AND ".join(where)
    base = "FROM admin_announcement announcement LEFT JOIN admin_announcement_read read_state ON read_state.announcement_id = announcement.id AND read_state.account_id = :account_id"
    result = await db.execute(text(f"SELECT announcement.id, announcement.version_id, announcement.category, announcement.title, announcement.title_color, announcement.title_bold, announcement.top, announcement.sort_order, announcement.link_to, announcement.created_at, read_state.id read_id {base} WHERE {clause} ORDER BY announcement.top DESC, announcement.sort_order DESC, announcement.published_at DESC, announcement.id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) FROM admin_announcement announcement WHERE {clause}"), {key: value for key, value in params.items() if key not in {"account_id", "limit", "offset"}})
    items = [AnnouncementItem(**{**dict(row), "read": row["read_id"] is not None}) for row in result.mappings().all()]
    total = int(count.scalar() or 0)
    return AnnouncementPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def academy_categories(db: AsyncSession, enabled: bool | None = True) -> list[AcademyCategory]:
    where = ["category_type = 'Guides'", "tenant_id = 1"]
    params: dict[str, object] = {}
    if enabled is not None:
        where.append("enabled = :enabled")
        params["enabled"] = enabled
    rows = (await db.execute(text(f"SELECT id, parent_id, name, description, image, category_type, sort, enabled, matchmaker_class_enabled FROM admin_academy_category WHERE {' AND '.join(where)} ORDER BY sort, id"), params)).mappings().all()
    nodes = {int(row["id"]): AcademyCategory(**dict(row), children=[]) for row in rows}
    roots: list[AcademyCategory] = []
    for node in nodes.values():
        if node.parent_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


async def recharge_items(db: AsyncSession) -> list[RechargeItem]:
    rows = await db.execute(text("SELECT id, name, resource_type, quantity, price FROM admin_recharge_item WHERE enabled = 1 AND tenant_id = 1 ORDER BY sort, id"))
    return [RechargeItem(**dict(row)) for row in rows.mappings().all()]
