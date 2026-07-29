"""Location sharing and nearby-user API models."""

from datetime import datetime

from pydantic import BaseModel, Field


class LocationUpdateRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=10000)
    source: str = Field(default="device", min_length=1, max_length=32)


class LocationSharingRequest(BaseModel):
    enabled: bool


class LocationResponse(BaseModel):
    enabled: bool
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None
    updated_at: datetime | None


class NearbyUserItem(BaseModel):
    user_id: int
    nickname: str | None
    avatar: str | None
    latitude: float
    longitude: float
    distance_km: float | None
    online: bool
    location_updated_at: datetime | None


class NearbyUserResponse(BaseModel):
    items: list[NearbyUserItem]
    total: int
    nearest_distance_km: float | None
    radius_km: float
