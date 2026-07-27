"""Paid matchmaker products, orders, service delivery and requests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser
from app.schemas.matchmaker import (
    MatchmakerAdminServiceRequestUpdate,
    MatchmakerCard,
    MatchmakerPage,
    MatchmakerContactResponse,
    MatchmakerContactExchangeCreate,
    MatchmakerContactExchangeContactsResponse,
    MatchmakerContactExchangeResponse,
    MatchmakerContactExchangeUpdate,
    MatchmakerContactUpdate,
    MatchmakerServiceOrderCreate,
    MatchmakerServiceOrderResponse,
    MatchmakerServiceProductCreate,
    MatchmakerServiceProductResponse,
    MatchmakerServiceProductUpdate,
    MatchmakerServiceRequestCreate,
    MatchmakerServiceRequestPage,
    MatchmakerServiceRequestResponse,
    MatchmakerServiceRequestUpdate,
)
from app.services.social import ensure_users_can_interact


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _card(row: Any) -> MatchmakerCard:
    return MatchmakerCard(
        user_id=int(row["user_id"]),
        nickname=row["nickname"],
        avatar=row["avatar"],
        application_type="service_matchmaker",
        intro=row["intro"] or "",
        certification_tags=["平台认证"],
        success_count=int(row["success_count"] or 0),
        rating_score=float(row["rating_score"] or 0),
        rating_count=int(row["rating_count"] or 0),
        is_available=bool(row["role_status"] == 1),
    )


async def list_matchmakers(
    db: AsyncSession, page: int, page_size: int, ranking: bool = False
) -> MatchmakerPage:
    order = "success_count DESC, rating_score DESC, app.reviewed_at DESC, app.id DESC" if ranking else "app.reviewed_at DESC, app.id DESC"
    params = {"limit": page_size, "offset": (page - 1) * page_size}
    query = text(f"""SELECT app.user_id, u.nickname, u.avatar, app.intro, app.cert_images,
        COALESCE(service_stats.success_count, 0) AS success_count,
        COALESCE(rating_stats.rating_score, 0) AS rating_score,
        COALESCE(rating_stats.rating_count, 0) AS rating_count,
        role.status AS role_status
        FROM user_matchmaker_apply app
        JOIN users u ON u.id = app.user_id AND u.status = 1
        JOIN user_role role ON role.user_id = app.user_id
          AND role.role_code = 'service_matchmaker' AND role.status = 1
        LEFT JOIN (SELECT matchmaker_id, COUNT(*) AS success_count
          FROM matchmaker_service WHERE status = 2 GROUP BY matchmaker_id) service_stats
          ON service_stats.matchmaker_id = app.user_id
        LEFT JOIN (SELECT matchmaker_id, AVG(score) AS rating_score, COUNT(*) AS rating_count
          FROM matchmaker_rating GROUP BY matchmaker_id) rating_stats
          ON rating_stats.matchmaker_id = app.user_id
        WHERE app.application_type = 'service_matchmaker' AND app.status = 1
        ORDER BY {order}
        LIMIT :limit OFFSET :offset""")
    result = await db.execute(query, params)
    count = await db.execute(text("""SELECT COUNT(*) FROM user_matchmaker_apply app
        JOIN users u ON u.id = app.user_id AND u.status = 1
        JOIN user_role role ON role.user_id = app.user_id
          AND role.role_code = 'service_matchmaker' AND role.status = 1
        WHERE app.application_type = 'service_matchmaker' AND app.status = 1"""))
    total = int(count.scalar() or 0)
    items = [_card(row) for row in result.mappings().all()]
    return MatchmakerPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def get_matchmaker(db: AsyncSession, matchmaker_id: int) -> MatchmakerCard:
    result = await db.execute(text("""SELECT app.user_id, u.nickname, u.avatar, app.intro, app.cert_images,
        COALESCE(service_stats.success_count, 0) AS success_count,
        COALESCE(rating_stats.rating_score, 0) AS rating_score,
        COALESCE(rating_stats.rating_count, 0) AS rating_count,
        role.status AS role_status
        FROM user_matchmaker_apply app
        JOIN users u ON u.id = app.user_id AND u.status = 1
        JOIN user_role role ON role.user_id = app.user_id
          AND role.role_code = 'service_matchmaker' AND role.status = 1
        LEFT JOIN (SELECT matchmaker_id, COUNT(*) AS success_count
          FROM matchmaker_service WHERE status = 2 GROUP BY matchmaker_id) service_stats
          ON service_stats.matchmaker_id = app.user_id
        LEFT JOIN (SELECT matchmaker_id, AVG(score) AS rating_score, COUNT(*) AS rating_count
          FROM matchmaker_rating GROUP BY matchmaker_id) rating_stats
          ON rating_stats.matchmaker_id = app.user_id
        WHERE app.user_id = :matchmaker_id AND app.application_type = 'service_matchmaker'
          AND app.status = 1"""), {"matchmaker_id": matchmaker_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="服务红娘不存在或暂不可用")
    return _card(row)


def _service_response(row: Any) -> MatchmakerServiceRequestResponse:
    return MatchmakerServiceRequestResponse(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        matchmaker_id=int(row["matchmaker_id"]) if row["matchmaker_id"] is not None else None,
        service_type=int(row["service_type"]),
        status=int(row["status"]),
        order_id=int(row["order_id"] or 0),
        product_id=int(row["product_id"] or 0),
        requirement=row["requirement"] or "",
        feedback=row["feedback"],
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        start_at=_datetime(row["start_at"]) if row["start_at"] else None,
        end_at=_datetime(row["end_at"]) if row["end_at"] else None,
    )


SERVICE_SELECT = """SELECT id, user_id, matchmaker_id, service_type, status, requirement,
    order_id, product_id, feedback, created_at, updated_at, start_at, end_at FROM matchmaker_service"""


async def _notify(db: AsyncSession, user_id: int, notification_type: str, title: str, content: str, related_id: int) -> None:
    await db.execute(text("""INSERT INTO user_notification
        (user_id, notification_type, title, content, related_id, is_read)
        VALUES (:user_id, :notification_type, :title, :content, :related_id, 0)"""), {
        "user_id": user_id, "notification_type": notification_type,
        "title": title, "content": content, "related_id": related_id,
    })


def _product_response(row: Any) -> MatchmakerServiceProductResponse:
    return MatchmakerServiceProductResponse(
        id=int(row["id"]), code=row["code"], name=row["name"],
        service_type=int(row["service_type"]), price=Decimal(str(row["price"])),
        description=row["description"], active=bool(row["status"] == 1),
        created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
    )


def _order_response(row: Any) -> MatchmakerServiceOrderResponse:
    return MatchmakerServiceOrderResponse(
        id=int(row["id"]), order_no=row["order_no"], user_id=int(row["user_id"]),
        matchmaker_id=int(row["matchmaker_id"]), product_id=int(row["service_product_id"]),
        service_type=int(row["service_type"]), product_name=row["product_name"],
        amount=Decimal(str(row["amount"])), status=int(row["status"]),
        service_request_id=int(row["service_request_id"]) if row["service_request_id"] else None,
        created_at=_datetime(row["created_at"]),
        pay_time=_datetime(row["pay_time"]) if row["pay_time"] else None,
    )


def _exchange_response(row: Any) -> MatchmakerContactExchangeResponse:
    values = dict(row)
    for field in ("source_consented_at", "target_consented_at", "delivered_at", "hidden_at", "created_at", "updated_at"):
        if values[field]:
            values[field] = _datetime(values[field])
    return MatchmakerContactExchangeResponse(**values)


async def list_service_products(db: AsyncSession) -> list[MatchmakerServiceProductResponse]:
    result = await db.execute(text("""SELECT id, code, name, service_type, price, description,
        status, created_at, updated_at FROM matchmaker_service_product
        WHERE status = 1 AND service_type IN (1, 3) ORDER BY id"""))
    return [_product_response(row) for row in result.mappings().all()]


async def admin_create_service_product(
    db: AsyncSession, admin_id: int, request: MatchmakerServiceProductCreate
) -> MatchmakerServiceProductResponse:
    exists = await db.execute(text("SELECT id FROM matchmaker_service_product WHERE code = :code"), {"code": request.code})
    if exists.scalar():
        raise HTTPException(409, detail="红娘服务商品编码已存在")
    result = await db.execute(text("""INSERT INTO matchmaker_service_product
        (code, name, service_type, price, description, status, created_by)
        VALUES (:code, :name, :service_type, :price, :description, :status, :admin_id)"""), {
        "code": request.code, "name": request.name, "service_type": request.service_type,
        "price": request.price, "description": request.description,
        "status": 1 if request.active else 2, "admin_id": admin_id,
    })
    await db.commit()
    row = (await db.execute(text("""SELECT id, code, name, service_type, price, description,
        status, created_at, updated_at FROM matchmaker_service_product WHERE id = :id"""), {"id": result.lastrowid})).mappings().one()
    return _product_response(row)


async def admin_update_service_product(
    db: AsyncSession, product_id: int, request: MatchmakerServiceProductUpdate
) -> MatchmakerServiceProductResponse:
    current = await db.execute(text("""SELECT id FROM matchmaker_service_product
        WHERE id = :id FOR UPDATE"""), {"id": product_id})
    if not current.scalar():
        raise HTTPException(404, detail="红娘服务商品不存在")
    await db.execute(text("""UPDATE matchmaker_service_product SET
        name = COALESCE(:name, name),
        service_type = COALESCE(:service_type, service_type),
        price = COALESCE(:price, price),
        description = COALESCE(:description, description),
        status = COALESCE(:status, status),
        updated_at = UTC_TIMESTAMP()
        WHERE id = :id"""), {
        "id": product_id, "name": request.name, "service_type": request.service_type,
        "price": request.price, "description": request.description,
        "status": None if request.active is None else (1 if request.active else 2),
    })
    await db.commit()
    row = (await db.execute(text("""SELECT id, code, name, service_type, price, description,
        status, created_at, updated_at FROM matchmaker_service_product WHERE id = :id"""), {"id": product_id})).mappings().one()
    return _product_response(row)


async def create_service_order(
    db: AsyncSession, current: CurrentUser, request: MatchmakerServiceOrderCreate, idempotency_key: str
) -> MatchmakerServiceOrderResponse:
    if current.realname_status != 2:
        raise HTTPException(403, detail="提交牵线申请前必须完成实名认证")
    if request.matchmaker_id == current.id:
        raise HTTPException(422, detail="不能向自己提交牵线申请")
    target = await db.execute(text("""SELECT app.user_id FROM user_matchmaker_apply app
        JOIN users u ON u.id = app.user_id AND u.status = 1
        JOIN user_role role ON role.user_id = app.user_id
          AND role.role_code = 'service_matchmaker' AND role.status = 1
        WHERE app.user_id = :matchmaker_id AND app.application_type = 'service_matchmaker'
          AND app.status = 1"""), {"matchmaker_id": request.matchmaker_id})
    if not target.scalar():
        raise HTTPException(404, detail="服务红娘不存在或暂不可用")
    await ensure_users_can_interact(db, current.id, request.matchmaker_id)
    product = await db.execute(text("""SELECT id, name, service_type, price FROM matchmaker_service_product
        WHERE id = :id AND status = 1 AND service_type IN (1, 3)"""), {"id": request.product_id})
    product_row = product.mappings().first()
    if not product_row:
        raise HTTPException(404, detail="红娘服务商品不存在或已下架")
    existing = await db.execute(text("""SELECT id, order_no, user_id, matchmaker_id,
        service_product_id, service_type, product_name, amount, status, service_request_id,
        created_at, pay_time FROM payment_order
        WHERE user_id = :user_id AND type = 3 AND idempotency_key = :key LIMIT 1"""), {
        "user_id": current.id, "key": idempotency_key,
    })
    existing_row = existing.mappings().first()
    if existing_row:
        return _order_response(existing_row)
    order_no = f"XM{datetime.utcnow():%Y%m%d%H%M%S}{current.id:08d}{secrets.token_hex(4)}"
    try:
        result = await db.execute(text("""INSERT INTO payment_order
            (user_id, order_no, type, product_id, product_type, product_name, amount,
             service_product_id, matchmaker_id, service_requirement, idempotency_key, status, expire_at)
            VALUES (:user_id, :order_no, 3, :product_id, 3, :product_name, :amount,
             :service_product_id, :matchmaker_id, :requirement, :idempotency_key, 0,
             DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 MINUTE))"""), {
            "user_id": current.id, "order_no": order_no, "product_id": request.product_id,
            "product_name": product_row["name"], "amount": product_row["price"],
            "service_product_id": request.product_id, "matchmaker_id": request.matchmaker_id,
            "requirement": request.requirement, "idempotency_key": idempotency_key,
        })
    except IntegrityError:
        await db.rollback()
        existing = await db.execute(text("""SELECT id, order_no, user_id, matchmaker_id,
            service_product_id, sp.service_type, po.product_name, po.amount, po.status,
            po.service_request_id, po.created_at, po.pay_time
            FROM payment_order po JOIN matchmaker_service_product sp ON sp.id = po.service_product_id
            WHERE po.user_id = :user_id AND po.type = 3 AND po.idempotency_key = :key LIMIT 1"""), {
            "user_id": current.id, "key": idempotency_key,
        })
        existing_row = existing.mappings().first()
        if not existing_row:
            raise
        return _order_response(existing_row)
    await db.commit()
    created = await db.execute(text("""SELECT po.id, po.order_no, po.user_id, po.matchmaker_id,
        po.service_product_id, sp.service_type, po.product_name, po.amount, po.status,
        po.service_request_id, po.created_at, po.pay_time
        FROM payment_order po JOIN matchmaker_service_product sp ON sp.id = po.service_product_id
        WHERE po.id = :id"""), {"id": result.lastrowid})
    return _order_response(created.mappings().one())


async def get_service_order(db: AsyncSession, current: CurrentUser, order_no: str) -> MatchmakerServiceOrderResponse:
    result = await db.execute(text("""SELECT po.id, po.order_no, po.user_id, po.matchmaker_id,
        po.service_product_id, sp.service_type, po.product_name, po.amount, po.status,
        po.service_request_id, po.created_at, po.pay_time
        FROM payment_order po JOIN matchmaker_service_product sp ON sp.id = po.service_product_id
        WHERE po.order_no = :order_no AND po.user_id = :user_id"""), {"order_no": order_no, "user_id": current.id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="红娘服务订单不存在")
    return _order_response(row)


async def activate_paid_service_order(db: AsyncSession, order_id: int) -> int:
    order_result = await db.execute(text("""SELECT po.id, po.user_id, po.matchmaker_id,
        po.service_product_id, po.service_request_id, po.service_requirement,
        po.status, sp.service_type FROM payment_order po
        JOIN matchmaker_service_product sp ON sp.id = po.service_product_id
        WHERE po.id = :id FOR UPDATE"""), {"id": order_id})
    order = order_result.mappings().first()
    if not order or order["service_product_id"] is None:
        raise HTTPException(422, detail="订单不是红娘服务订单")
    if order["status"] != 1:
        raise HTTPException(409, detail="支付成功后才能开通红娘服务")
    if order["service_request_id"]:
        return int(order["service_request_id"])
    target = await db.execute(text("""SELECT 1 FROM user_matchmaker_apply app
        JOIN users u ON u.id = app.user_id AND u.status = 1
        JOIN user_role role ON role.user_id = app.user_id
          AND role.role_code = 'service_matchmaker' AND role.status = 1
        WHERE app.user_id = :matchmaker_id AND app.application_type = 'service_matchmaker'
          AND app.status = 1"""), {"matchmaker_id": order["matchmaker_id"]})
    if not target.scalar():
        raise HTTPException(409, detail="支付订单对应的服务红娘已不可用")
    result = await db.execute(text("""INSERT INTO matchmaker_service
        (user_id, matchmaker_id, service_type, status, order_id, product_id, requirement)
        VALUES (:user_id, :matchmaker_id, :service_type, 0, :order_id, :product_id, :requirement)"""), {
        "user_id": order["user_id"], "matchmaker_id": order["matchmaker_id"],
        "service_type": order["service_type"], "order_id": order["id"],
        "product_id": order["service_product_id"], "requirement": order["service_requirement"] or "",
    })
    service_id = int(result.lastrowid)
    await db.execute(text("UPDATE payment_order SET service_request_id = :service_id WHERE id = :id"), {"service_id": service_id, "id": order_id})
    await _notify(db, int(order["matchmaker_id"]), "matchmaker_service_request", "收到新的付费红娘服务", "有用户购买了你的红娘服务", service_id)
    return service_id


async def create_service_request(
    db: AsyncSession, current: CurrentUser, request: MatchmakerServiceRequestCreate
) -> MatchmakerServiceRequestResponse:
    order_result = await db.execute(text("""SELECT id, user_id, status, service_request_id,
        service_requirement FROM payment_order WHERE order_no = :order_no FOR UPDATE"""), {"order_no": request.order_no})
    order = order_result.mappings().first()
    if not order or int(order["user_id"]) != current.id:
        raise HTTPException(404, detail="红娘服务订单不存在")
    if order["status"] != 1:
        raise HTTPException(409, detail="红娘服务订单尚未支付成功")
    if order["service_requirement"] and order["service_requirement"] != request.requirement:
        raise HTTPException(409, detail="服务需求与订单需求不一致，请重新提交")
    if not order["service_requirement"]:
        await db.execute(text("""UPDATE payment_order SET service_requirement = :requirement
            WHERE id = :id"""), {"requirement": request.requirement, "id": order["id"]})
    await activate_paid_service_order(db, int(order["id"]))
    await db.commit()
    created = await db.execute(text(f"{SERVICE_SELECT} WHERE order_id = :order_id"), {"order_id": order["id"]})
    return _service_response(created.mappings().one())


async def set_matchmaker_contact(
    db: AsyncSession, current: CurrentUser, service_id: int, request: MatchmakerContactUpdate
) -> MatchmakerContactResponse:
    service = await db.execute(text("""SELECT id, matchmaker_id, status FROM matchmaker_service
        WHERE id = :id FOR UPDATE"""), {"id": service_id})
    row = service.mappings().first()
    if not row:
        raise HTTPException(404, detail="红娘服务不存在")
    if row["matchmaker_id"] != current.id:
        raise HTTPException(403, detail="只有被分配的服务红娘可以提交微信")
    if row["status"] == 3:
        raise HTTPException(409, detail="已取消或退款的服务不能交付联系方式")
    await db.execute(text("""INSERT INTO matchmaker_service_contact
        (service_id, matchmaker_id, wechat_contact)
        VALUES (:service_id, :matchmaker_id, :wechat_contact)
        ON DUPLICATE KEY UPDATE wechat_contact = VALUES(wechat_contact),
            delivered_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()"""), {
        "service_id": service_id, "matchmaker_id": current.id,
        "wechat_contact": request.wechat_contact,
    })
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'matchmaker_contact.deliver', 'matchmaker_service', :service_id, '红娘提交服务微信')"""), {
        "actor_id": current.id, "service_id": service_id,
    })
    await db.commit()
    result = await db.execute(text("""SELECT service_id, matchmaker_id, wechat_contact, delivered_at
        FROM matchmaker_service_contact WHERE service_id = :service_id"""), {"service_id": service_id})
    return MatchmakerContactResponse(**dict(result.mappings().one()))


