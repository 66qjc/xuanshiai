"""Location sharing, Redis GEO indexing and nearby-user discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.schemas.location import (
    LocationResponse,
    LocationSharingRequest,
    LocationUpdateRequest,
    NearbyUserItem,
    NearbyUserResponse,
)

LOCATION_GEO_KEY = "location:online:users"
LOCATION_TTL_SECONDS = 150
MAX_RADIUS_KM = 100.0
MAX_LIMIT = 200


def _blur_coordinate(value: float) -> float:
    """Expose a roughly 500m grid cell center instead of an exact position."""
    return round(round(value / 0.005) * 0.005, 5)


def _location_response(row: dict[str, Any] | None) -> LocationResponse:
    row = row or {}
    return LocationResponse(
        enabled=bool(row.get("location_visible") and row.get("location_consent")),
        latitude=float(row["latitude"]) if row.get("latitude") is not None else None,
        longitude=float(row["longitude"]) if row.get("longitude") is not None else None,
        accuracy_m=float(row["location_precision"]) if row.get("location_precision") is not None else None,
        updated_at=row.get("location_updated_at"),
    )


async def get_location(db: AsyncSession, user_id: int) -> LocationResponse:
    result = await db.execute(
        text("""SELECT latitude, longitude, location_precision, location_updated_at,
                      location_consent, location_visible
               FROM user_profile WHERE user_id = :user_id"""),
        {"user_id": user_id},
    )
    return _location_response(dict(result.mappings().first() or {}))


async def update_location(db: AsyncSession, user_id: int, request: LocationUpdateRequest) -> LocationResponse:
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.execute(
        text("""INSERT INTO user_profile
                    (user_id, latitude, longitude, location_source, location_updated_at,
                     location_precision, location_consent, location_visible)
               VALUES (:user_id, :latitude, :longitude, :source, :updated_at,
                       :accuracy_m, 1, 1)
               ON DUPLICATE KEY UPDATE latitude = VALUES(latitude),
                    longitude = VALUES(longitude), location_source = VALUES(location_source),
                    location_updated_at = VALUES(location_updated_at),
                    location_precision = VALUES(location_precision),
                    location_consent = 1, location_visible = 1"""),
        {"user_id": user_id, "latitude": request.latitude, "longitude": request.longitude,
         "source": request.source, "updated_at": now, "accuracy_m": request.accuracy_m},
    )
    await db.commit()
    try:
        await redis_client.geoadd(LOCATION_GEO_KEY, [request.longitude, request.latitude, str(user_id)])
        await redis_client.expire(LOCATION_GEO_KEY, LOCATION_TTL_SECONDS)
    except RedisError as exc:
        raise HTTPException(503, detail="位置服务暂时不可用") from exc
    return await get_location(db, user_id)


async def set_location_sharing(db: AsyncSession, user_id: int, request: LocationSharingRequest) -> LocationResponse:
    await db.execute(
        text("""UPDATE user_profile SET location_visible = :enabled,
                    location_consent = CASE WHEN :enabled = 1 THEN location_consent ELSE 0 END
               WHERE user_id = :user_id"""),
        {"user_id": user_id, "enabled": int(request.enabled)},
    )
    await db.commit()
    if not request.enabled:
        await remove_online_location(user_id)
    return await get_location(db, user_id)


async def remove_online_location(user_id: int) -> None:
    try:
        await redis_client.zrem(LOCATION_GEO_KEY, str(user_id))
    except RedisError:
        pass


async def nearby_users(db: AsyncSession, viewer_id: int, latitude: float, longitude: float,
                       radius_km: float = 20.0, limit: int = 100) -> NearbyUserResponse:
    try:
        matches = await redis_client.geosearch(
            LOCATION_GEO_KEY, longitude=longitude, latitude=latitude,
            radius=min(radius_km, MAX_RADIUS_KM), unit="km", withcoord=True,
            withdist=True, sort="ASC", count=min(limit, MAX_LIMIT),
        )
    except RedisError as exc:
        raise HTTPException(503, detail="位置服务暂时不可用") from exc

    candidates: list[tuple[int, float, float, float]] = []
    for item in matches:
        user_id = int(item[0])
        if user_id == viewer_id:
            continue
        coords = item[2]
        candidates.append((user_id, float(item[1]), float(coords[1]), float(coords[0])))
    if not candidates:
        return NearbyUserResponse(items=[], total=0, nearest_distance_km=None, radius_km=radius_km)

    params: dict[str, Any] = {"viewer_id": viewer_id}
    placeholders: list[str] = []
    for index, (user_id, _, _, _) in enumerate(candidates):
        key = f"candidate_{index}"
        placeholders.append(f":{key}")
        params[key] = user_id
    result = await db.execute(
        text(f"""SELECT u.id AS user_id, u.nickname, u.avatar, p.location_updated_at,
                         p.online_status, p.last_active_at,
                         COALESCE(pr.hide_distance, 0) AS hide_distance,
                         COALESCE(pr.hide_online_status, 0) AS hide_online_status,
                         COALESCE(pr.show_profile, 1) AS show_profile
                  FROM users u JOIN user_profile p ON p.user_id = u.id
                  LEFT JOIN user_privacy pr ON pr.user_id = u.id
                  WHERE u.id IN ({', '.join(placeholders)}) AND u.id <> :viewer_id
                    AND u.status = 1 AND COALESCE(p.location_consent, 0) = 1
                    AND COALESCE(p.location_visible, 0) = 1 AND COALESCE(p.online_status, 0) = 1
                    AND COALESCE(p.last_active_at, '1970-01-01') >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 SECOND)
                    AND COALESCE(pr.show_profile, 1) = 1
                    AND NOT EXISTS (SELECT 1 FROM user_block b
                                    WHERE (b.user_id = :viewer_id AND b.target_user_id = u.id)
                                       OR (b.user_id = u.id AND b.target_user_id = :viewer_id))"""),
        params,
    )
    rows = {int(row["user_id"]): dict(row) for row in result.mappings().all()}
    items: list[NearbyUserItem] = []
    for user_id, distance, lat, lon in candidates:
        row = rows.get(user_id)
        if not row:
            continue
        items.append(NearbyUserItem(
            user_id=user_id, nickname=row["nickname"], avatar=row["avatar"],
            latitude=_blur_coordinate(lat), longitude=_blur_coordinate(lon),
            distance_km=None if row["hide_distance"] else round(distance, 2),
            online=not bool(row["hide_online_status"]),
            location_updated_at=row["location_updated_at"],
        ))
    nearest = next((item.distance_km for item in items if item.distance_km is not None), None)
    return NearbyUserResponse(items=items, total=len(items), nearest_distance_km=nearest, radius_km=radius_km)
