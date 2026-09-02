"""Phase 1 moxiang_state REST service tests (Contract v1.1 §6/§7).

Pure-function tests for ``app.services.ai.moxiang_state``. The repository is
injected as a fake so the service logic can be exercised without a real
MySQL connection.

Coverage:

- ``build_state_response`` returns 200-shaped response when consent is missing
  (``consent_granted=False``) — never raises.
- Two-subject model: ``personal`` is the default ``active_subject``; if
  personal has no session and ideal_partner does, ideal_partner is active.
- Per-dimension percent mapping: 0 / 1 / 2+ confirmed → 0 / 50 / 100.
- ``overall_percent`` is the six-dimension average.
- ``list_turns`` paginates ascending by turn_no and returns ``next_before_turn_no``
  while more rows exist, ``None`` when exhausted.
- ``advance_journey_stage`` enforces the monotonic chain
  chatting → building → ready → published; regressions raise ``ValueError``.
- ``has_pending_invite`` is ``True`` iff the session has a pending build_invite
  row, otherwise ``False`` even when published_revision_id is set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_FILE = REPO_ROOT / "app" / "services" / "ai" / "moxiang_state.py"


def _read(path):
    return path.read_text(encoding="utf-8")


def _await(coro: Any) -> Any:
    """同步驱动 async 状态聚合，避免测试依赖 pytest-asyncio。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- in-memory repository -----------------------------------------------


class _FakeRepo:
    def __init__(
        self,
        *,
        consent_granted: bool = True,
        sessions: dict[tuple[int, str], dict] | None = None,
        dimensions: dict[tuple[int, str], dict[str, int]] | None = None,
        pending_invites: dict[tuple[int, str], dict] | None = None,
        pending_confirm_cards: set[str] | None = None,
        published_revisions: dict[tuple[int, str], dict] | None = None,
        avg_confidence: float = 0.0,
        confirmation_pct: float = 0.0,
        turns: dict[str, list[dict]] | None = None,
    ):
        self.consent_granted = consent_granted
        self.sessions = sessions or {}
        self.dimensions = dimensions or {}
        self.pending_invites = pending_invites or {}
        self.pending_confirm_cards = pending_confirm_cards or set()
        self.published_revisions = published_revisions or {}
        self.avg_confidence = avg_confidence
        self.confirmation_pct = confirmation_pct
        self.turns = turns or {}

    def has_active_consent(self, user_id, scope, version):
        return self.consent_granted

    def find_active_session(self, user_id, subject):
        return self.sessions.get((user_id, subject))

    def find_published_revision(self, user_id, subject):
        return self.published_revisions.get((user_id, subject))

    def find_pending_build_invite(self, user_id, session_id):
        for (uid, _), row in self.pending_invites.items():
            if uid == user_id and row.get("session_id") == session_id:
                return row
        return None

    def find_pending_confirm_card(self, session_id):
        return session_id in self.pending_confirm_cards

    def count_dimension_confirmed(self, user_id, subject):
        return self.dimensions.get((user_id, subject), {})

    def average_confidence(self, user_id, subject):
        return self.avg_confidence

    def confirmation_percent(self, user_id, subject):
        return self.confirmation_pct

    def list_session_turns(self, session_id, before_turn_no, limit):
        rows = self.turns.get(session_id, [])
        if before_turn_no is not None:
            rows = [r for r in rows if int(r.get("turn_no", 0)) < int(before_turn_no)]
        # 仓储按 ASC 约定返回 limit 行
        rows = sorted(rows, key=lambda r: int(r.get("turn_no", 0)))
        return tuple(rows[:limit])

    # Phase 2 P2-01 老用户恢复 fake：默认按"会话或发布过 revision"判定。
    def has_subject_history(self, user_id, subject):
        if (user_id, subject) in self.sessions:
            return True
        if (user_id, subject) in self.published_revisions:
            return True
        return False


class _AsyncRepo(_FakeRepo):
    """真实 SQL 仓储同构 fake：所有取数方法均返回 awaitable。"""

    async def has_active_consent(self, user_id, scope, version):
        return super().has_active_consent(user_id, scope, version)

    async def find_active_session(self, user_id, subject):
        return super().find_active_session(user_id, subject)

    async def find_published_revision(self, user_id, subject):
        return super().find_published_revision(user_id, subject)

    async def find_pending_build_invite(self, user_id, session_id):
        return super().find_pending_build_invite(user_id, session_id)

    async def find_pending_confirm_card(self, session_id):
        return super().find_pending_confirm_card(session_id)

    async def count_dimension_confirmed(self, user_id, subject):
        return super().count_dimension_confirmed(user_id, subject)

    async def average_confidence(self, user_id, subject):
        return super().average_confidence(user_id, subject)

    async def confirmation_percent(self, user_id, subject):
        return super().confirmation_percent(user_id, subject)

    async def has_subject_history(self, user_id, subject):
        return super().has_subject_history(user_id, subject)


# ---- top-level service tests --------------------------------------------


def test_state_response_consent_missing_returns_200_shape() -> None:
    """Unauthorised callers must receive a valid 200-shaped response (Contract §6.1)."""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(consent_granted=False)
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.consent_granted is False
    assert state.personal.journey_stage == "chatting"
    assert state.ideal_partner.journey_stage == "chatting"