async def get_matchmaker_contact(
    db: AsyncSession, current: CurrentUser, service_id: int
) -> MatchmakerContactResponse:
    service = await db.execute(text("""SELECT id, user_id, status FROM matchmaker_service
        WHERE id = :id"""), {"id": service_id})
    row = service.mappings().first()
    if not row or row["user_id"] != current.id:
        raise HTTPException(404, detail="红娘服务不存在")
    if row["status"] == 3:
        raise HTTPException(409, detail="已取消或退款的服务不能查看红娘微信")
    result = await db.execute(text("""SELECT service_id, matchmaker_id, wechat_contact, delivered_at
        FROM matchmaker_service_contact WHERE service_id = :service_id"""), {"service_id": service_id})
    contact = result.mappings().first()
    if not contact:
        raise HTTPException(409, detail="红娘尚未提交微信联系方式")
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'matchmaker_contact.view', 'matchmaker_service', :service_id, '用户查看红娘服务微信')"""), {
        "actor_id": current.id, "service_id": service_id,
    })
    await db.commit()
    return MatchmakerContactResponse(**dict(contact))


async def create_contact_exchange(
    db: AsyncSession, current: CurrentUser, service_id: int, request: MatchmakerContactExchangeCreate
) -> MatchmakerContactExchangeResponse:
    service_result = await db.execute(text("""SELECT id, user_id, matchmaker_id, status
        FROM matchmaker_service WHERE id = :id FOR UPDATE"""), {"id": service_id})
    service = service_result.mappings().first()
    if not service or service["matchmaker_id"] != current.id:
        raise HTTPException(404, detail="服务不存在或不属于当前红娘")
    if service["status"] not in (1, 2):
        raise HTTPException(409, detail="当前服务状态不能发起联系方式交换")
    if request.target_user_id == service["user_id"]:
        raise HTTPException(422, detail="不能将服务用户作为联系方式交换对象")
    await ensure_users_can_interact(db, int(service["user_id"]), request.target_user_id)
    await ensure_users_can_interact(db, current.id, request.target_user_id)
    target = await db.execute(text("SELECT id FROM users WHERE id = :id AND status = 1"), {"id": request.target_user_id})
    if not target.scalar():
        raise HTTPException(404, detail="联系方式交换对象不存在或不可用")
    existing = await db.execute(text("""SELECT id, service_id, source_user_id, target_user_id,
        status, source_consented_at, target_consented_at, delivered_at, hidden_at, hidden_reason,
        created_at, updated_at FROM matchmaker_contact_exchange
        WHERE service_id = :service_id AND source_user_id = :source_id AND target_user_id = :target_id
        FOR UPDATE"""), {"service_id": service_id, "source_id": service["user_id"], "target_id": request.target_user_id})
    row = existing.mappings().first()
    if row:
        return _exchange_response(row)
    result = await db.execute(text("""INSERT INTO matchmaker_contact_exchange
        (service_id, source_user_id, target_user_id, status)
        VALUES (:service_id, :source_id, :target_id, 'PENDING')"""), {
        "service_id": service_id, "source_id": service["user_id"], "target_id": request.target_user_id,
    })
    exchange_id = int(result.lastrowid)
    await _notify(db, int(request.target_user_id), "matchmaker_contact_exchange", "收到联系方式交换申请", "红娘邀请你授权交换联系方式", exchange_id)
    await db.commit()
    result = await db.execute(text("""SELECT id, service_id, source_user_id, target_user_id,
        status, source_consented_at, target_consented_at, delivered_at, hidden_at, hidden_reason,
        created_at, updated_at FROM matchmaker_contact_exchange WHERE id = :id"""), {"id": exchange_id})
    return _exchange_response(result.mappings().one())


async def update_contact_exchange(
    db: AsyncSession, current: CurrentUser, exchange_id: int, request: MatchmakerContactExchangeUpdate
) -> MatchmakerContactExchangeResponse:
    result = await db.execute(text("""SELECT id, service_id, source_user_id, target_user_id,
        status, source_consented_at, target_consented_at, delivered_at, hidden_at, hidden_reason,
        created_at, updated_at FROM matchmaker_contact_exchange WHERE id = :id FOR UPDATE"""), {"id": exchange_id})
    row = result.mappings().first()
    if not row or current.id not in (row["source_user_id"], row["target_user_id"]):
        raise HTTPException(404, detail="联系方式交换不存在或无权操作")
    if request.action == "REVOKE":
        if row["status"] in ("DELIVERED", "HIDDEN"):
            raise HTTPException(409, detail="已交付的联系方式不能删除历史事实")
        await db.execute(text("""UPDATE matchmaker_contact_exchange SET status = 'REVOKED',
            hidden_at = UTC_TIMESTAMP(), hidden_reason = '用户撤回授权', updated_at = UTC_TIMESTAMP()
            WHERE id = :id"""), {"id": exchange_id})
    else:
        column = "source_consented_at" if current.id == row["source_user_id"] else "target_consented_at"
        await db.execute(text(f"""UPDATE matchmaker_contact_exchange SET {column} = COALESCE({column}, UTC_TIMESTAMP()),
            updated_at = UTC_TIMESTAMP()
            WHERE id = :id AND status NOT IN ('REVOKED', 'HIDDEN', 'DELIVERED')"""), {"id": exchange_id})
        consented = await db.execute(text("""SELECT source_consented_at, target_consented_at
            FROM matchmaker_contact_exchange WHERE id = :id FOR UPDATE"""), {"id": exchange_id})
        consented_row = consented.mappings().one()
        if consented_row["source_consented_at"] and consented_row["target_consented_at"]:
            await db.execute(text("""UPDATE matchmaker_contact_exchange SET status = 'DELIVERED',
                delivered_at = COALESCE(delivered_at, UTC_TIMESTAMP()), updated_at = UTC_TIMESTAMP()
                WHERE id = :id"""), {"id": exchange_id})
        else:
            await db.execute(text("""UPDATE matchmaker_contact_exchange SET status = 'ONE_SIDE_CONSENT',
                updated_at = UTC_TIMESTAMP() WHERE id = :id AND status NOT IN ('REVOKED', 'HIDDEN')"""), {"id": exchange_id})
    await db.commit()
    result = await db.execute(text("""SELECT id, service_id, source_user_id, target_user_id,
        status, source_consented_at, target_consented_at, delivered_at, hidden_at, hidden_reason,
        created_at, updated_at FROM matchmaker_contact_exchange WHERE id = :id"""), {"id": exchange_id})
    return _exchange_response(result.mappings().one())


async def get_contact_exchange(
    db: AsyncSession, current: CurrentUser, exchange_id: int
) -> MatchmakerContactExchangeResponse:
    result = await db.execute(text("""SELECT id, service_id, source_user_id, target_user_id,
        status, source_consented_at, target_consented_at, delivered_at, hidden_at, hidden_reason,
        created_at, updated_at FROM matchmaker_contact_exchange
        WHERE id = :id AND (source_user_id = :user_id OR target_user_id = :user_id)"""), {
        "id": exchange_id, "user_id": current.id,
    })
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="联系方式交换不存在或无权查看")
    return _exchange_response(row)


async def get_contact_exchange_contacts(
    db: AsyncSession, current: CurrentUser, exchange_id: int
) -> MatchmakerContactExchangeContactsResponse:
    result = await db.execute(text("""SELECT e.id, e.source_user_id, e.target_user_id, e.status,
        e.delivered_at, source.phone AS source_phone, target.phone AS target_phone
        FROM matchmaker_contact_exchange e
        JOIN users source ON source.id = e.source_user_id
        JOIN users target ON target.id = e.target_user_id
        WHERE e.id = :id AND (e.source_user_id = :user_id OR e.target_user_id = :user_id)"""), {
        "id": exchange_id, "user_id": current.id,
    })
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="联系方式交换不存在或无权查看")
    if row["status"] != "DELIVERED" or not row["delivered_at"]:
        raise HTTPException(409, detail="双方尚未完成联系方式授权")
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'matchmaker_contact_exchange.view', 'matchmaker_contact_exchange', :exchange_id,
            '用户查看已授权联系方式')"""), {"actor_id": current.id, "exchange_id": exchange_id})
    await db.commit()
    return MatchmakerContactExchangeContactsResponse(
        exchange_id=int(row["id"]), source_user_id=int(row["source_user_id"]),
        target_user_id=int(row["target_user_id"]), source_phone=row["source_phone"],
        target_phone=row["target_phone"], delivered_at=_datetime(row["delivered_at"]),
    )


