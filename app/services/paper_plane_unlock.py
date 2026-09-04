"""Server-authoritative paper-plane profile unlocks.

The small service protocol is intentionally store-based so the policy can be
tested without a database while the production store keeps debit and
entitlement writes in one transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.candidate_visibility import CandidateVisibilityService, VisibilityScene
from app.services.points import _balance

PAPER_PLANE_PROFILE_UNLOCK_COST = 80


class ProfileUnlockError(Exception):
    """Base error for the transport-independent unlock service."""


class ProfileUnlockConflict(ProfileUnlockError):
    pass


class ProfileUnlockForbidden(ProfileUnlockError):
    pass


class ProfileUnlockInsufficientPoints(ProfileUnlockError):
    pass


class ProfileUnlockNotFound(ProfileUnlockError):
    pass


class UnlockStore(Protocol):
    async def unlock_atomically(
        self, viewer_id: int, target_id: int, idempotency_key: str, points_cost: int
    ) -> dict[str, object]: ...


class PaperPlaneProfileUnlockService:
    def __init__(self, store: UnlockStore, points_cost: int = PAPER_PLANE_PROFILE_UNLOCK_COST) -> None:
        if points_cost != PAPER_PLANE_PROFILE_UNLOCK_COST:
            raise ValueError("paper-plane profile unlock cost is fixed at 80 points")
        self.store = store
        self.points_cost = points_cost

    async def unlock(self, viewer_id: int, target_id: int, idempotency_key: str) -> dict[str, object]:
        if viewer_id < 1 or target_id < 1:
            raise ProfileUnlockForbidden("invalid user")
        if not 1 <= len(idempotency_key) <= 128:
            raise ProfileUnlockConflict("invalid idempotency key")
        return await self.store.unlock_atomically(viewer_id, target_id, idempotency_key, self.points_cost)


# Short name retained for callers that model the feature as a paper-plane
# entitlement rather than a profile-specific service.
PaperPlaneUnlockService = PaperPlaneProfileUnlockService


class SqlAlchemyPaperPlaneUnlockStore:
    """MySQL store implementing visibility, idempotency and debit atomically."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.visibility = CandidateVisibilityService()

    async def _target_row(self, target_id: int) -> dict[str, object]:
        row = (
            await self.db.execute(
                text("SELECT id, status, deleted_at, nickname FROM users WHERE id = :id LIMIT 1"),
                {"id": target_id},
            )
        ).mappings().first()
        if not row:
            raise ProfileUnlockNotFound("目标用户不存在")
        if int(row["status"] or 0) != 1 or row.get("deleted_at") is not None:
            raise ProfileUnlockForbidden("目标用户当前不可见")
        return dict(row)

    async def ensure_target_visible(self, viewer_id: int, target_id: int) -> None:
        """Apply the same target and relationship gates used by POST."""
        await self._target_row(target_id)
        if viewer_id == target_id:
            raise ProfileUnlockForbidden("不能查看自己的纸飞机资料")
        decision = await self.visibility.decide(
            self.db, viewer_id, target_id, VisibilityScene.INTERACTION
        )
        if not decision.allowed:
            raise ProfileUnlockForbidden("目标用户当前不可见")

    async def unlock_atomically(
        self, viewer_id: int, target_id: int, idempotency_key: str, points_cost: int
    ) -> dict[str, object]:
        async with self.db.begin():
            # Lock the account row as a stable serialization point even when
            # the viewer has no existing user_points ledger row (zero balance).
            await self.db.execute(
                text("SELECT id FROM users WHERE id = :viewer FOR UPDATE"),
                {"viewer": viewer_id},
            )
            # A key is scoped to the viewer. Lock it before checking the target
            # so concurrent retries cannot debit twice or retarget a request.
            existing_key = (
                await self.db.execute(
                    text("""SELECT target_user_id, points_cost, unlocked_at
                           FROM paper_plane_profile_unlock
                           WHERE viewer_user_id = :viewer AND idempotency_key = :key
                           FOR UPDATE"""),
                    {"viewer": viewer_id, "key": idempotency_key},
                )
            ).mappings().first()
            if existing_key:
                if int(existing_key["target_user_id"]) != target_id:
                    raise ProfileUnlockConflict("幂等键已用于其他目标用户")
                balance = await _balance(self.db, viewer_id)
                return self._result(target_id, balance, existing_key["unlocked_at"], 0)

            await self._target_row(target_id)
            if viewer_id == target_id:
                raise ProfileUnlockForbidden("不能解锁自己的资料")

            decision = await self.visibility.decide(
                self.db, viewer_id, target_id, VisibilityScene.INTERACTION
            )
            if not decision.allowed:
                raise ProfileUnlockForbidden("目标用户当前不可见")

            # Lock the viewer's balance and the viewer-target entitlement row.
            entitlement = (
                await self.db.execute(
                    text("""SELECT target_user_id, points_cost, unlocked_at
                           FROM paper_plane_profile_unlock
                           WHERE viewer_user_id = :viewer AND target_user_id = :target
                           FOR UPDATE"""),
                    {"viewer": viewer_id, "target": target_id},
                )
            ).mappings().first()
            balance = await _balance(self.db, viewer_id, lock=True)
            if entitlement:
                return self._result(target_id, balance, entitlement["unlocked_at"], 0)
            if balance < points_cost:
                raise ProfileUnlockInsufficientPoints("积分余额不足")

            after = balance - points_cost
            await self.db.execute(
                text("""INSERT INTO user_points (user_id, type, amount, balance, `desc`)
                       VALUES (:user_id, 4, :amount, :balance, :description)"""),
                {
                    "user_id": viewer_id,
                    "amount": -points_cost,
                    "balance": after,
                    "description": "纸飞机解锁对方资料",
                },
            )
            await self.db.execute(
                text("""INSERT INTO paper_plane_profile_unlock
                       (viewer_user_id, target_user_id, points_cost, unlock_source,
                        idempotency_key, unlocked_at)
                       VALUES (:viewer, :target, :cost, 'paper_plane', :key, UTC_TIMESTAMP())"""),
                {
                    "viewer": viewer_id,
                    "target": target_id,
                    "cost": points_cost,
                    "key": idempotency_key,
                },
            )
            unlocked_at = (
                await self.db.execute(
                    text("""SELECT unlocked_at FROM paper_plane_profile_unlock
                           WHERE viewer_user_id = :viewer AND target_user_id = :target"""),
                    {"viewer": viewer_id, "target": target_id},
                )
            ).scalar_one()
            return self._result(target_id, after, unlocked_at, points_cost)

    @staticmethod
    def _result(
        target_id: int, balance: int, unlocked_at: datetime | None, debited: int
    ) -> dict[str, object]:
        return {
            "target_user_id": target_id,
            "viewer_id": None,
            "unlocked": True,
            "points_cost": PAPER_PLANE_PROFILE_UNLOCK_COST,
            "points_debited": debited,
            "balance": int(balance),
            "unlocked_at": unlocked_at,
            "profile": {"display_name": "匿名用户", "phone": None, "wechat": None},
        }


async def get_profile_unlock(
    db: AsyncSession, viewer_id: int, target_id: int
) -> dict[str, object]:
    """Read entitlement and current balance without exposing contact data."""
    row = (
        await db.execute(
            text("""SELECT unlocked_at FROM paper_plane_profile_unlock
                   WHERE viewer_user_id = :viewer AND target_user_id = :target"""),
            {"viewer": viewer_id, "target": target_id},
        )
    ).mappings().first()
    return {
        "target_user_id": target_id,
        "unlocked": bool(row),
        "points_cost": PAPER_PLANE_PROFILE_UNLOCK_COST,
        "balance": await _balance(db, viewer_id),
        "unlocked_at": row["unlocked_at"] if row else None,
        "profile": {"display_name": "匿名用户", "phone": None, "wechat": None} if row else None,
    }
