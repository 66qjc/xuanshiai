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
        pending_invites: dict[tuple[int, str], dict] | None = None,
        pending_confirm_cards: set[str] | None = None,
        published_revisions: dict[tuple[int, str], dict] | None = None,
        turns: dict[str, list[dict]] | None = None,
        candidates: dict[str, tuple] | None = None,
    ):
        self.consent_granted = consent_granted
        self.sessions = sessions or {}
        self.pending_invites = pending_invites or {}
        self.pending_confirm_cards = pending_confirm_cards or set()
        self.published_revisions = published_revisions or {}
        self.turns = turns or {}
        self.candidates = candidates

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

    def list_session_candidates(self, session_id):
        return (self.candidates or {}).get(session_id, ())

    def list_session_turns(self, session_id, before_turn_no, limit):
        rows = self.turns.get(session_id, [])
        if before_turn_no is not None:
            rows = [r for r in rows if int(r.get("turn_no", 0)) < int(before_turn_no)]
        # 生产仓储先取最新 limit 行；服务层再统一整理为 ASC 响应。
        rows = sorted(rows, key=lambda r: int(r.get("turn_no", 0)), reverse=True)
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

    async def list_session_candidates(self, session_id):
        return super().list_session_candidates(session_id)

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


def test_state_exposes_published_revision_without_active_session() -> None:
    """发布后 session 关闭（active_status=0）：state 仍须暴露 published_revision_id，
    并据此解锁 ideal_partner——否则墨相师页与愿遇之相入口双双"失明"。"""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        published_revisions={(1, "personal"): {"revision_id": 77}},
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.personal.session_id is None
    assert state.personal.published_revision_id == 77
    assert state.can_start_ideal_partner is True
    assert state.ideal_partner.can_start_ideal_partner is True


def test_state_without_session_or_revision_keeps_partner_locked() -> None:
    """无任何资产时 ideal_partner 仍锁定（回归保护）。"""
    from app.services.ai.moxiang_state import build_state_response

    state = _await(build_state_response(user_id=1, repo=_FakeRepo()))
    assert state.personal.published_revision_id is None
    assert state.can_start_ideal_partner is False


def test_state_progress_uses_active_high_confidence_candidates() -> None:
    """Reconnect state must use the same candidate projection as live WS progress."""
    from app.schemas.ai_moxiang import CandidateRecord
    from app.services.ai.moxiang_state import build_state_response

    def candidate(candidate_id: str, dimension: str) -> CandidateRecord:
        return CandidateRecord(
            candidate_id=candidate_id,
            session_id="s-p",
            user_id=1,
            subject="personal",
            profile_dimension=dimension,
            field_kind="structured",
            field_key="temperament",
            category=None,
            content="偏内敛",
            value="偏内敛",
            confidence=0.75,
            source_turn_ids=("turn-1",),
            source_span=None,
            consent_version="profile-text-v1",
            policy_revision="ai-policy-2026-08-07-v1",
            status="active",
            content_hash=(candidate_id + ("x" * 64))[:64],
        )

    repo = _FakeRepo(
        sessions={(1, "personal"): {"session_id": "s-p", "journey_stage": "chatting"}},
        candidates={"s-p": (candidate("one", "lifestyle"),)},
    )
    state = _await(build_state_response(user_id=1, repo=repo))

    assert state.personal.dimensions["lifestyle"]["evidence_count"] == 1
    assert state.personal.dimensions["lifestyle"]["percent"] == 50.0
    assert state.personal.overall_percent == pytest.approx(50.0 / 6.0)


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


def test_list_turns_returns_latest_page_ascending_with_cursor() -> None:
    """首次读取取最新一页，响应按 turn_no ASC，并给出更早页游标。"""
    from app.services.ai.moxiang_state import list_turns

    turns = [
        {"turn_id": f"t-{i}", "turn_no": i, "role": "user", "content": f"turn {i}"}
        for i in range(1, 11)  # 10 turns
    ]
    repo = _FakeRepo(turns={"s-1": turns})
    page, cursor = _await(
        list_turns(session_id="s-1", before_turn_no=None, limit=3, repo=repo)
    )
    assert [t["turn_no"] for t in page] == [8, 9, 10]
    assert cursor == 8  # 下一页读取 turn_no < 8


def test_list_turns_exhausted_returns_none_cursor() -> None:
    """When fewer rows than ``limit`` come back, ``next_before_turn_no`` is ``None``."""
    from app.services.ai.moxiang_state import list_turns

    turns = [
        {"turn_id": f"t-{i}", "turn_no": i, "role": "user", "content": f"turn {i}"}
        for i in range(1, 4)  # 3 turns
    ]
    repo = _FakeRepo(turns={"s-1": turns})
    page, cursor = _await(
        list_turns(session_id="s-1", before_turn_no=None, limit=10, repo=repo)
    )
    assert [t["turn_no"] for t in page] == [1, 2, 3]
    assert cursor is None


def test_list_turns_validates_limit() -> None:
    """``limit`` outside 1..100 must raise ``ValueError``."""
    from app.services.ai.moxiang_state import list_turns

    repo = _FakeRepo()
    with pytest.raises(ValueError):
        _await(list_turns(session_id="s-1", before_turn_no=None, limit=0, repo=repo))
    with pytest.raises(ValueError):
        _await(list_turns(session_id="s-1", before_turn_no=None, limit=200, repo=repo))


def test_list_turns_awaits_async_repository() -> None:
    """生产 SQL 仓储是 async；服务不能把 coroutine 当作可迭代对象。"""
    from app.services.ai.moxiang_state import list_turns

    class _AsyncRepo(_FakeRepo):
        async def list_session_turns(self, session_id, before_turn_no, limit):
            return super().list_session_turns(session_id, before_turn_no, limit)

    repo = _AsyncRepo(
        turns={
            "s-1": [
                {
                    "turn_id": "t-1",
                    "turn_no": 1,
                    "role": "user",
                    "answer_text": "你好",
                }
            ]
        }
    )
    page, cursor = _await(
        list_turns(session_id="s-1", before_turn_no=None, limit=50, repo=repo)
    )
    assert [t["turn_id"] for t in page] == ["t-1"]
    assert cursor is None


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