def test_state_response_awaits_async_sql_repository() -> None:
    """生产 SQL 仓储是 async，状态聚合必须消费其真实结果而非 coroutine。"""
    from app.services.ai.moxiang_state import build_state_response

    repo = _AsyncRepo(
        sessions={(1, "personal"): {"session_id": "s-p", "journey_stage": "chatting"}}
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.personal.session_id == "s-p"


def test_state_response_default_active_subject_is_personal() -> None:
    """Both subjects with sessions: active_subject defaults to personal."""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={
            (1, "personal"): {"session_id": "s-p", "journey_stage": "chatting"},
            (1, "ideal_partner"): {"session_id": "s-i", "journey_stage": "chatting"},
        }
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.active_subject == "personal"


def test_state_response_falls_back_to_ideal_partner_when_personal_absent() -> None:
    """If personal has no session, ideal_partner becomes active (Contract §6.1)."""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={
            (1, "ideal_partner"): {"session_id": "s-i", "journey_stage": "chatting"},
        }
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.active_subject == "ideal_partner"


def test_dimension_percent_three_step_mapping() -> None:
    """0 / 1 / 2+ confirmed map to 0 / 50 / 100 (Contract §7)."""
    from app.services.ai.moxiang_state import (
        PROFILE_DIMENSIONS,
        _dimension_percent_from_confirmed,
    )

    assert _dimension_percent_from_confirmed(0) == 0.0
    assert _dimension_percent_from_confirmed(1) == 50.0
    assert _dimension_percent_from_confirmed(2) == 100.0
    assert _dimension_percent_from_confirmed(5) == 100.0
    # All six dimensions must exist in the canonical tuple (Contract §1.3).
    assert len(PROFILE_DIMENSIONS) == 6


def test_overall_percent_is_six_dimension_average() -> None:
    """overall_percent must be the average of the six per-dimension percents."""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={
            (1, "personal"): {"session_id": "s-p", "journey_stage": "chatting"},
        },
        dimensions={
            (1, "personal"): {
                "personality_social": 2,  # 100
                "intimacy_pattern": 0,  # 0
                "lifestyle": 1,  # 50
                "emotional_expression": 0,  # 0
                "relationship_boundaries": 0,  # 0
                "future_expectations": 1,  # 50
            }
        },
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    expected = (100 + 0 + 50 + 0 + 0 + 50) / 6
    assert abs(state.personal.overall_percent - expected) < 0.01


def test_state_response_marks_has_pending_invite() -> None:
    """has_pending_invite must be True when a pending row exists, else False."""
    from app.services.ai.moxiang_state import build_state_response

    repo_with = _FakeRepo(
        sessions={(1, "personal"): {"session_id": "s-p", "journey_stage": "building"}},
        pending_invites={
            (1, "personal"): {"invite_id": "inv-1", "session_id": "s-p"},
        },
    )
    state_with = _await(build_state_response(user_id=1, repo=repo_with))
    assert state_with.personal.has_pending_invite is True

    repo_without = _FakeRepo(
        sessions={(1, "personal"): {"session_id": "s-p", "journey_stage": "building"}},
    )
    state_without = _await(build_state_response(user_id=1, repo=repo_without))
    assert state_without.personal.has_pending_invite is False


# ---- list_turns pagination ----------------------------------------------


def test_list_turns_returns_ascending_with_cursor() -> None:
    """``list_turns`` must return ASC by turn_no and yield a cursor for next page."""
    from app.services.ai.moxiang_state import list_turns

    turns = [
        {"turn_id": f"t-{i}", "turn_no": i, "role": "user", "content": f"turn {i}"}
        for i in range(1, 11)  # 10 turns
    ]
    repo = _FakeRepo(turns={"s-1": turns})
    page, cursor = list_turns(session_id="s-1", before_turn_no=None, limit=3, repo=repo)
    assert [t["turn_no"] for t in page] == [1, 2, 3]
    assert cursor == 1  # next_before_turn_no is the smallest turn_no on this page


def test_list_turns_exhausted_returns_none_cursor() -> None:
    """When fewer rows than ``limit`` come back, ``next_before_turn_no`` is ``None``."""
    from app.services.ai.moxiang_state import list_turns

    turns = [
        {"turn_id": f"t-{i}", "turn_no": i, "role": "user", "content": f"turn {i}"}
        for i in range(1, 4)  # 3 turns
    ]
    repo = _FakeRepo(turns={"s-1": turns})
    page, cursor = list_turns(session_id="s-1", before_turn_no=None, limit=10, repo=repo)
    assert [t["turn_no"] for t in page] == [1, 2, 3]
    assert cursor is None


def test_list_turns_validates_limit() -> None:
    """``limit`` outside 1..100 must raise ``ValueError``."""
    from app.services.ai.moxiang_state import list_turns

    repo = _FakeRepo()
    with pytest.raises(ValueError):
        list_turns(session_id="s-1", before_turn_no=None, limit=0, repo=repo)
    with pytest.raises(ValueError):
        list_turns(session_id="s-1", before_turn_no=None, limit=200, repo=repo)


# ---- journey stage state machine ----------------------------------------


def test_advance_journey_stage_monotonic_chain() -> None:
    """The journey stage must move forward: chatting → building → ready → published."""
    from app.services.ai.moxiang_state import advance_journey_stage

    assert advance_journey_stage("chatting", "building") == "building"
    assert advance_journey_stage("building", "ready") == "ready"
    assert advance_journey_stage("ready", "published") == "published"
    # Same stage is a no-op.
    assert advance_journey_stage("chatting", "chatting") == "chatting"


def test_advance_journey_stage_rejects_regression() -> None:
    """Regression (e.g. ready → chatting) must raise ``ValueError``."""
    from app.services.ai.moxiang_state import advance_journey_stage

    with pytest.raises(ValueError):
        advance_journey_stage("ready", "chatting")
    with pytest.raises(ValueError):
        advance_journey_stage("published", "building")


def test_advance_journey_stage_rejects_invalid_target() -> None:
    """Unknown target stage must raise ``ValueError``."""
    from app.services.ai.moxiang_state import advance_journey_stage

    with pytest.raises(ValueError):
        advance_journey_stage("chatting", "bogus")
