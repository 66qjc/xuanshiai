"""注册意图与红娘申请业务。"""

import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser
from app.schemas.auth import MatchmakerApplicationCreate, MatchmakerReviewRequest, RegistrationIntentUpdate

INTENT_OPTIONS = {
    "self_match": ("自己找", "以本人交友和婚恋匹配为主要目的"),
    "parent_match": ("父母帮找", "由本人授权父母参与资料和匹配流程"),
    "companion": ("找搭子", "以兴趣、活动和同城搭子为主要目的"),
}

APPLICATION_OPTIONS = {
    "promoter": "推广红娘",
    "partner": "合伙人招募",
    "service_matchmaker": "服务红娘",
}


async def update_registration_intent(
    db: AsyncSession, user_id: int, request: RegistrationIntentUpdate
) -> dict[str, str]:
    label, description = INTENT_OPTIONS[request.intent_type]
    await db.execute(
        text("""INSERT INTO user_registration_intent
            (user_id, intent_type, source, version, status, selected_at, revoked_at)
            VALUES (:user_id, :intent_type, :source, 'v1', 1, UTC_TIMESTAMP(), NULL)
            ON DUPLICATE KEY UPDATE intent_type = VALUES(intent_type), source = VALUES(source),
              version = VALUES(version), status = 1, selected_at = UTC_TIMESTAMP(), revoked_at = NULL"""),
        {"user_id": user_id, "intent_type": request.intent_type, "source": request.source},
    )
    await db.commit()
    return {"intent_type": request.intent_type, "label": label, "description": description}


async def get_registration_intent(db: AsyncSession, user_id: int) -> dict[str, str] | None:
    result = await db.execute(
        text("SELECT intent_type FROM user_registration_intent WHERE user_id = :user_id AND status = 1"),
        {"user_id": user_id},
    )
    intent_type = result.scalar()
    if not intent_type or intent_type not in INTENT_OPTIONS:
        return None
    label, description = INTENT_OPTIONS[intent_type]
    return {"intent_type": intent_type, "label": label, "description": description}


async def _notify_matchmaker_review(
    db: AsyncSession, user_id: int, application_id: int, status: int, reason: str | None
) -> None:
    title = {1: "红娘申请已通过", 2: "红娘申请未通过", 3: "红娘服务已暂停"}[status]
    content = {
        1: "你的服务红娘申请已通过审核。",
        2: f"你的服务红娘申请未通过审核：{reason or '请补充材料后重新提交'}",
        3: f"你的红娘服务已暂停：{reason or '请联系平台客服'}",
    }[status]
    await db.execute(text("""INSERT INTO user_notification
        (user_id, notification_type, title, content, related_id, is_read)
        VALUES (:user_id, :notification_type, :title, :content, :related_id, 0)"""), {
        "user_id": user_id,
        "notification_type": "matchmaker_application_reviewed",
        "title": title,
        "content": content,
        "related_id": application_id,
    })


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def application_response(row: Any) -> dict[str, Any]:
    cert_images = row["cert_images"] or []
    if isinstance(cert_images, str):
        try:
            cert_images = json.loads(cert_images)
        except json.JSONDecodeError:
            cert_images = []
    application_details = row.get("application_details") or {}
    if isinstance(application_details, str):
        try:
            application_details = json.loads(application_details)
        except json.JSONDecodeError:
            application_details = {}
    return {
        "id": row["id"],
        "application_type": row["application_type"],
        "status": row["status"],
        "real_name": row["real_name"],
        "phone_masked": mask_phone(row["phone"]),
        "intro": row["intro"],
        "cert_images": cert_images,
        "application_details": application_details,
        "fail_reason": row["fail_reason"],
        "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else str(row["created_at"]),
        "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
    }


