"""墨相师四阶段融合 Pydantic / dataclass schema（Contract v1.1）。

本模块只负责"打印契约"，不负责落库（落库由
``app.services.ai.candidates`` 与 ``app.services.ai.build_invite``
封装）。前端（``xuanshiai-vue/api/ai-moxiang.uts``）通过 REST 拿到的就是
本 schema 的 camelCase 投影：snake_case 是后端落库字段，camelCase 是网络
字段，两者由 ``adapt_state`` / ``adapt_subject_summary`` / ``adapt_turns``
在 ``ai-moxiang.uts`` 内做显式映射。

Contract v1.1 引用约定：
- §1.2 旅程阶段 chatting / building / ready / published
- §1.3 六维固定维度
- §1.4 单 pending 邀请：active_slot 生成列 + uk_ai_profile_build_invite_pending
- §2.2 content_hash 不含 source_turn_ids
- §3.1 邀请门槛 4 turns / 3 dimensions / 3 high-confidence candidates
- §3.3 summary_items 至多 6 条，每条 1 维、content ≤ 80 字
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.ai_schema import PROFILE_DIMENSIONS

# 旅程阶段四值，Contract v1.1 §1.2，DB enum 同源。
JOURNEY_STAGES: tuple[str, ...] = ("chatting", "building", "ready", "published")
JOURNEY_STAGE_SET: frozenset[str] = frozenset(JOURNEY_STAGES)

# 邀请状态四值，与 ai_profile_build_invite.status enum 一致。
INVITE_STATUSES: tuple[str, ...] = ("pending", "accepted", "snoozed", "expired")

# 候选状态四值，与 ai_profile_candidate.status enum 一致。
CANDIDATE_STATUSES: tuple[str, ...] = ("active", "promoted", "dismissed", "expired")

# summary_items 内容最大长度（P1-UX 契约，summary 不展示超长原句）。
SUMMARY_CONTENT_MAX_LENGTH = 80

# 高置信度候选门槛（Contract v1.1 §3.1 邀请触发的候选数量条件）。
HIGH_CONFIDENCE_THRESHOLD = 0.75


@dataclass(frozen=True)
class CandidateRecord:
    """一行 ``ai_profile_candidate`` 的不可变领域对象。

    ``field_kind`` 决定 ``field_key`` 与 ``category`` 谁必须为非空：
    ``structured`` 必须有 ``field_key``；``entry`` 必须有 ``category``。
    该约束由 Pydantic schema（``CandidateRecordSchema``）做运行期校验，
    dataclass 自身不重复以避免在 P1-B 早期引入额外复杂度。
    """

    candidate_id: str
    session_id: str
    user_id: int
    subject: str
    profile_dimension: str
    field_kind: str
    field_key: str | None
    category: str | None
    content: str | None
    value: Any
    confidence: float
    source_turn_ids: tuple[str, ...]
    source_span: str | None
    consent_version: str
    policy_revision: str
    status: str = "active"
    content_hash: str = ""


class CandidateRecordSchema(BaseModel):
    """Pydantic 校验版 CandidateRecord——主要给 REST 出口与 LLM 抽取器使用。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., min_length=1, max_length=64)
    session_id: str = Field(..., min_length=1, max_length=64)
    user_id: int = Field(..., ge=1)
    subject: str = Field(..., pattern="^(personal|ideal_partner)$")
    profile_dimension: str = Field(..., min_length=1, max_length=64)
    field_kind: str = Field(..., pattern="^(structured|entry)$")
    field_key: str | None = None
    category: str | None = None
    content: str | None = None
    value: Any = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    source_turn_ids: tuple[str, ...] = ()
    source_span: str | None = Field(default=None, max_length=512)
    consent_version: str = Field(..., min_length=1, max_length=32)
    policy_revision: str = Field(..., min_length=1, max_length=64)
    status: str = Field(default="active", pattern="^(active|promoted|dismissed|expired)$")
    content_hash: str = Field(..., min_length=64, max_length=64)


class SummaryItem(BaseModel):
    """``build_invite`` 推送给前端的单条摘要项（Contract v1.1 §3.3）。"""

    model_config = ConfigDict(extra="forbid")

    profile_dimension: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., min_length=1, max_length=SUMMARY_CONTENT_MAX_LENGTH)


