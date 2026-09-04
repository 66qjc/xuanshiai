"""Pure progress projection for the Moxiang natural-conversation journey.

Only active candidates at or above the shared high-confidence threshold may
advance the visible six-dimensional understanding progress.  The module has no
database dependency so the worker, state endpoint and WebSocket route all use
the exact same projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.db.ai_schema import PROFILE_DIMENSIONS, PROFILE_DIMENSION_SET
from app.schemas.ai_moxiang import CandidateRecord, HIGH_CONFIDENCE_THRESHOLD


@dataclass(frozen=True)
class JourneyDimensionProgress:
    """One fixed dimension's safe UI projection."""

    percent: float
    evidence_count: int


@dataclass(frozen=True)
class JourneyProgress:
    """The aggregate six-dimension progress projection."""

    overall_percent: float
    dimensions: dict[str, JourneyDimensionProgress]


def calculate_journey_progress(candidates: Iterable[CandidateRecord]) -> JourneyProgress:
    """Return 0/50/100 progress from unique eligible candidates per dimension.

    Callers provide the active session's candidate rows.  Content-hash
    de-duplication is also enforced here defensively so a faulty query or a
    retried worker cannot inflate the user-visible progress.
    """
    hashes_per_dimension: dict[str, set[str]] = {
        dimension: set() for dimension in PROFILE_DIMENSIONS
    }
    for candidate in candidates:
        if candidate.status not in {"active", "promoted"}:
            continue
        if candidate.confidence < HIGH_CONFIDENCE_THRESHOLD:
            continue
        if candidate.profile_dimension not in PROFILE_DIMENSION_SET:
            continue
        hashes_per_dimension[candidate.profile_dimension].add(candidate.content_hash)

    dimensions: dict[str, JourneyDimensionProgress] = {}
    percentages: list[float] = []
    for dimension in PROFILE_DIMENSIONS:
        evidence_count = len(hashes_per_dimension[dimension])
        percent = 100.0 if evidence_count >= 2 else 50.0 if evidence_count == 1 else 0.0
        dimensions[dimension] = JourneyDimensionProgress(
            percent=percent,
            evidence_count=evidence_count,
        )
        percentages.append(percent)

    return JourneyProgress(
        overall_percent=sum(percentages) / len(percentages) if percentages else 0.0,
        dimensions=dimensions,
    )
