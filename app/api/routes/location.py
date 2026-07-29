"""Authenticated location-sharing and nearby-user endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.location import LocationResponse, LocationSharingRequest, LocationUpdateRequest, NearbyUserResponse
from app.services.location import get_location, nearby_users, set_location_sharing, update_location

router = APIRouter(prefix="/users/me/location")
users_router = APIRouter(prefix="/users")


@router.post("", response_model=LocationResponse, summary="上报当前位置")
async def update_my_location(body: LocationUpdateRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> LocationResponse:
    return await update_location(db, current.id, body)


@router.put("/sharing", response_model=LocationResponse, summary="开启或关闭位置共享")
async def update_location_sharing(body: LocationSharingRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> LocationResponse:
    return await set_location_sharing(db, current.id, body)


@router.get("", response_model=LocationResponse, summary="查询我的位置共享状态")
async def my_location(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> LocationResponse:
    return await get_location(db, current.id)


@router.delete("", response_model=LocationResponse, summary="关闭位置共享")
async def delete_my_location(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> LocationResponse:
    return await set_location_sharing(db, current.id, LocationSharingRequest(enabled=False))


@users_router.get("/nearby", response_model=NearbyUserResponse, summary="查询附近在线用户")
async def nearby(latitude: float = Query(..., ge=-90, le=90), longitude: float = Query(..., ge=-180, le=180),
                 radius_km: float = Query(20, gt=0, le=100), limit: int = Query(100, ge=1, le=200),
                 current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> NearbyUserResponse:
    return await nearby_users(db, current.id, latitude, longitude, radius_km, limit)
