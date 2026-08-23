"""Community topic and banner operations for the back office."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.community import (
    CommunityBannerAdminCreate, CommunityBannerAdminResponse, CommunityBannerAdminUpdate,
    CommunityTopicAdminCreate, CommunityTopicAdminUpdate, CommunityTopicResponse,
)

router = APIRouter(prefix="/admin/community")


@router.get("/topics", response_model=list[CommunityTopicResponse])
async def topics(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text("SELECT id, name, icon, sort, is_active, created_at FROM community_topic ORDER BY sort DESC, id DESC"))).mappings().all()
    return [CommunityTopicResponse(**dict(row), post_count=0, participant_count=0, heat=0, joined=False) for row in rows]


@router.post("/topics", response_model=CommunityTopicResponse, status_code=201)
async def create_topic(body: CommunityTopicAdminCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    duplicate = await db.execute(text("SELECT id FROM community_topic WHERE name = :name"), {"name": body.name})
    if duplicate.scalar():
        raise HTTPException(409, detail="???????")
    result = await db.execute(text("INSERT INTO community_topic (name, icon, sort, is_active) VALUES (:name, :icon, :sort, :is_active)"), {**body.model_dump(), "is_active": int(body.is_active)})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor, 'community.topic.create', 'community_topic', :id)"), {"actor": current.account.id, "id": result.lastrowid})
    await db.commit()
    row = (await db.execute(text("SELECT id, name, icon, sort, is_active, created_at FROM community_topic WHERE id = :id"), {"id": result.lastrowid})).mappings().one()
    return CommunityTopicResponse(**dict(row), post_count=0, participant_count=0, heat=0, joined=False)


@router.patch("/topics/{topic_id}", response_model=CommunityTopicResponse)
async def update_topic(topic_id: int = Path(..., ge=1), body: CommunityTopicAdminUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT id FROM community_topic WHERE id = :id FOR UPDATE"), {"id": topic_id})
    values = body.model_dump()
    await db.execute(text("UPDATE community_topic SET name=:name, icon=:icon, sort=:sort, is_active=:is_active WHERE id=:id"), {**values, "is_active": int(body.is_active), "id": topic_id})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor, 'community.topic.update', 'community_topic', :id)"), {"actor": current.account.id, "id": topic_id})
    await db.commit()
    row = (await db.execute(text("SELECT id, name, icon, sort, is_active, created_at FROM community_topic WHERE id = :id"), {"id": topic_id})).mappings().first()
    if not row: raise HTTPException(404, detail="?????")
    return CommunityTopicResponse(**dict(row), post_count=0, participant_count=0, heat=0, joined=False)


@router.get("/banners", response_model=list[CommunityBannerAdminResponse])
async def banners(position: str | None = Query(None, max_length=32), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    where = "WHERE 1=1"; params = {}
    if position: where += " AND position=:position"; params["position"] = position
    rows = (await db.execute(text(f"SELECT id, title, image_url, link_type, link_value, sort, position, is_active, start_at, end_at FROM config_banner {where} ORDER BY sort DESC, id DESC"), params)).mappings().all()
    return [CommunityBannerAdminResponse(**dict(row)) for row in rows]


@router.post("/banners", response_model=CommunityBannerAdminResponse, status_code=201)
async def create_banner(body: CommunityBannerAdminCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""INSERT INTO config_banner (title, image_url, link_type, link_value, sort, position, is_active, start_at, end_at)
        VALUES (:title, :image_url, :link_type, :link_value, :sort, :position, :is_active, :start_at, :end_at)"""), {**body.model_dump(), "is_active": int(body.is_active)})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor, 'community.banner.create', 'config_banner', :id)"), {"actor": current.account.id, "id": result.lastrowid})
    await db.commit()
    row = (await db.execute(text("SELECT id, title, image_url, link_type, link_value, sort, position, is_active, start_at, end_at FROM config_banner WHERE id=:id"), {"id": result.lastrowid})).mappings().one()
    return CommunityBannerAdminResponse(**dict(row))


@router.patch("/banners/{banner_id}", response_model=CommunityBannerAdminResponse)
async def update_banner(banner_id: int = Path(..., ge=1), body: CommunityBannerAdminUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("UPDATE config_banner SET title=:title, image_url=:image_url, link_type=:link_type, link_value=:link_value, sort=:sort, position=:position, is_active=:is_active, start_at=:start_at, end_at=:end_at, updated_at=UTC_TIMESTAMP() WHERE id=:id"), {**body.model_dump(), "is_active": int(body.is_active), "id": banner_id})
    if result.rowcount == 0: raise HTTPException(404, detail="Banner???")
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor, 'community.banner.update', 'config_banner', :id)"), {"actor": current.account.id, "id": banner_id})
    await db.commit()
    row = (await db.execute(text("SELECT id, title, image_url, link_type, link_value, sort, position, is_active, start_at, end_at FROM config_banner WHERE id=:id"), {"id": banner_id})).mappings().one()
    return CommunityBannerAdminResponse(**dict(row))