class SubjectSummary(BaseModel):
    """``MoxiangStateResponse`` 内每个主体的简要投影。

    完整字段集对齐 Contract v1.1 §6.1：会话 ID、旅程阶段、最后话题时间、
    六维理解进度、邀请/卡片/发布状态。service 层只填充候选理解度
    ``overall_percent`` 与 ``dimensions``；它们不表示确认或发布进度。
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., pattern="^(personal|ideal_partner)$")
    journey_stage: str = Field(..., pattern="^(chatting|building|ready|published)$")
    session_id: str | None = None
    last_turn_at: str | None = None
    last_topic_excerpt: str = ""
    overall_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    dimensions: dict[str, dict[str, float | int]] = Field(
        default_factory=lambda: {
            dim: {"percent": 0.0, "evidence_count": 0}
            for dim in PROFILE_DIMENSIONS
        }
    )
    pending_build_invite_id: str | None = None
    pending_confirm_card: bool = False
    published_revision_id: int | None = None
    # P1-B invite 计数（前端的可发现性提示，前端不直接用做 UI 计算）。
    effective_turn_count: int = Field(default=0, ge=0)
    dimension_count: int = Field(default=0, ge=0)
    high_confidence_candidate_count: int = Field(default=0, ge=0)
    auto_invite_count: int = Field(default=0, ge=0)
    has_pending_invite: bool = False
    # Phase 2 P2-01/P2-04 —— ideal_partner 主体专用:
    #   True 表示用户当前已具备开启/继续愿遇之相的条件;
    #   personal 主体该字段恒为 False(它本身不能自己"开启自己")。
    # 判定规则见 ``app.services.ai.moxiang_state``:
    #   - ideal_partner 已有活动会话 / 草稿 / 历史 revision → 永远 True(老用户恢复);
    #   - 否则 personal 已发布(published_revision_id 非空)→ True;
    #   - 其余情况 False(锁定图标)。
    can_start_ideal_partner: bool = False


class MoxiangStateResponse(BaseModel):
    """``GET /api/v1/ai/moxiang/state`` 的契约（snake_case 版）。

    顶层字段对齐 Contract v1.1 §6.1：``personal`` 与 ``ideal_partner`` 是
    顶层键，方便前端直接 ``state.personal`` / ``state.ideal_partner`` 索引。
    ``consent_granted`` 与 ``active_subject`` 是状态机辅助字段，未授权
    时前端用 ``consent_granted`` 走授权引导路径。
    """

    model_config = ConfigDict(extra="forbid")

    consent_granted: bool
    active_subject: str = Field(..., pattern="^(personal|ideal_partner)$")
    personal: SubjectSummary
    ideal_partner: SubjectSummary
    # 该字段让前端可重放历史（与 ai_profile_session.journey_stage 同步）。
    last_assessed_at: str | None = None
    # Phase 2 P2-01/P2-04 —— 顶层冗余字段,前端不必深入 ideal_partner 就能
    # 拿到"是否能开启愿遇之相"。恒等于 ``ideal_partner.can_start_ideal_partner``。
    can_start_ideal_partner: bool = False


class MoxiangTurn(BaseModel):
    """``GET /api/v1/ai/profile-sessions/{session_id}/turns`` 单条历史 turn。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    turn_no: int = Field(..., ge=1)
    role: str = Field(..., pattern="^(user|assistant)$")
    answer_text: str = Field(..., min_length=1, max_length=2000)
    client_turn_id: str
    created_at: str | None = None


class MoxiangTurnsResponse(BaseModel):
    """``GET /api/v1/ai/profile-sessions/{session_id}/turns`` 响应。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    subject: str
    turns: tuple[MoxiangTurn, ...] = ()
    next_before_turn_no: int | None = None  # 游标分页；null 表示已到末尾


# ----------------------------------------------------------------------
# 邀请域对象（create_pending_invite / resolve_invite 返回值）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BuildInviteRecord:
    """一行 ``ai_profile_build_invite`` 的不可变领域对象。"""

    invite_id: str
    session_id: str
    user_id: int
    subject: str
    status: str
    trigger_kind: str
    invite_no: int
    summary_json: tuple[dict[str, Any], ...]
    effective_turn_count_at_create: int
    dimension_count: int
    candidate_count: int
    snoozed_at_effective_turn_count: int | None = None
    accepted_at: str | None = None
    snoozed_at: str | None = None
    expired_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def validate_profile_dimension(value: str) -> str:
    """白名单校验：未知维度拒绝（Contract v1.1 §1.3）。"""
    if value not in PROFILE_DIMENSIONS:
        raise ValueError(
            f"profile_dimension must be one of {PROFILE_DIMENSIONS!r}, got {value!r}"
        )
    return value