async def list_service_requests(
    db: AsyncSession, current: CurrentUser, page: int, page_size: int, assigned: bool = False
) -> MatchmakerServiceRequestPage:
    if assigned:
        role = await db.execute(text("""SELECT 1 FROM user_role WHERE user_id = :user_id
            AND role_code = 'service_matchmaker' AND status = 1 LIMIT 1"""), {"user_id": current.id})
        if not role.scalar():
            raise HTTPException(403, detail="当前用户不是有效服务红娘")
        where, params = "matchmaker_id = :user_id", {"user_id": current.id}
    else:
        where, params = "user_id = :user_id", {"user_id": current.id}
    result = await db.execute(text(f"{SERVICE_SELECT} WHERE {where} ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"), {**params, "limit": page_size, "offset": (page - 1) * page_size})
    count = await db.execute(text(f"SELECT COUNT(*) FROM matchmaker_service WHERE {where}"), params)
    total = int(count.scalar() or 0)
    items = [_service_response(row) for row in result.mappings().all()]
    return MatchmakerServiceRequestPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def update_service_request(
    db: AsyncSession, current: CurrentUser, service_id: int, request: MatchmakerServiceRequestUpdate
) -> MatchmakerServiceRequestResponse:
    row_result = await db.execute(text(f"{SERVICE_SELECT} WHERE id = :id FOR UPDATE"), {"id": service_id})
    row = row_result.mappings().first()
    if not row:
        raise HTTPException(404, detail="牵线申请不存在")
    if row["matchmaker_id"] != current.id:
        raise HTTPException(403, detail="只有被分配的服务红娘可以处理申请")
    allowed_transitions = {0: {1, 3}, 1: {2, 3}}
    if request.status not in allowed_transitions.get(int(row["status"]), set()):
        raise HTTPException(409, detail="当前牵线申请状态不能继续处理")
    start_at = "start_at = COALESCE(start_at, UTC_TIMESTAMP())," if request.status == 1 else ""
    end_at = "end_at = UTC_TIMESTAMP()," if request.status in (2, 3) else ""
    await db.execute(text(f"""UPDATE matchmaker_service SET status = :status,
        feedback = :feedback, {start_at} {end_at} updated_at = UTC_TIMESTAMP()
        WHERE id = :id"""), {"status": request.status, "feedback": request.feedback, "id": service_id})
    await _notify(db, row["user_id"], "matchmaker_service_updated", "牵线服务状态更新", "你的牵线申请状态已更新", service_id)
    await db.commit()
    updated = await db.execute(text(f"{SERVICE_SELECT} WHERE id = :id"), {"id": service_id})
    return _service_response(updated.mappings().one())


