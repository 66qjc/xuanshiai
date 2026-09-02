"""Phase 1 build_invite threshold contract tests (Contract v1.1 §3).

Validates the pure-source shape of ``app.services.ai.build_invite``:

- Threshold guard: invite creation fires only when the session has at least
  four *effective* user turns, covers three distinct dimensions, has at least
  three high-confidence candidates (confidence ≥ ``HIGH_CONFIDENCE``), and
  has fewer than two auto invites for this session.
- Single-pending guarantee: the service must not create a second pending row
  for a session that already has a pending invite; instead it should
  idempotently return the existing one.
- Snooze + accept transitions keep the ``active_slot=NULL`` invariant for
  terminal states; only ``pending`` keeps the row count under the unique key
  ``uk_ai_profile_build_invite_pending`` (Contract §1.4 / DDL STORED col).
- Summary items: at most six items, one per profile dimension, each carrying
  the dimension key and a short content string (≤ 80 字, per P1-UX).

These tests exercise the pure logic surface (threshold, summary shaping,
state transitions). The MySQL DDL enforcement is verified by
``test_ai_moxiang_candidate_schema.py`` and the reviewed migration runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_INVITE_FILE = REPO_ROOT / "app" / "services" / "ai" / "build_invite.py"
AI_SCHEMA_FILE = REPO_ROOT / "app" / "db" / "ai_schema.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_build_invite_module_declares_public_surface() -> None:
    """build_invite.py must export the public contract surface used by P1-C."""
    source = _read(BUILD_INVITE_FILE)
    for name in (
        "should_offer_invite",
        "build_invite_summary",
        "create_pending_invite",
        "resolve_invite",
    ):
        assert f"def {name}" in source, f"build_invite.py missing {name}()"


def test_threshold_constants_match_contract() -> None:
    """The threshold values must match Contract v1.1 §3.1 exactly."""
    source = _read(BUILD_INVITE_FILE)
    for const, expected in (
        ("MIN_EFFECTIVE_TURNS", "4"),
        ("MIN_DIMENSION_COUNT", "3"),
        ("MIN_HIGH_CONFIDENCE_CANDIDATES", "3"),
        ("MAX_AUTO_INVITES_PER_SESSION", "2"),
    ):
        assert const in source, f"build_invite.py missing {const} constant"
        # The numeric value must appear (allow whitespace / type coercion).
        body = source[source.find(const):]
        head = body[:200]
        assert expected in head, f"{const} expected to be {expected}, found head={head[:80]!r}"


def test_should_offer_invite_blocks_below_threshold() -> None:
    """Below-threshold inputs must return False, not partial invites."""
    from app.services.ai.build_invite import should_offer_invite

    assert should_offer_invite(
        effective_turn_count=3,
        dimension_count=3,
        high_confidence_candidate_count=3,
        auto_invite_count=0,
    ) is False
    assert should_offer_invite(
        effective_turn_count=4,
        dimension_count=2,
        high_confidence_candidate_count=3,
        auto_invite_count=0,
    ) is False
    assert should_offer_invite(
        effective_turn_count=4,
        dimension_count=3,
        high_confidence_candidate_count=2,
        auto_invite_count=0,
    ) is False


def test_should_offer_invite_blocks_after_max_auto_invites() -> None:
    """The auto invite counter caps at 2 per session; further attempts are blocked."""
    from app.services.ai.build_invite import should_offer_invite

    assert should_offer_invite(
        effective_turn_count=10,
        dimension_count=6,
        high_confidence_candidate_count=10,
        auto_invite_count=2,
    ) is False


def test_should_offer_invite_passes_at_threshold() -> None:
    """At-threshold inputs (4/3/3) must return True."""
    from app.services.ai.build_invite import should_offer_invite

    assert should_offer_invite(
        effective_turn_count=4,
        dimension_count=3,
        high_confidence_candidate_count=3,
        auto_invite_count=0,
    ) is True


def test_build_invite_summary_caps_at_six_items_one_per_dimension() -> None:
    """summary_items must contain at most six items, one per dimension, no dupes."""
    from app.services.ai.candidates import CandidateRecord
    from app.services.ai.build_invite import build_invite_summary

    candidates = [
        CandidateRecord(
            candidate_id=f"c-{i}",
            session_id="s-1",
            user_id=1,
            subject="personal",
            profile_dimension=dim,
            field_kind="entry",
            field_key=None,
            category=cat,
            content=("这是一段关于该维度的描述。" * 5)[:200],
            value=None,
            confidence=0.9,
            source_turn_ids=("t-1",),
            source_span=None,
            consent_version="profile-text-v1",
            policy_revision="ai-policy-2026-08-07-v1",
            status="active",
            content_hash=f"hash-{i}",
        )
        for i, (dim, cat) in enumerate(
            (
                ("personality_social", "personality"),
                ("intimacy_pattern", "values"),
                ("lifestyle", "routine"),
                ("emotional_expression", "personality"),
                ("relationship_boundaries", "values"),
                ("future_expectations", "life_plan"),
            )
        )
    ]
    items = build_invite_summary(candidates)
    assert 1 <= len(items) <= 6
    dimensions = [it.profile_dimension for it in items]
    assert len(set(dimensions)) == len(dimensions), (
        "summary_items must dedupe by profile_dimension (one per dimension)"
    )


def test_build_invite_summary_truncates_long_content() -> None:
    """Each item.content is capped at 80 字 (P1-UX contract)."""
    from app.services.ai.candidates import CandidateRecord
    from app.services.ai.build_invite import SUMMARY_CONTENT_MAX_LENGTH, build_invite_summary

    long_text = "一二三四五" * 60  # 300 chars
    candidates = [
        CandidateRecord(
            candidate_id="c-1",
            session_id="s-1",
            user_id=1,
            subject="personal",
            profile_dimension="lifestyle",
            field_kind="entry",
            field_key=None,
            category="routine",
            content=long_text,
            value=None,
            confidence=0.9,
            source_turn_ids=("t-1",),
            source_span=None,
            consent_version="profile-text-v1",
            policy_revision="ai-policy-2026-08-07-v1",
            status="active",
            content_hash="hash-1",
        )
    ]
    items = build_invite_summary(candidates)
    assert items[0].content == long_text[:SUMMARY_CONTENT_MAX_LENGTH]


def test_create_pending_invite_blocks_when_pending_exists() -> None:
    """If a session already has a pending invite, create_pending_invite returns it."""
    from app.services.ai.build_invite import create_pending_invite

    class _FakePending:
        invite_id = "inv-existing"
        session_id = "s-1"
        user_id = 1
        subject = "personal"
        status = "pending"
        invite_no = 1
        summary_json = []
        effective_turn_count_at_create = 4
        dimension_count = 3
        candidate_count = 3
        snoozed_at_effective_turn_count = None
        accepted_at = None
        snoozed_at = None
        expired_at = None
        created_at = None
        updated_at = None

    class _FakeRepo:
        def find_pending_invite(self, session_id: str):
            return _FakePending()

        def count_auto_invites(self, session_id: str) -> int:
            return 0

        def count_active_candidates(self, session_id: str) -> int:
            return 3

        def list_high_confidence_candidates(self, session_id: str):
            return []

        def list_active_candidates(self, session_id: str):
            return []

        def next_invite_no(self, session_id: str) -> int:
            return 2

        def insert_pending(self, **kwargs):  # pragma: no cover
            raise AssertionError("must not insert when pending already exists")

    repo = _FakeRepo()
    result = create_pending_invite(
        repo=repo,
        session_id="s-1",
        user_id=1,
        subject="personal",
        effective_turn_count=4,
        dimension_count=3,
        candidate_count=3,
    )
    assert result.invite.invite_id == "inv-existing"
    assert result.already_pending is True


def test_resolve_invite_rejects_unknown_resolution() -> None:
    """Unknown resolution strings raise ``AIInputError`` and never touch DB."""
    from app.services.ai.build_invite import AIInputError, resolve_invite

    class _FakeRepo:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def get_invite(self, invite_id: str):
            return type(
                "_P",
                (),
                {"invite_id": invite_id, "session_id": "s-1", "status": "pending", "user_id": 1},
            )()

        def mark_resolved(self, invite_id: str, resolution: str) -> None:
            self.calls.append((invite_id, resolution))

    repo = _FakeRepo()
    with pytest.raises(AIInputError):
        resolve_invite(repo, "inv-1", user_id=1, resolution="bogus")
    assert repo.calls == [], "bogus resolution must not call repo.mark_resolved"
