"""Typed contracts for target-specific paper-plane profile unlocks."""

from datetime import datetime

from pydantic import BaseModel, Field


class PaperPlaneProfileUnlockResponse(BaseModel):
    target_user_id: int = Field(ge=1)
    unlocked: bool
    points_cost: int = Field(default=80, ge=0)
    balance: int = Field(ge=0)
    unlocked_at: datetime | None = None
    # Deliberately privacy-filtered. Contact details are never part of this API.
    profile: dict[str, object] | None = None

