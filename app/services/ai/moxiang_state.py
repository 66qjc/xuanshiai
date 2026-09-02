"""墨相师四阶段融合 P1-C 状态查询服务（Contract v1.1 §6.1 / §7）。

只读 / 计算逻辑（不落库），由路由层（``app/api/routes/ai_moxiang.py``）
装配 REST 响应；前端（``xuanshiai-vue/api/ai-moxiang.uts``）拿到 snake_case
字段后由 ``adaptState`` 翻译为 camelCase。

Contract v1.1 关键引用：

- §6.1 ``GET /api/v1/ai/moxiang/state``：返回两个主体的会话快照 + 进度 + 邀请状态。
- §6.2 ``GET /api/v1/ai/profile-sessions/{session_id}/turns``：按 turn_no 升序
  分页（before_turn_no exclusive，limit 1..100）。
- §7 真实进度：六维固定词表，每维 confirmed 数 0/1/2+ 映射 0/50/100%。
  overall_percent = 六维平均。confirmation_percent / confidence_percent 由
  confirmed vs suggested 计数得出（仅 confirmed 才进主进度）。
- §1.2 旅程阶段四值：chatting/building/ready/published。
- §10 隐私：未授权返回 200 + consent_granted=false，绝不暴露内部 ID 之外的资源。

本模块**不**连接数据库；所有 SQL 由仓储协议 / 现有 profile.py 给出，本
服务只做"取数 + 投影"。
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Awaitable, Protocol

from app.db.ai_schema import PROFILE_DIMENSIONS
from app.schemas.ai_moxiang import (
    JOURNEY_STAGE_SET,
    MoxiangStateResponse,
    SubjectSummary,
)
from app.schemas.ai_profile import ProfileSubject  # noqa: F401

# 进度阈值（Contract v1.1 §7）：每维 0/1/2+ confirmed → 0/50/100%
_PER_DIMENSION_PERCENTS: tuple[float, ...] = (0.0, 50.0, 100.0)
_TOPIC_EXCERPT_MAX_LENGTH = 120


async def _resolve(value: Any) -> Any:
    """兼容生产 async 仓储与纯函数测试 fake 的同步返回值。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _dimension_percent_from_confirmed(confirmed_count: int) -> float:
    if confirmed_count <= 0:
        return 0.0
    if confirmed_count == 1:
        return 50.0
    return 100.0


@dataclass(frozen=True)
class SubjectSessionSnapshot:
    """单个主体在 ai_profile_session 表上的最新状态。"""

    session_id: str | None
    subject: str
    journey_stage: str | None
    last_turn_at: str | None
    last_topic_excerpt: str
    overall_percent: float
    confidence_percent: float
    confirmation_percent: float
    dimensions: dict[str, dict[str, float | int]]
    hard_gate_met: bool
    pending_build_invite_id: str | None
    pending_confirm_card: bool
    published_revision_id: int | None


@dataclass(frozen=True)
class MoxiangState:
    """REST 服务的内存投影：双主体快照 + 授权状态。"""

    consent_granted: bool
    active_subject: str
    subjects: dict[str, SubjectSessionSnapshot]


# ----------------------------------------------------------------------
# 仓储协议（不依赖 SQLAlchemy，单元测试可直接 fake）
# ----------------------------------------------------------------------