async def admin_list_service_requests(db: AsyncSession, page: int, page_size: int, status: int | None) -> MatchmakerServiceRequestPage:
    where = "WHERE 1=1"
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    if status is not None:
        where += " AND status = :status"
        params["status"] = status
    result = await db.execute(text(f"{SERVICE_SELECT} {where} ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) FROM matchmaker_service {where}"), {key: value for key, value in params.items() if key == "status"})
    total = int(count.scalar() or 0)
    items = [_service_response(row) for row in result.mappings().all()]
    return MatchmakerServiceRequestPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def admin_update_service_request(db: AsyncSession, admin_id: int, service_id: int, request: MatchmakerAdminServiceRequestUpdate) -> MatchmakerServiceRequestResponse:
    row_result = await db.execute(text(f"{SERVICE_SELECT} WHERE id = :id FOR UPDATE"), {"id": service_id})
    row = row_result.mappings().first()
    if not row:
        raise HTTPException(404, detail="牵线申请不存在")
    if request.matchmaker_id is not None:
        target = await db.execute(text("""SELECT 1 FROM user_role WHERE user_id = :user_id
            AND role_code = 'service_matchmaker' AND status = 1 LIMIT 1"""), {"user_id": request.matchmaker_id})
        if not target.scalar():
            raise HTTPException(422, detail="只能分配给有效服务红娘")
    updates: list[str] = []
    params: dict[str, Any] = {"id": service_id}
    if request.matchmaker_id is not None:
        updates.append("matchmaker_id = :matchmaker_id")
        params["matchmaker_id"] = request.matchmaker_id
    if request.status is not None:
        allowed_transitions = {0: {1, 3}, 1: {2, 3}}
        if request.status not in allowed_transitions.get(int(row["status"]), set()):
            raise HTTPException(409, detail="当前牵线申请状态不能继续变更")
        updates.append("status = :status")
        params["status"] = request.status
        if request.status == 1:
            updates.append("start_at = COALESCE(start_at, UTC_TIMESTAMP())")
        if request.status in (2, 3):
            updates.append("end_at = UTC_TIMESTAMP()")
    if request.feedback is not None:
        updates.append("feedback = :feedback")
        params["feedback"] = request.feedback
    updates.extend(["updated_at = UTC_TIMESTAMP()"])
    await db.execute(text(f"UPDATE matchmaker_service SET {', '.join(updates)} WHERE id = :id"), params)
    await _notify(db, request.matchmaker_id or row["matchmaker_id"] or row["user_id"], "matchmaker_service_admin_updated", "牵线申请已更新", "管理员更新了牵线申请", service_id)
    await db.commit()
    updated = await db.execute(text(f"{SERVICE_SELECT} WHERE id = :id"), {"id": service_id})
    return _service_response(updated.mappings().one())
