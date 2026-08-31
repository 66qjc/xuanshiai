"""M06 AI 匹配度 schemas: bidirectional shadow snapshots and recompute requests.

The snapshot follows the unified plan §9.4 shadow shape; ``display_eligible``
starts ``false`` and the old ``match_score`` semantics stay ``legacy-rule-v1``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CompatibilitySnapshotStatus(str, Enum):
    """Snapshot state (统一方案 §9.4)."""

    READY = "ready"
    STALE = "stale"
    BLOCKED = "blocked"
    COVERAGE_INSUFFICIENT = "coverage_insufficient"


class CompatibilityDirectionScores(BaseModel):
    viewer_to_target: float = Field(..., ge=0.0, le=100.0)
    target_to_viewer: float = Field(..., ge=0.0, le=100.0)


class CompatibilityRecomputeRequest(BaseModel):
    expected_viewer_profile_revision: int = Field(..., ge=0)
    expected_target_profile_revision: int = Field(..., ge=0)


class CompatibilitySnapshotRead(BaseModel):
    snapshot_id: str
    status: CompatibilitySnapshotStatus
    algorithm_version: str = "compatibility-rule-v1"
    score_semantics: str = "rule_based_reference_shadow"
    compatibility_index: float | None = Field(default=None, ge=0.0, le=100.0)
    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    directions: CompatibilityDirectionScores | None = None
    reason_codes: list[str] = Field(default_factory=list)
    profile_revision_pair: dict[str, int] = Field(default_factory=dict)
    privacy_revision_pair: dict[str, int] = Field(default_factory=dict)
    experiment_bucket: str = "shadow"
    display_eligible: bool = False
    engine: str = Field(
        default="rule-v1",
        description="WP-C1：最近一次计算来源 rule-v1/llm-v1",
    )
    brand_label: str | None = Field(
        default=None,
        description="WP-C4：AI 算法标注（默认「来自良配Ai算法」，随读取端点下发）",
    )
    reason_texts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="WP-C1：llm 精算的可解释中文理由（按方向分组，rule 引擎为空）",
    )
    disclaimer: str = "仅根据双方当前可见且已确认资料整理，供了解和破冰参考"
    calculated_at: datetime
    expires_at: datetime
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="原因码对应的证据引用（字段 key、source revisions、可展示标记与限制说明），不含对方敏感原文",
    )


class CompatibilitySnapshotRecomputeRead(BaseModel):
    """202 response after requesting a shadow recompute task."""

    snapshot_id: str
    task_id: str
    status: CompatibilitySnapshotStatus
    poll_after_ms: int = Field(default=1000, ge=0)
    expires_at: datetime | None = None


class CompatibilityErrorDetail(BaseModel):
    """Non-blocking internal note, never exposing hidden target facts."""

    code: str
    message: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
