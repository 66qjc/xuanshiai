"""AI feature gates: ``AiFeature`` enum and the fail-closed guard.

Feature gate follows the unified plan §6.6: in production any AI switch that is
turned on while the three approval gates are not all satisfied must fail closed
with the stable ``AI_FEATURE_DISABLED`` business error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.config import Settings
from app.schemas.ai_common import AiErrorCode


class AiFeature(str, Enum):
    """Per-module AI feature keys mapped to their settings switches."""

    PROFILE = "profile"
    SEARCH = "search"
    COMPATIBILITY_SHADOW = "compatibility_shadow"


class AiFeatureDisabledError(Exception):
    """Stable business error raised by ``require_ai_feature``.

    Maps to ``HTTP 503 AI_FEATURE_DISABLED`` (retryable=false, §11.2).  The
    message is safe for error responses: it never exposes provider internals.
    """

    def __init__(self, feature: AiFeature) -> None:
        super().__init__(f"AI 功能暂不可用: {feature.value}")
        self.code = AiErrorCode.AI_FEATURE_DISABLED.value
        self.feature = feature


def is_ai_feature_enabled(feature: AiFeature, settings: Settings) -> bool:
    """Return whether the feature switch is on, including the master gate."""
    if not settings.ai_master_enabled:
        return False
    if feature is AiFeature.PROFILE:
        return settings.ai_profile_enabled
    if feature is AiFeature.SEARCH:
        return settings.ai_search_enabled
    if feature is AiFeature.COMPATIBILITY_SHADOW:
        return settings.ai_compatibility_shadow_enabled
    return False


def _production_approvals_ok(settings: Settings) -> bool:
    """Fail closed in production when approvals or provider are not ready."""
    if settings.environment != "production":
        return True
    return bool(
        settings.ai_policy_approved
        and settings.ai_provider_approved
        and settings.ai_retention_policy_version
        and settings.ai_provider != "mock"
    )


def require_ai_feature(feature: AiFeature, settings: Settings) -> None:
    """Raise :class:`AiFeatureDisabledError` when the feature gate is closed.

    The production Settings validator already rejects invalid combinations at
    boot; this guard re-checks at runtime so a stale configuration can never
    silently route traffic to a disabled feature.
    """
    if not is_ai_feature_enabled(feature, settings):
        raise AiFeatureDisabledError(feature)
    if not _production_approvals_ok(settings):
        raise AiFeatureDisabledError(feature)


# ----------------------------------------------------------------------
# 发布证据与 release gate（统一方案 §12.3 / §13.4，执行计划 Task 12）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseEvidence:
    """Evidence package a production enablement decision must verify.

    ``blockers`` carries any additional hard-gate findings aggregated by the
    release verification script (missing tables, missing OpenAPI paths,
    privacy/deletion residue, rollback drill not proven, ...); any non-empty
    ``blockers`` keeps the gate disabled.  ``required_paths``,
    ``phase4_requires_dpa`` and ``phase5_requires_fairness_review`` are the
    explicit OpenAPI / future-phase launch conditions pinned by the Task 12
    test contract.
    """

    required_paths: tuple[str, ...] = ()
    phase4_requires_dpa: bool = False
    phase5_requires_fairness_review: bool = False
    blockers: tuple[str, ...] = field(default_factory=tuple)

    def with_blockers(self, blockers: tuple[str, ...]) -> ReleaseEvidence:
        """Return a copy carrying additional evidence blockers."""
        return ReleaseEvidence(
            required_paths=self.required_paths,
            phase4_requires_dpa=self.phase4_requires_dpa,
            phase5_requires_fairness_review=self.phase5_requires_fairness_review,
            blockers=tuple(dict.fromkeys((*self.blockers, *blockers))),
        )


@dataclass(frozen=True)
class ReleaseGateDecision:
    """Outcome of :func:`evaluate_ai_release_gate`.

    ``enabled=True`` uses the stable ``AI_RELEASE_APPROVED`` code; every other
    outcome is disabled with the stable ``AI_FEATURE_DISABLED`` code and the
    ordered blocker list.

    Factory methods are named ``approved``/``blocked`` (not ``enabled``/
    ``disabled``) because a dataclass field named ``enabled`` would otherwise
    treat a same-named classmethod as its default value.
    """

    enabled: bool
    code: str
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def approved(cls, code: str = "AI_RELEASE_APPROVED") -> ReleaseGateDecision:
        return cls(enabled=True, code=code)

    @classmethod
    def blocked(
        cls, code: str = "AI_FEATURE_DISABLED", blockers: tuple[str, ...] = ()
    ) -> ReleaseGateDecision:
        return cls(enabled=False, code=code, blockers=tuple(blockers))

    @property
    def release_gate(self) -> str:
        """Stable script output: ``passed`` or ``disabled-until-approved``."""
        return "passed" if self.enabled else "disabled-until-approved"


def evaluate_ai_release_gate(
    settings: Settings, evidence: ReleaseEvidence
) -> ReleaseGateDecision:
    """Decide whether the AI release may be turned on (Task 12 gate).

    Configuration checks run first: master switch, policy approval, provider
    approval and retention policy version.  Any non-empty ``evidence.blockers``
    (hard-gate findings aggregated by ``scripts/verify_ai_release.py``) keeps
    the gate disabled.  Every disabled outcome carries the stable
    ``AI_FEATURE_DISABLED`` code; only a fully-approved configuration with an
    empty blocker list returns ``AI_RELEASE_APPROVED``.
    """
    blockers: list[str] = []
    if not settings.ai_master_enabled:
        blockers.append("master_disabled")
    if not settings.ai_policy_approved:
        blockers.append("policy_not_approved")
    if not settings.ai_provider_approved:
        blockers.append("provider_not_approved")
    if not settings.ai_retention_policy_version:
        blockers.append("retention_policy_missing")
    blockers.extend(evidence.blockers)
    if blockers:
        return ReleaseGateDecision.blocked(
            "AI_FEATURE_DISABLED", tuple(dict.fromkeys(blockers))
        )
    return ReleaseGateDecision.approved("AI_RELEASE_APPROVED")