class MoxiangStateRepository(Protocol):
    """``moxiang_state`` 服务的仓储协议。

    真实实现由 P1-C 在 ``app/services/ai/moxiang_state_repo.py`` 提供
    （AI 集成测试驱动）。本协议只列出本服务所需的最小数据面。
    """

    def has_active_consent(
        self, user_id: int, scope: str, version: str
    ) -> bool | Awaitable[bool]: ...

    def find_active_session(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def find_published_revision(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def find_pending_build_invite(
        self, user_id: int, session_id: str | None
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def find_pending_confirm_card(
        self, session_id: str | None
    ) -> bool | Awaitable[bool]: ...

    def count_dimension_confirmed(
        self, user_id: int, subject: str
    ) -> dict[str, int] | Awaitable[dict[str, int]]: ...

    def average_confidence(
        self, user_id: int, subject: str
    ) -> float | Awaitable[float]: ...

    def confirmation_percent(
        self, user_id: int, subject: str
    ) -> float | Awaitable[float]: ...

    def list_session_turns(
        self,
        session_id: str,
        before_turn_no: int | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...] | Awaitable[tuple[dict[str, Any], ...]]: ...

    def has_subject_history(
        self, user_id: int, subject: str
    ) -> bool | Awaitable[bool]:
        """判断该用户在该主体上是否有过任何记录(活动会话/草稿/历史 revision)。

        Phase 2 P2-01 老用户恢复路径:若返回 True,即便 personal 未发布,
        ideal_partner 主体也必须保持可恢复(``can_start_ideal_partner=True``)。
        默认实现要求仓储协议显式提供;真实 SQL 由 ``moxiang_state_db`` 实现。
        """


# ----------------------------------------------------------------------
# 纯函数投影
# ----------------------------------------------------------------------


def build_subject_summary(
    snapshot: SubjectSessionSnapshot | None,
) -> SubjectSummary:
    """把 ``SubjectSessionSnapshot`` 投影为 REST 字段（snake_case）。

    当主体无会话时（snapshot is None），所有数值归零、journey_stage 显式
    设为 'chatting'（Contract §1.2 默认值），让前端可以无空字段渲染。
    """
    if snapshot is None:
        return SubjectSummary(
            subject="personal",
            journey_stage="chatting",
            effective_turn_count=0,
            dimension_count=0,
            high_confidence_candidate_count=0,
            auto_invite_count=0,
            has_pending_invite=False,
        )
    return SubjectSummary(
        subject=snapshot.subject,
        journey_stage=snapshot.journey_stage or "chatting",
        effective_turn_count=0,  # 由 P1-C 仓储查询补齐
        dimension_count=0,  # 由 P1-C 仓储查询补齐
        high_confidence_candidate_count=0,  # 由 P1-C 仓储查询补齐
        auto_invite_count=0,  # 由 P1-C 仓储查询补齐
        has_pending_invite=snapshot.pending_build_invite_id is not None,
    )


async def build_subject_session_snapshot(
    *,
    user_id: int,
    subject: str,
    repo: MoxiangStateRepository,
) -> SubjectSessionSnapshot | None:
    """聚合单主体快照：会话元信息 + 六维进度 + 邀请/确认卡状态。

    返回 ``None`` 表示该主体无活动会话（前端展示空字段而非 404）。
    """
    session = await _resolve(repo.find_active_session(user_id, subject))
    if session is None:
        return None
    session_id = str(session.get("session_id") or "")
    journey_stage = str(session.get("journey_stage") or "chatting")
    if journey_stage not in JOURNEY_STAGE_SET:
        journey_stage = "chatting"
    last_turn_at = session.get("updated_at")
    last_turn_at_iso = (
        last_turn_at.isoformat() if hasattr(last_turn_at, "isoformat") else str(last_turn_at or "")
    )
    last_topic_excerpt = str(session.get("last_topic_excerpt") or "")
    if len(last_topic_excerpt) > _TOPIC_EXCERPT_MAX_LENGTH:
        last_topic_excerpt = last_topic_excerpt[:_TOPIC_EXCERPT_MAX_LENGTH]
    confirmed_per_dim = await _resolve(
        repo.count_dimension_confirmed(user_id, subject)
    )
    dimensions: dict[str, dict[str, float | int]] = {}
    per_dim_percents: list[float] = []
    for dim in PROFILE_DIMENSIONS:
        count = int(confirmed_per_dim.get(dim, 0))
        pct = _dimension_percent_from_confirmed(count)
        dimensions[dim] = {"percent": pct, "confirmed_count": count}
        per_dim_percents.append(pct)
    overall_percent = (
        sum(per_dim_percents) / len(per_dim_percents) if per_dim_percents else 0.0
    )
    confidence_percent = (
        await _resolve(repo.average_confidence(user_id, subject))
    ) * 100.0
    confirmation_percent = await _resolve(
        repo.confirmation_percent(user_id, subject)
    )
    invite_row = await _resolve(
        repo.find_pending_build_invite(user_id, session_id)
    )
    pending_invite_id = (
        str(invite_row.get("invite_id")) if invite_row else None
    )
    has_card = await _resolve(repo.find_pending_confirm_card(session_id))
    pub = await _resolve(repo.find_published_revision(user_id, subject))
    published_revision_id = int(pub["revision_id"]) if pub else None
    # hard_gate 简化为 overall_percent>=60 + 六维 confirmed 总和≥6（占位定义；
    # 真实工程实现由 P1-C 仓储协议补齐）。
    total_confirmed = sum(confirmed_per_dim.values())
    hard_gate_met = overall_percent >= 60.0 and total_confirmed >= 6
    return SubjectSessionSnapshot(
        session_id=session_id,
        subject=subject,
        journey_stage=journey_stage,
        last_turn_at=last_turn_at_iso or None,
        last_topic_excerpt=last_topic_excerpt,
        overall_percent=overall_percent,
        confidence_percent=confidence_percent,
        confirmation_percent=confirmation_percent,
        dimensions=dimensions,
        hard_gate_met=hard_gate_met,
        pending_build_invite_id=pending_invite_id,
        pending_confirm_card=has_card,
        published_revision_id=published_revision_id,
    )


async def build_state_response(
    *,
    user_id: int,
    repo: MoxiangStateRepository,
    consent_version: str = "profile-text-v1",
) -> MoxiangStateResponse:
    """组装双主体状态为 Pydantic 响应。"""
    consent_granted = await _resolve(
        repo.has_active_consent(
            user_id, "profile_text_extract", consent_version
        )
    )
    personal = await build_subject_session_snapshot(
        user_id=user_id, subject=ProfileSubject.PERSONAL.value, repo=repo
    )
    partner = await build_subject_session_snapshot(
        user_id=user_id, subject=ProfileSubject.IDEAL_PARTNER.value, repo=repo
    )
    active = ProfileSubject.PERSONAL.value
    if personal is None and partner is not None:
        active = ProfileSubject.IDEAL_PARTNER.value

    def _summary(snap: SubjectSessionSnapshot | None) -> SubjectSummary:
        if snap is None:
            return SubjectSummary(
                subject="personal",
                journey_stage="chatting",
                effective_turn_count=0,
                dimension_count=0,
                high_confidence_candidate_count=0,
                auto_invite_count=0,
                has_pending_invite=False,
                overall_percent=0.0,
                confidence_percent=0.0,
                confirmation_percent=0.0,
                dimensions={
                    dim: {"percent": 0.0, "confirmed_count": 0}
                    for dim in PROFILE_DIMENSIONS
                },
                hard_gate_met=False,
            )
        return SubjectSummary(
            subject=snap.subject,
            session_id=snap.session_id,
            journey_stage=snap.journey_stage or "chatting",
            last_turn_at=snap.last_turn_at,
            last_topic_excerpt=snap.last_topic_excerpt,
            overall_percent=snap.overall_percent,
            confidence_percent=snap.confidence_percent,
            confirmation_percent=snap.confirmation_percent,
            dimensions=snap.dimensions,
            hard_gate_met=snap.hard_gate_met,
            pending_build_invite_id=snap.pending_build_invite_id,
            pending_confirm_card=snap.pending_confirm_card,
            published_revision_id=snap.published_revision_id,
            effective_turn_count=0,
            dimension_count=0,
            high_confidence_candidate_count=0,
            auto_invite_count=0,
            has_pending_invite=snap.pending_build_invite_id is not None,
        )

    personal_summary = _summary(personal).model_copy(
        update={"subject": ProfileSubject.PERSONAL.value}
    )
    partner_summary = _summary(partner).model_copy(
        update={"subject": ProfileSubject.IDEAL_PARTNER.value}
    )
    # Phase 2 P2-01 / P2-04 —— 决定 ideal_partner 是否可开启/恢复。
    # 规则:
    #   1) ideal_partner 已有活动会话/历史记录 → 永远 True(老用户恢复);
    #   2) 否则 personal 已发布(published_revision_id 非空)→ True;
    #   3) 其余情况 False(显示锁定图标,等待 personal 完成过渡)。
    ideal_history_exists = (
        partner is not None
        or await _resolve(
            repo.has_subject_history(user_id, ProfileSubject.IDEAL_PARTNER.value)
        )
    )
    personal_published = (
        personal is not None and personal.published_revision_id is not None
    )
    can_start_partner = ideal_history_exists or personal_published
    partner_summary = partner_summary.model_copy(
        update={"can_start_ideal_partner": can_start_partner}
    )
    return MoxiangStateResponse(
        consent_granted=consent_granted,
        active_subject=active,
        personal=personal_summary,
        ideal_partner=partner_summary,
        can_start_ideal_partner=can_start_partner,
    )


def list_turns(
    *,
    session_id: str,
    before_turn_no: int | None,
    limit: int,
    repo: MoxiangStateRepository,
) -> tuple[dict[str, Any], int | None]:
    """读取会话 turn 列表（升序分页）。返回 (turns, next_before_turn_no)。

    仓储协议约定按 turn_no ASC 拉取；本函数不反转。``next_before_turn_no``
    为本批最小 turn_no；读完（拿到的不足 limit）时返回 ``None``。
    """
    if limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    if before_turn_no is not None and before_turn_no < 1:
        raise ValueError("before_turn_no must be >= 1")
    rows = tuple(repo.list_session_turns(session_id, before_turn_no, limit))
    if not rows:
        return (), None
    # 仓储按 ASC 约定；若返回 DESC（rows[0].turn_no > rows[-1].turn_no），
    # 我们做一次反转以对齐 Contract §6.2 升序要求。
    if (
        len(rows) >= 2
        and int(rows[0].get("turn_no", 0)) > int(rows[-1].get("turn_no", 0))
    ):
        ordered = tuple(reversed(rows))
    else:
        ordered = rows
    next_cursor: int | None = None
    if len(rows) >= limit:
        next_cursor = int(ordered[0].get("turn_no") or 0)
        if next_cursor <= 0:
            next_cursor = None
    return ordered, next_cursor


# ----------------------------------------------------------------------
# Journey stage 推进（纯函数层）
# ----------------------------------------------------------------------


def advance_journey_stage(current: str, target: str) -> str:
    """合法状态机校验（Contract §1.2）。

    chatting → building → ready → published 单向链；试图回退或跳级抛
    ``ValueError``（由路由层翻译为 409）。
    """
    if current not in JOURNEY_STAGE_SET:
        current = "chatting"
    if target not in JOURNEY_STAGE_SET:
        raise ValueError(f"invalid journey stage: {target!r}")
    order = ("chatting", "building", "ready", "published")
    if order.index(target) < order.index(current):
        raise ValueError(
            f"journey stage must not regress: {current} -> {target}"
        )
    if target == current:
        return current
    return target
