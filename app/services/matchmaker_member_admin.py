"""Member administration services backed by existing user tables."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.matchmaker_member_admin import (
    CertificationDetail,
    CertificationMaterial,
    MatchmakerMemberAdminItem,
    MatchmakerMemberCreate,
    MatchmakerMemberUpdate,
    MemberAuditLogItem,
)


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "***"


async def _member(db: AsyncSession, member_id: int) -> MatchmakerMemberAdminItem:
    row = (await db.execute(text("""SELECT u.id, u.nickname, u.phone, u.gender, u.status,
        u.created_at, u.updated_at, a.matchmaker_id,
        v.vip_end_at,
        CASE WHEN v.user_id IS NULL OR (v.vip_end_at IS NOT NULL AND v.vip_end_at <= UTC_TIMESTAMP())
             THEN 0 ELSE 1 END AS is_vip
        FROM users u
        LEFT JOIN (SELECT user_id, MAX(end_at) vip_end_at FROM user_membership
            WHERE status = 1 GROUP BY user_id) v ON v.user_id = u.id
        LEFT JOIN (SELECT user_id, matchmaker_id FROM resource_assignment
            WHERE status = 1) a ON a.user_id = u.id
        WHERE u.id = :id"""), {"id": member_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="会员不存在")
    return MatchmakerMemberAdminItem(
        id=int(row["id"]),
        nickname=row["nickname"],
        phone_masked=_mask_phone(row["phone"]),
        gender=row["gender"],
        status=int(row["status"]),
        is_vip=bool(row["is_vip"]),
        vip_end_at=row["vip_end_at"],
        matchmaker_id=row["matchmaker_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_member(db: AsyncSession, body: MatchmakerMemberCreate, actor_id: int) -> MatchmakerMemberAdminItem:
    duplicate = await db.execute(text("SELECT id FROM users WHERE phone = :phone"), {"phone": body.phone})
    if duplicate.scalar():
        raise HTTPException(409, detail="手机号已注册")
    result = await db.execute(text("""INSERT INTO users
        (phone, nickname, gender, birthday, is_married, avatar, status)
        VALUES (:phone, :nickname, :gender, :birthday, :is_married, :avatar, 1)"""), {
        **body.model_dump(exclude={"remark"}), "phone": body.phone,
    })
    member_id = int(result.lastrowid)
    await db.execute(text("""INSERT INTO matchmaker_admin_member_note
        (user_id, note, updated_by) VALUES (:user_id, :note, :updated_by)"""), {
        "user_id": member_id, "note": body.remark, "updated_by": actor_id,
    })
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'member.create', 'user', :resource_id, :reason)"""), {
        "actor": actor_id, "resource_id": member_id, "reason": body.remark,
    })
    await db.commit()
    return await _member(db, member_id)


async def update_member(db: AsyncSession, member_id: int, body: MatchmakerMemberUpdate, actor_id: int) -> MatchmakerMemberAdminItem:
    await _member(db, member_id)
    values = body.model_dump(exclude_unset=True)
    remark = values.pop("remark", None)
    user_values = {key: values.pop(key) for key in ("nickname", "gender", "birthday", "is_married", "avatar") if key in values}
    if user_values:
        assignments = ", ".join(f"{key} = :{key}" for key in user_values)
        await db.execute(text(f"UPDATE users SET {assignments}, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {
            **user_values, "id": member_id,
        })
    if values:
        columns = ["user_id", *values]
        updates = ", ".join(f"{key} = VALUES({key})" for key in values)
        await db.execute(text(f"""INSERT INTO user_profile ({', '.join(columns)})
            VALUES ({', '.join(':' + key for key in columns)})
            ON DUPLICATE KEY UPDATE {updates}"""), {"user_id": member_id, **values})
    if remark is not None:
        await db.execute(text("""INSERT INTO matchmaker_admin_member_note (user_id, note, updated_by)
            VALUES (:user_id, :note, :updated_by)
            ON DUPLICATE KEY UPDATE note = VALUES(note), updated_by = VALUES(updated_by),
            updated_at = UTC_TIMESTAMP()"""), {"user_id": member_id, "note": remark, "updated_by": actor_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'member.update', 'user', :resource_id, :reason)"""), {
        "actor": actor_id, "resource_id": member_id, "reason": remark,
    })
    await db.commit()
    return await _member(db, member_id)


async def certification_detail(db: AsyncSession, member_id: int, kind: str) -> CertificationDetail:
    if kind == "marriage":
        row = (await db.execute(text(
            "SELECT id, is_married, updated_at FROM users WHERE id = :id"
        ), {"id": member_id})).mappings().first()
        if not row:
            await _member(db, member_id)
        return CertificationDetail(
            user_id=member_id,
            kind=kind,
            status=1 if row["is_married"] else 0,
            submitted_at=None,
            reviewed_at=row["updated_at"],
            fail_reason=None,
            value=str(row["is_married"]) if row["is_married"] else None,
            material_urls=[],
            reviewer_id=None,
            audit_history=[],
        )

    fields = {
        "education": ("education_verified", "education", "education_cert", "fail_reason", "created_at", "updated_at"),
        "house": ("house_verified", "house_cert", "house_cert", "fail_reason", "created_at", "updated_at"),
    }
    if kind not in fields:
        raise HTTPException(422, detail="不支持的认证类型")
    status_field, value_field, material_field, fail_field, submitted_field, reviewed_field = fields[kind]
    row = (await db.execute(text(f"""SELECT ua.user_id, ua.{status_field} AS status,
        ua.{value_field} AS value, ua.{material_field} AS material,
        ua.{fail_field} AS fail_reason, ua.{submitted_field} AS submitted_at,
        ua.{reviewed_field} AS reviewed_at
        FROM user_auth ua WHERE ua.user_id = :id"""), {"id": member_id})).mappings().first()
    if not row:
        await _member(db, member_id)
        return CertificationDetail(
            user_id=member_id, kind=kind, status=0, submitted_at=None, reviewed_at=None,
            fail_reason=None, value=None, material_urls=[], reviewer_id=None, audit_history=[],
        )
    material_urls = []
    if row["material"]:
        material_urls = [CertificationMaterial(id=0, url=str(row["material"]), thumbnail_url=None, expires_at=None)]
    return CertificationDetail(
        user_id=member_id, kind=kind, status=int(row["status"] or 0),
        submitted_at=row["submitted_at"], reviewed_at=row["reviewed_at"],
        fail_reason=row["fail_reason"], value=row["value"],
        material_urls=material_urls, reviewer_id=None, audit_history=[],
    )


async def member_audit_logs(db: AsyncSession, member_id: int) -> list[MemberAuditLogItem]:
    await _member(db, member_id)
    result = await db.execute(text("""SELECT id, action, resource_type, resource_id, reason, created_at
        FROM business_audit_log WHERE resource_type = 'user' AND resource_id = :id
        ORDER BY id DESC LIMIT 200"""), {"id": member_id})
    return [MemberAuditLogItem(**dict(row)) for row in result.mappings().all()]
