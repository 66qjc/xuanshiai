"""Central candidate visibility policy shared by discovery-style reads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


POLICY_REVISION = "ai-policy-2026-08-07-v1"
_SQL_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class VisibilityScene(str, Enum):
    DISCOVERY = "discovery"
    SEARCH = "search"
    PROFILE = "profile"
    HISTORY = "history"
    VISITORS = "visitors"
    FAVORITES = "favorites"
    INTERACTION = "interaction"


@dataclass(frozen=True, slots=True)
class ViewerContext:
    user_id: int
    realname_status: int | None
    is_vip: bool


@dataclass(frozen=True, slots=True)
class CandidateContext:
    user_id: int
    who_can_see_me: int
    account_active: bool = True
    profile_visible: bool = True
    match_active: bool = True
    not_restricted: bool = True
    profile_complete: bool = True
    media_approved: bool = True
    blocked: bool = False

    @classmethod
    def visible(cls, user_id: int, who_can_see_me: int, **overrides: bool) -> CandidateContext:
        return cls(user_id=user_id, who_can_see_me=who_can_see_me, **overrides)


@dataclass(frozen=True, slots=True)
class VisibilityDecision:
    allowed: bool
    denial_code: str | None
    policy_revision: str = POLICY_REVISION
    scene: VisibilityScene | None = None


@dataclass(frozen=True, slots=True)
class SqlPredicate:
    clause: str
    params: dict[str, int]
    scene: VisibilityScene
    policy_revision: str = POLICY_REVISION


def evaluate_visibility(
    viewer: ViewerContext, candidate: CandidateContext
) -> VisibilityDecision:
    if candidate.user_id == viewer.user_id:
        return VisibilityDecision(False, "SELF_REFERENCE")
    if candidate.blocked:
        return VisibilityDecision(False, "BLOCKED_RELATIONSHIP")
    if not candidate.account_active:
        return VisibilityDecision(False, "ACCOUNT_INACTIVE")
    if not candidate.not_restricted:
        return VisibilityDecision(False, "ACCOUNT_RESTRICTED")
    if not candidate.profile_visible:
        return VisibilityDecision(False, "CANDIDATE_NOT_VISIBLE")
    if not candidate.match_active:
        return VisibilityDecision(False, "MATCH_DISABLED")
    if not candidate.profile_complete:
        return VisibilityDecision(False, "PROFILE_INCOMPLETE")
    if not candidate.media_approved:
        return VisibilityDecision(False, "MEDIA_REVIEW_PENDING")
    if candidate.who_can_see_me == 4:
        return VisibilityDecision(False, "PRIVATE_PROFILE")
    if candidate.who_can_see_me == 2 and viewer.realname_status != 2:
        return VisibilityDecision(False, "REALNAME_REQUIRED")
    if candidate.who_can_see_me == 3 and not viewer.is_vip:
        return VisibilityDecision(False, "VIP_REQUIRED")
    if candidate.who_can_see_me not in (1, 2, 3):
        return VisibilityDecision(False, "UNKNOWN_VISIBILITY_POLICY")
    return VisibilityDecision(True, None)


class CandidateVisibilityService:
    """Expose one policy source for object checks and server-owned SQL filters."""

    async def decide(
        self,
        db: AsyncSession,
        viewer_id: int,
        candidate_id: int,
        scene: VisibilityScene,
    ) -> VisibilityDecision:
        """Read current visibility facts for one candidate and apply the shared rule."""
        result = await db.execute(
            text(
                """SELECT candidate.id AS candidate_id,
                    COALESCE(viewer_auth.realname_status, 0) AS viewer_realname_status,
                    EXISTS (
                        SELECT 1 FROM user_membership viewer_membership
                        WHERE viewer_membership.user_id = :visibility_viewer_id
                          AND viewer_membership.status = 1
                          AND (viewer_membership.start_at IS NULL
                               OR viewer_membership.start_at <= UTC_TIMESTAMP())
                          AND (viewer_membership.end_at IS NULL
                               OR viewer_membership.end_at > UTC_TIMESTAMP())
                    ) AS viewer_is_vip,
                    COALESCE(candidate_privacy.who_can_see_me, 1) AS who_can_see_me,
                    candidate.status = 1 AS account_active,
                    COALESCE(candidate_privacy.show_profile, 1) = 1 AS profile_visible,
                    COALESCE(candidate_privacy.match_status, 1) = 1 AS match_active,
                    NOT EXISTS (
                        SELECT 1 FROM user_restriction candidate_restriction
                        WHERE candidate_restriction.user_id = candidate.id
                          AND candidate_restriction.restriction_type = 'TOTAL_BAN'
                          AND candidate_restriction.status = 1
                          AND candidate_restriction.starts_at <= UTC_TIMESTAMP()
                          AND (candidate_restriction.ends_at IS NULL
                               OR candidate_restriction.ends_at > UTC_TIMESTAMP())
                    ) AS not_restricted,
                    COALESCE(candidate_completion.score, 0) >= 100 AS profile_complete,
                    NOT EXISTS (
                        SELECT 1 FROM user_media pending_media
                        WHERE pending_media.user_id = candidate.id
                          AND pending_media.deleted_at IS NULL
                          AND pending_media.review_status IN (0, 2, 3)
                    ) AS media_approved,
                    EXISTS (
                        SELECT 1 FROM user_block blocked_relationship
                        WHERE (blocked_relationship.user_id = :visibility_viewer_id
                               AND blocked_relationship.target_user_id = candidate.id)
                           OR (blocked_relationship.user_id = candidate.id
                               AND blocked_relationship.target_user_id = :visibility_viewer_id)
                    ) AS blocked
                FROM users candidate
                LEFT JOIN user_auth viewer_auth
                  ON viewer_auth.user_id = :visibility_viewer_id
                LEFT JOIN user_privacy candidate_privacy
                  ON candidate_privacy.user_id = candidate.id
                LEFT JOIN user_profile_completion candidate_completion
                  ON candidate_completion.user_id = candidate.id
                WHERE candidate.id = :candidate_id
                LIMIT 1"""
            ),
            {
                "visibility_viewer_id": viewer_id,
                "candidate_id": candidate_id,
            },
        )
        row = result.mappings().first()
        if not row:
            return VisibilityDecision(False, "CANDIDATE_NOT_VISIBLE", scene=scene)

        viewer = ViewerContext(
            user_id=viewer_id,
            realname_status=int(row["viewer_realname_status"] or 0),
            is_vip=bool(row["viewer_is_vip"]),
        )
        candidate = CandidateContext(
            user_id=int(row["candidate_id"]),
            who_can_see_me=int(row["who_can_see_me"] or 0),
            account_active=bool(row["account_active"]),
            profile_visible=bool(row["profile_visible"]),
            match_active=bool(row["match_active"]),
            not_restricted=bool(row["not_restricted"]),
            profile_complete=bool(row["profile_complete"]),
            media_approved=bool(row["media_approved"]),
            blocked=bool(row["blocked"]),
        )
        decision = evaluate_visibility(viewer, candidate)
        return VisibilityDecision(
            decision.allowed,
            decision.denial_code,
            scene=scene,
        )

    def predicate(
        self,
        viewer: ViewerContext,
        scene: VisibilityScene,
        *,
        candidate_alias: str = "u",
        privacy_alias: str = "pr",
        completion_alias: str = "c",
    ) -> SqlPredicate:
        for alias in (candidate_alias, privacy_alias, completion_alias):
            if not _SQL_ALIAS.fullmatch(alias):
                raise ValueError("invalid SQL alias")
        clause = " AND ".join(
            (
                f"{candidate_alias}.id <> :visibility_viewer_id",
                f"{candidate_alias}.status = 1",
                f"COALESCE({completion_alias}.score, 0) >= 100",
                "NOT EXISTS (SELECT 1 FROM user_restriction ban "
                f"WHERE ban.user_id = {candidate_alias}.id "
                "AND ban.restriction_type = 'TOTAL_BAN' AND ban.status = 1 "
                "AND ban.starts_at <= UTC_TIMESTAMP() "
                "AND (ban.ends_at IS NULL OR ban.ends_at > UTC_TIMESTAMP()))",
                f"COALESCE({privacy_alias}.show_profile, 1) = 1",
                f"COALESCE({privacy_alias}.match_status, 1) = 1",
                f"COALESCE({privacy_alias}.who_can_see_me, 1) IN (1, 2, 3)",
                "(:visibility_realname_status = 2 "
                f"OR COALESCE({privacy_alias}.who_can_see_me, 1) <> 2)",
                "(:visibility_viewer_is_vip = 1 "
                f"OR COALESCE({privacy_alias}.who_can_see_me, 1) <> 3)",
                "NOT EXISTS (SELECT 1 FROM user_media pending_media "
                f"WHERE pending_media.user_id = {candidate_alias}.id "
                "AND pending_media.deleted_at IS NULL "
                "AND pending_media.review_status IN (0, 2, 3))",
                "NOT EXISTS (SELECT 1 FROM user_block bl "
                "WHERE (bl.user_id = :visibility_viewer_id "
                f"AND bl.target_user_id = {candidate_alias}.id) "
                f"OR (bl.user_id = {candidate_alias}.id "
                "AND bl.target_user_id = :visibility_viewer_id))",
            )
        )
        return SqlPredicate(
            clause=clause,
            params={
                "visibility_viewer_id": viewer.user_id,
                "visibility_realname_status": viewer.realname_status or 0,
                "visibility_viewer_is_vip": int(viewer.is_vip),
            },
            scene=scene,
        )