async def create_matchmaker_application(
    db: AsyncSession, current: CurrentUser, request: MatchmakerApplicationCreate
) -> dict[str, Any]:
    if not current.phone or current.realname_status != 2:
        raise HTTPException(403, detail="申请红娘或合伙人前必须绑定手机号并完成实名认证")
    result = await db.execute(
        text("SELECT id, status FROM user_matchmaker_apply WHERE user_id = :user_id AND application_type = :application_type"),
        {"user_id": current.id, "application_type": request.application_type},
    )
    existing = result.mappings().first()
    if existing and existing["status"] in (0, 1, 3):
        raise HTTPException(409, detail="该类型申请已存在或正在生效")
    if existing and existing["status"] == 2:
        await db.execute(text("""UPDATE user_matchmaker_apply SET real_name = :real_name, phone = :phone,
            intro = :intro, cert_images = :cert_images, application_details = :application_details,
            status = 0, fail_reason = NULL,
            reviewed_by = NULL, reviewed_at = NULL, suspended_at = NULL, suspension_reason = NULL,
            updated_at = UTC_TIMESTAMP() WHERE id = :id"""), {
                **request.model_dump(exclude={"application_details"}),
                "cert_images": json.dumps(request.cert_images, ensure_ascii=False),
                "application_details": request.application_details.model_dump_json(),
                "id": existing["id"],
            })
    else:
        await db.execute(text("""INSERT INTO user_matchmaker_apply
            (user_id, application_type, real_name, phone, intro, cert_images, application_details, status)
            VALUES (:user_id, :application_type, :real_name, :phone, :intro, :cert_images, :application_details, 0)"""), {
                "user_id": current.id,
                **request.model_dump(exclude={"application_details"}),
                "cert_images": json.dumps(request.cert_images, ensure_ascii=False),
                "application_details": request.application_details.model_dump_json(),
            })
    await db.commit()
    result = await db.execute(text("""SELECT id, application_type, status, real_name, phone, intro,
        cert_images, application_details, fail_reason, created_at, reviewed_at FROM user_matchmaker_apply
        WHERE user_id = :user_id AND application_type = :application_type"""),
        {"user_id": current.id, "application_type": request.application_type})
    return application_response(result.mappings().one())


async def list_my_applications(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    result = await db.execute(text("""SELECT id, application_type, status, real_name, phone, intro,
        cert_images, application_details, fail_reason, created_at, reviewed_at FROM user_matchmaker_apply
        WHERE user_id = :user_id ORDER BY created_at DESC"""), {"user_id": user_id})
    return [application_response(row) for row in result.mappings().all()]


async def review_matchmaker_application(
    db: AsyncSession, admin_id: int, application_id: int, request: MatchmakerReviewRequest
) -> dict[str, Any]:
    result = await db.execute(text("SELECT * FROM user_matchmaker_apply WHERE id = :id FOR UPDATE"), {"id": application_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="申请不存在")
    if row["status"] not in (0, 3):
        raise HTTPException(409, detail="当前申请状态不能审核")
    await db.execute(text("""UPDATE user_matchmaker_apply SET status = :status, fail_reason = :reason,
        reviewed_by = :admin_id, reviewed_at = UTC_TIMESTAMP(),
        suspended_at = CASE WHEN :status = 3 THEN UTC_TIMESTAMP() ELSE NULL END,
        suspension_reason = CASE WHEN :status = 3 THEN :reason ELSE NULL END,
        updated_at = UTC_TIMESTAMP() WHERE id = :id"""),
        {"status": request.status, "reason": request.fail_reason, "admin_id": admin_id, "id": application_id})
    role_code = row["application_type"]
    if request.status == 1:
        await db.execute(text("""INSERT INTO user_role (user_id, role_code, status, granted_by)
            VALUES (:user_id, :role_code, 1, :admin_id)
            ON DUPLICATE KEY UPDATE status = 1, granted_by = :admin_id, revoked_at = NULL, revoke_reason = NULL"""),
            {"user_id": row["user_id"], "role_code": role_code, "admin_id": admin_id})
    elif request.status in (2, 3):
        await db.execute(text("""UPDATE user_role SET status = CASE WHEN :status = 3 THEN 2 ELSE 3 END,
            revoked_at = UTC_TIMESTAMP(), revoke_reason = :reason
            WHERE user_id = :user_id AND role_code = :role_code"""),
            {"status": request.status, "reason": request.fail_reason, "user_id": row["user_id"], "role_code": role_code})
    await _notify_matchmaker_review(db, int(row["user_id"]), application_id, request.status, request.fail_reason)
    await db.commit()
    result = await db.execute(text("""SELECT id, application_type, status, real_name, phone, intro,
        cert_images, application_details, fail_reason, created_at, reviewed_at FROM user_matchmaker_apply WHERE id = :id"""), {"id": application_id})
    return application_response(result.mappings().one())
