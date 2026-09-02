"""墨相师构建邀请服务（Contract v1.1 §3）。

负责把候选理解池 + 旅程状态转化为"邀请整理"的输出物：

- 门槛判定：``should_offer_invite`` 复述 Contract §3.1 全部四条（4 turns
  / 3 dimensions / 3 high-confidence / 2 auto cap）。
- 摘要塑造：``build_invite_summary`` 折叠为最多 6 条（每维 1 条），
  ``content`` 截断到 ``SUMMARY_CONTENT_MAX_LENGTH = 80`` 字。
- 邀请创建：``create_pending_invite`` 调用仓储协议
  ``CandidateRepository``：若已存在 pending 行则原样回放，**绝不**插入
  第二条 pending（DDL ``uk_ai_profile_build_invite_pending`` 兜底）。
- 邀请终结：``resolve_invite(accepted | snoozed)`` 切换 status、写入
  accepted_at / snoozed_at；其它值抛 ``AIInputError``。

本模块**不**持有数据库连接——P1-C 在路由层用 ``ai_profile_candidate``
/ ``ai_profile_build_invite`` 表的真实适配器实现
``CandidateRepository`` 协议，本服务只依赖纯函数 + 协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.ai_moxiang import (
    SUMMARY_CONTENT_MAX_LENGTH,
    BuildInviteRecord,
    CandidateRecord,
    SummaryItem,
)

# Contract v1.1 §3.1 邀请触发门槛。append-only；旧邀请不可被新门槛追溯。
MIN_EFFECTIVE_TURNS = 4
MIN_DIMENSION_COUNT = 3
MIN_HIGH_CONFIDENCE_CANDIDATES = 3
MAX_AUTO_INVITES_PER_SESSION = 2

# 摘要最多条数（每维 1 条，六维上限 6）。
SUMMARY_MAX_ITEMS = 6


class AIInputError(Exception):
    """400 AI_INPUT_INVALID：邀请终结入参非法（resolution 不在白名单）。"""

    code = "AI_INPUT_INVALID"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class InviteBuildResult:
    """``create_pending_invite`` 的返回值（来自仓储或新建）。"""

    invite: BuildInviteRecord
    already_pending: bool


# ----------------------------------------------------------------------
# 门槛
# ----------------------------------------------------------------------


def should_offer_invite(
    *,
    effective_turn_count: int,
    dimension_count: int,
    high_confidence_candidate_count: int,
    auto_invite_count: int,
) -> bool:
    """邀请是否应该发出——四条全部满足才返回 True。"""
    if effective_turn_count < MIN_EFFECTIVE_TURNS:
        return False
    if dimension_count < MIN_DIMENSION_COUNT:
        return False
    if high_confidence_candidate_count < MIN_HIGH_CONFIDENCE_CANDIDATES:
        return False
    if auto_invite_count >= MAX_AUTO_INVITES_PER_SESSION:
        return False
    return True


# ----------------------------------------------------------------------
# 摘要
# ----------------------------------------------------------------------


def build_invite_summary(
    candidates: tuple[CandidateRecord, ...],
) -> tuple[SummaryItem, ...]:
    """把候选池折叠为前端展示用的摘要（最多六条，每维 1 条）。

    排序规则：按 confidence 降序，再按 profile_dimension 字符串升序稳定
    排序。截断 ``content`` 至 ``SUMMARY_CONTENT_MAX_LENGTH`` 字。
    """
    if not candidates:
        return ()

    # 每维只保留 confidence 最高的一条（避免同维 2 条挤占他维）。
    per_dimension: dict[str, CandidateRecord] = {}
    for record in candidates:
        existing = per_dimension.get(record.profile_dimension)
        if existing is None or record.confidence > existing.confidence:
            per_dimension[record.profile_dimension] = record

    ranked = sorted(
        per_dimension.values(),
        key=lambda r: (-r.confidence, r.profile_dimension),
    )
    sliced = ranked[:SUMMARY_MAX_ITEMS]
    items: list[SummaryItem] = []
    for record in sliced:
        text = (record.content or "").strip()
        if not text:
            # entry 类候选可降级为 field_key 标签；structured 必有 content。
            text = record.field_key or record.category or record.profile_dimension
        if len(text) > SUMMARY_CONTENT_MAX_LENGTH:
            text = text[:SUMMARY_CONTENT_MAX_LENGTH]
        items.append(
            SummaryItem(
                profile_dimension=record.profile_dimension,
                content=text,
            )
        )
    return tuple(items)


# ----------------------------------------------------------------------
# 创建
# ----------------------------------------------------------------------


def create_pending_invite(
    *,
    repo: Any,
    session_id: str,
    user_id: int,
    subject: str,
    effective_turn_count: int,
    dimension_count: int,
    candidate_count: int,
    summary_items: tuple[SummaryItem, ...] | None = None,
) -> InviteBuildResult:
    """创建或回放一条 pending 邀请。

    已存在 pending：直接回放（不创建第二条）；DDL 兜底
    ``uk_ai_profile_build_invite_pending`` 阻止并发创建。
    """
    existing = repo.find_pending_invite(session_id)
    if existing is not None:
        return InviteBuildResult(
            invite=_record_from_repo(existing), already_pending=True
        )

    invite_no = repo.next_invite_no(session_id)
    summary_json = tuple(
        item.model_dump() for item in (summary_items or ())
    )
    row = repo.insert_pending(
        session_id=session_id,
        user_id=user_id,
        subject=subject,
        invite_no=invite_no,
        summary_json=summary_json,
        effective_turn_count=effective_turn_count,
        dimension_count=dimension_count,
        candidate_count=candidate_count,
    )
    return InviteBuildResult(
        invite=_record_from_repo(row), already_pending=False
    )


# ----------------------------------------------------------------------
# 终结
# ----------------------------------------------------------------------


def resolve_invite(
    repo: Any,
    invite_id: str,
    *,
    user_id: int,
    resolution: str,
    at_effective_turn_count: int | None = None,
) -> BuildInviteRecord:
    """接受或延迟一条邀请。resolution 必须在 ``INVITE_STATUSES`` 白名单内。

    非法值抛 ``AIInputError``；仓储层负责 active_slot 守卫（DDL
    ``uk_ai_profile_build_invite_pending`` 拒绝把已终态再开回 pending）。
    """
    if resolution not in {"accepted", "snoozed"}:
        raise AIInputError(
            f"resolution must be accepted or snoozed, got {resolution!r}"
        )
    invite = repo.get_invite(invite_id)
    if invite is None:
        raise AIInputError("邀请不存在或已过期")
    if int(getattr(invite, "user_id", 0)) != int(user_id):
        raise AIInputError("无权操作该邀请")
    if getattr(invite, "status", None) != "pending":
        raise AIInputError("该邀请已终结，请勿重复操作")
    repo.mark_resolved(invite_id, resolution)
    if resolution == "snoozed" and at_effective_turn_count is not None:
        repo.mark_snoozed_at_effective_turn_count(
            invite_id, int(at_effective_turn_count)
        )
    refreshed = repo.get_invite(invite_id)
    return _record_from_repo(refreshed)


# ----------------------------------------------------------------------
# 辅助：仓储对象 → 领域对象
# ----------------------------------------------------------------------


def _record_from_repo(row: Any) -> BuildInviteRecord:
    """把仓储层 dataclass / dict / 命名行归一为 BuildInviteRecord。"""
    if isinstance(row, dict):
        data = row
    else:
        data = {key: getattr(row, key) for key in (
            "invite_id", "session_id", "user_id", "subject", "status",
            "trigger_kind", "invite_no", "summary_json",
            "effective_turn_count_at_create", "dimension_count",
            "candidate_count", "snoozed_at_effective_turn_count",
            "accepted_at", "snoozed_at", "expired_at", "created_at",
            "updated_at",
        ) if hasattr(row, key)}
    summary_raw = data.get("summary_json") or ()
    if isinstance(summary_raw, str):
        import json
        try:
            summary_raw = json.loads(summary_raw)
        except ValueError:
            summary_raw = ()
    summary = tuple(summary_raw) if isinstance(summary_raw, (list, tuple)) else ()
    return BuildInviteRecord(
        invite_id=str(data.get("invite_id") or ""),
        session_id=str(data.get("session_id") or ""),
        user_id=int(data.get("user_id") or 0),
        subject=str(data.get("subject") or ""),
        status=str(data.get("status") or "pending"),
        trigger_kind=str(data.get("trigger_kind") or "auto"),
        invite_no=int(data.get("invite_no") or 0),
        summary_json=summary,
        effective_turn_count_at_create=int(
            data.get("effective_turn_count_at_create") or 0
        ),
        dimension_count=int(data.get("dimension_count") or 0),
        candidate_count=int(data.get("candidate_count") or 0),
        snoozed_at_effective_turn_count=data.get(
            "snoozed_at_effective_turn_count"
        ),
        accepted_at=_iso_or_none(data.get("accepted_at")),
        snoozed_at=_iso_or_none(data.get("snoozed_at")),
        expired_at=_iso_or_none(data.get("expired_at")),
        created_at=_iso_or_none(data.get("created_at")),
        updated_at=_iso_or_none(data.get("updated_at")),
    )


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
