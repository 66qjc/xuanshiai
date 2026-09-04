"""Guard the journey-only realtime WebSocket and worker wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTE = REPO_ROOT / "app" / "api" / "routes" / "voice_moxiang.py"
WORKER = REPO_ROOT / "app" / "workers" / "ai_worker.py"
JOURNEY = REPO_ROOT / "app" / "services" / "ai" / "journey.py"
PROFILE = REPO_ROOT / "app" / "services" / "ai" / "profile.py"
LEGACY_RETIREMENT_MIGRATION = (
    REPO_ROOT / "migrations" / "ai" / "20260902_01_retire_legacy_moxiang_profile_extract_up.sql"
)


def test_moxiang_route_uses_journey_candidate_pipeline_only() -> None:
    source = ROUTE.read_text(encoding="utf-8")

    assert '"moxiang_journey"' in source
    assert "submit_journey_turn" in source
    assert '"extraction_status"' in source
    assert '"journey_progress"' in source
    assert "_PROFILE_BUILD_MODE" not in source
    assert "_finish_build_turn" not in source
    assert "submit_profile_turn" not in source


def test_worker_registers_dedicated_journey_candidate_handler() -> None:
    source = WORKER.read_text(encoding="utf-8")

    assert '"moxiang_candidate_extract"' in source
    assert "extract_journey_candidates" in source


def test_journey_route_resolves_invites_after_candidate_thresholds() -> None:
    source = ROUTE.read_text(encoding="utf-8")

    assert "maybe_create_build_invite" in source
    assert "resolve_journey_invite" in source
    assert '"build_invite_accept"' in source
    assert '"build_invite_snooze"' in source


def test_journey_turn_replay_uses_a_stable_task_key() -> None:
    """A retransmitted client turn must find its original task, not queue another."""
    source = JOURNEY.read_text(encoding="utf-8")

    assert "def journey_task_key" in source
    assert "idempotency_key=task_key" in source
    from app.services.ai.journey import journey_task_key

    assert journey_task_key("session-1", "turn-1") == journey_task_key(
        "session-1", "turn-1"
    )


def test_revised_final_text_and_disconnect_keep_candidate_work_independent() -> None:
    """All final text paths enqueue work; disconnect cleanup must not mutate the set."""
    source = ROUTE.read_text(encoding="utf-8")
    revise = source[source.index('elif msg_type == "revise_text"'):source.index('elif msg_type == "cancel"')]

    assert "_submit_journey_candidate_turn" in revise
    assert "_finish_journey_turn" in revise
    assert "for poll_task in tuple(poll_tasks)" in source


def test_journey_websocket_requires_the_explicit_production_feature_gate() -> None:
    source = ROUTE.read_text(encoding="utf-8")

    assert "def _check_journey_feature" in source
    assert "ai_moxiang_journey_enabled" in source
    assert "AiFeature.PROFILE" in source


def test_legacy_master_extract_tasks_are_audit_cancelled_before_deploy() -> None:
    """Removing the old handler must not strand queued legacy jobs."""
    source = LEGACY_RETIREMENT_MIGRATION.read_text(encoding="utf-8")

    assert "AI_LEGACY_MOXIANG_RETIRED" in source
    assert "profile_extract" in source
    assert "`session_kind` = 'master'" in source
    assert "`status` = 'cancelled'" in source


def test_profile_extract_no_longer_has_a_master_draft_branch() -> None:
    """The generic extractor must terminate a stray legacy master task safely."""
    source = PROFILE.read_text(encoding="utf-8")
    branch_start = source.index('if session.session_kind == "master":')
    branch_end = source.index('if session.session_kind == "update":', branch_start)
    branch = source[branch_start:branch_end]

    assert "AI_LEGACY_MOXIANG_RETIRED" in branch
    assert "_handle_master_extract" not in branch
