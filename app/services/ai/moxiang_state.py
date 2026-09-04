"""墨相师四阶段融合 P1-C 状态查询服务（Contract v1.1 §6.1 / §7）。

只读 / 计算逻辑（不落库），由路由层（``app/api/routes/ai_moxiang.py``）
装配 REST 响应；前端（``xuanshiai-vue/api/ai-moxiang.uts``）拿到 snake_case
字段后由 ``adaptState`` 翻译为 camelCase。

Contract v1.1 关键引用：

- §6.1 ``GET /api/v1/ai/moxiang/state``：返回两个主体的会话快照 + 进度 + 邀请状态。
- §6.2 ``GET /api/v1/ai/profile-sessions/{session_id}/turns``：按 turn_no 升序
  分页（before_turn_no exclusive，limit 1..100）。
- §7 真实进度：六维固定词表，每维有效高置信候选数 0/1/2+ 映射 0/50/100%。
  overall_percent = 六维平均；确认/发布状态不参与该口径。
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
from app.services.ai.journey_progress import calculate_journey_progress

_TOPIC_EXCERPT_MAX_LENGTH = 120


async def _resolve(value: Any) -> Any:
    """兼容生产 async 仓储与纯函数测试 fake 的同步返回值。"""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class SubjectSessionSnapshot:
    """单个主体在 ai_profile_session 表上的最新状态。"""

    session_id: str | None
    subject: str
    journey_stage: str | None
    last_turn_at: str | None
    last_topic_excerpt: str
    overall_percent: float
    dimensions: dict[str, dict[str, float | int]]
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

    def list_session_candidates(
        self, session_id: str
    ) -> tuple[Any, ...] | Awaitable[tuple[Any, ...]]: ...

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
    # 新旅程仓储以候选池作为实时进度唯一来源；兼容尚未迁移的
    # 旧仓储/fake 时回退到 confirmed 计数，避免状态页因缺少新方法直接 500。
    candidate_loader = getattr(repo, "list_session_candidates", None)
    if candidate_loader is None:
        confirmed_loader = getattr(repo, "count_dimension_confirmed", None)
        confirmed = (
            await _resolve(confirmed_loader(user_id, subject))
            if confirmed_loader is not None
            else {}
        )

        def _confirmed_percent(dimension: str) -> float:
            count = int(confirmed.get(dimension, 0))
            if count >= 2:
                return 100.0
            if count == 1:
                return 50.0
            return 0.0

        dimensions = {
            dim: {
                "percent": _confirmed_percent(dim),
                "evidence_count": int(confirmed.get(dim, 0)),
            }
            for dim in PROFILE_DIMENSIONS
        }
        overall_percent = sum(
            item["percent"] for item in dimensions.values()
        ) / len(dimensions)
    else:
        candidates = await _resolve(candidate_loader(session_id))
        candidate_progress = calculate_journey_progress(candidates)
        dimensions = {
            dim: {
                "percent": candidate_progress.dimensions[dim].percent,
                "evidence_count": candidate_progress.dimensions[dim].evidence_count,
            }
            for dim in PROFILE_DIMENSIONS
        }
        overall_percent = candidate_progress.overall_percent
    invite_row = await _resolve(
        repo.find_pending_build_invite(user_id, session_id)
    )
    pending_invite_id = (
        str(invite_row.get("invite_id")) if invite_row else None
    )
    has_card = await _resolve(repo.find_pending_confirm_card(session_id))
    pub = await _resolve(repo.find_published_revision(user_id, subject))
    published_revision_id = int(pub["revision_id"]) if pub else None
    return SubjectSessionSnapshot(
        session_id=session_id,
        subject=subject,
        journey_stage=journey_stage,
        last_turn_at=last_turn_at_iso or None,
        last_topic_excerpt=last_topic_excerpt,
        overall_percent=overall_percent,
        dimensions=dimensions,
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
                dimensions={
                    dim: {"percent": 0.0, "evidence_count": 0}
                    for dim in PROFILE_DIMENSIONS
                },
            )
        return SubjectSummary(
            subject=snap.subject,
            session_id=snap.session_id,
            journey_stage=snap.journey_stage or "chatting",
            last_turn_at=snap.last_turn_at,
            last_topic_excerpt=snap.last_topic_excerpt,
            overall_percent=snap.overall_percent,
            dimensions=snap.dimensions,
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
    # 发布后 session 置 active_status=0，snapshot 变 None，但 revision 仍在。
    # 无活动会话时也要暴露 published_revision_id：档案成稿入口、双阶段卡
    # 「已发布」与 can_start_ideal_partner 判定都依赖这一事实。
    async def _published_id(
        snap: SubjectSessionSnapshot | None, subject: str
    ) -> int | None:
        if snap is not None:
            return snap.published_revision_id
        pub = await _resolve(repo.find_published_revision(user_id, subject))
        return int(pub["revision_id"]) if pub else None

    personal_published_id = await _published_id(
        personal, ProfileSubject.PERSONAL.value
    )
    partner_published_id = await _published_id(
        partner, ProfileSubject.IDEAL_PARTNER.value
    )
    personal_summary = personal_summary.model_copy(
        update={"published_revision_id": personal_published_id}
    )
    partner_summary = partner_summary.model_copy(
        update={"published_revision_id": partner_published_id}
    )
    # Phase 2 P2-01 / P2-04 —— 决定 ideal_partner 是否可开启/恢复。
    # 规则:
    #   1) ideal_partner 已有活动会话/历史记录 → 永远 True(老用户恢复);
    #   2) 否则 personal 已发布(published_revision_id 非空)→ True;
    #   3) 其余情况 False(显示锁定图标,等待 personal 完成过渡)。
    ideal_history_exists = (
        partner is not None
        or partner_published_id is not None
        or await _resolve(
            repo.has_subject_history(user_id, ProfileSubject.IDEAL_PARTNER.value)
        )
    )
    personal_published = personal_published_id is not None
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


async def list_turns(
    *,
    session_id: str,
    before_turn_no: int | None,
    limit: int,
    repo: MoxiangStateRepository,
) -> tuple[dict[str, Any], int | None]:
    """读取会话 turn 列表（升序分页）。返回 (turns, next_before_turn_no)。

    仓储取不超过 ``limit + 1`` 条最新记录用于判断是否还有更早一页；本函数
    统一按 ``turn_no`` 升序输出。``next_before_turn_no`` 为响应页最小 turn_no，
    读完时返回 ``None``。
    """
    if limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    if before_turn_no is not None and before_turn_no < 1:
        raise ValueError("before_turn_no must be >= 1")
    rows = tuple(
        await _resolve(
            repo.list_session_turns(session_id, before_turn_no, limit + 1)
        )
    )
    if not rows:
        return (), None
    ordered = tuple(sorted(rows, key=lambda row: int(row.get("turn_no") or 0)))
    has_older = len(ordered) > limit
    page = ordered[-limit:] if has_older else ordered
    next_cursor: int | None = None
    if has_older:
        next_cursor = int(page[0].get("turn_no") or 0)
        if next_cursor <= 0:
            next_cursor = None
    return page, next_cursor


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
