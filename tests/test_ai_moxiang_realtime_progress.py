"""Realtime journey progress is derived from high-confidence candidates."""

from __future__ import annotations

from app.schemas.ai_moxiang import CandidateRecord


def _candidate(
    candidate_id: str,
    dimension: str,
    confidence: float = 0.75,
    status: str = "active",
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id,
        session_id="session-1",
        user_id=1,
        subject="personal",
        profile_dimension=dimension,
        field_kind="structured",
        field_key="temperament",
        category=None,
        content="偏内敛",
        value="偏内敛",
        confidence=confidence,
        source_turn_ids=("turn-1",),
        source_span=None,
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
        status=status,
        content_hash=(candidate_id + ("x" * 64))[:64],
    )


def test_high_confidence_candidates_drive_six_dimension_progress() -> None:
    """0/1/2 eligible candidates map to 0/50/100 and total is their mean."""
    from app.services.ai.journey_progress import calculate_journey_progress

    snapshot = calculate_journey_progress(
        (
            _candidate("candidate-1", "personality_social"),
            _candidate("candidate-2", "personality_social"),
            _candidate("candidate-3", "lifestyle"),
            _candidate("candidate-4", "intimacy_pattern", confidence=0.74),
            _candidate("candidate-5", "future_expectations", status="dismissed"),
        )
    )

    assert snapshot.dimensions["personality_social"].percent == 100.0
    assert snapshot.dimensions["personality_social"].evidence_count == 2
    assert snapshot.dimensions["lifestyle"].percent == 50.0
    assert snapshot.dimensions["lifestyle"].evidence_count == 1
    assert snapshot.dimensions["intimacy_pattern"].percent == 0.0
    assert snapshot.dimensions["future_expectations"].evidence_count == 0
    assert snapshot.overall_percent == 25.0
