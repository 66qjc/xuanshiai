"""Phase 2 moxiang subject-journey tests (P2-01 / P2-04).

覆盖要点：

- ``MoxiangSubjectSummary.can_start_ideal_partner`` 字段存在且布尔。
- 新用户 personal 未发布 → ideal_partner 摘要 ``can_start_ideal_partner=False``。
- personal 已发布 → ideal_partner 摘要 ``can_start_ideal_partner=True``。
- 已有 ideal_partner 会话/草稿/历史 revision 的"老用户"始终可恢复
  （``can_start_ideal_partner`` 必须为 True，无论 personal 状态）。
- ``build_state_response`` 在两种场景下行为正确：理想伴侣已是 active 主体时
  personal 仍可独立查询。

测试策略：与现有 ``test_ai_moxiang_state_service.py`` 一致，注入 fake
repository，service 内部从快照派生布尔结果。
"""

from __future__ import annotations

import asyncio
from typing import Any


def _await(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRepo:
    def __init__(
        self,
        *,
        consent_granted: bool = True,
        sessions: dict[tuple[int, str], dict] | None = None,
        dimensions: dict[tuple[int, str], dict[str, int]] | None = None,
        pending_invites: dict[tuple[int, str], dict] | None = None,
        pending_confirm_cards: set[str] | None = None,
        published_revisions: dict[tuple[int, str], dict] | None = None,
        avg_confidence: float = 0.0,
        confirmation_pct: float = 0.0,
        turns: dict[str, list[dict]] | None = None,
        legacy_ideal_resume: bool = False,
    ):
        self.consent_granted = consent_granted
        self.sessions = sessions or {}
        self.dimensions = dimensions or {}
        self.pending_invites = pending_invites or {}
        self.pending_confirm_cards = pending_confirm_cards or set()
        self.published_revisions = published_revisions or {}
        self.avg_confidence = avg_confidence
        self.confirmation_pct = confirmation_pct
        self.turns = turns or {}
        self.legacy_ideal_resume = legacy_ideal_resume

    def has_active_consent(self, user_id, scope, version):
        return self.consent_granted

    def find_active_session(self, user_id, subject):
        return self.sessions.get((user_id, subject))

    def find_published_revision(self, user_id, subject):
        return self.published_revisions.get((user_id, subject))

    def find_pending_build_invite(self, user_id, session_id):
        for (uid, _), row in self.pending_invites.items():
            if uid == user_id and row.get("session_id") == session_id:
                return row
        return None

    def find_pending_confirm_card(self, session_id):
        return session_id in self.pending_confirm_cards

    def count_dimension_confirmed(self, user_id, subject):
        return self.dimensions.get((user_id, subject), {})

    def average_confidence(self, user_id, subject):
        return self.avg_confidence

    def confirmation_percent(self, user_id, subject):
        return self.confirmation_pct

    def list_session_turns(self, session_id, before_turn_no, limit):
        rows = self.turns.get(session_id, [])
        if before_turn_no is not None:
            rows = [r for r in rows if int(r.get("turn_no", 0)) < int(before_turn_no)]
        rows = sorted(rows, key=lambda r: int(r.get("turn_no", 0)))
        return tuple(rows[:limit])

    def has_subject_history(self, user_id, subject):
        # 默认 fake:若该 user+subject 有会话/草稿/历史 revision → True。
        if (user_id, subject) in self.sessions:
            return True
        if (user_id, subject) in self.published_revisions:
            return True
        return False


def _personal_draft_session() -> dict:
    return {
        "session_id": "s-personal-1",
        "subject": "personal",
        "journey_stage": "building",
        "updated_at": "2026-09-01T10:00:00Z",
        "last_topic_excerpt": "我在关系里比较慢热",
    }


def _ideal_draft_session() -> dict:
    return {
        "session_id": "s-ideal-1",
        "subject": "ideal_partner",
        "journey_stage": "chatting",
        "updated_at": "2026-09-01T09:00:00Z",
        "last_topic_excerpt": "",
    }


def test_schema_exposes_can_start_field() -> None:
    """Schema 字段必须存在；旧字段保持不变。"""
    from app.schemas.ai_moxiang import SubjectSummary

    fields = SubjectSummary.model_fields
    assert "can_start_ideal_partner" in fields
    assert fields["can_start_ideal_partner"].annotation is bool
    assert "journey_stage" in fields
    assert "overall_percent" in fields


def test_new_user_cannot_start_ideal_partner() -> None:
    """新用户 personal 还没开始 → ideal_partner 不可开启。"""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={(1, "personal"): _personal_draft_session()},
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.consent_granted is True
    assert state.ideal_partner.can_start_ideal_partner is False


def test_personal_published_unlocks_ideal_partner() -> None:
    """personal 已发布（published_revision_id 非空）→ ideal_partner 可开启。"""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={
            (1, "personal"): {
                **_personal_draft_session(),
                "journey_stage": "published",
            },
        },
        published_revisions={(1, "personal"): {"revision_id": 7}},
    )
    state = _await(build_state_response(user_id=1, repo=repo))
    assert state.personal.published_revision_id == 7
    assert state.ideal_partner.can_start_ideal_partner is True


def test_legacy_ideal_partner_user_can_resume() -> None:
    """已有 ideal_partner 会话/草稿/历史 revision 的老用户永远可恢复。"""
    from app.services.ai.moxiang_state import build_state_response

    # 即使 personal 没有会话/草稿,ideal_partner 也必须可继续（has_session=True）
    repo = _FakeRepo(
        sessions={(2, "ideal_partner"): _ideal_draft_session()},
        # personal 没有会话也没有发布
    )
    state = _await(build_state_response(user_id=2, repo=repo))
    # 老用户即便 personal 空白,理想伴侣入口必须保留
    assert state.ideal_partner.can_start_ideal_partner is True
    assert state.ideal_partner.session_id == "s-ideal-1"


def test_ideal_partner_without_history_but_personal_published() -> None:
    """personal 已发布、ideal_partner 没历史 → 仍可开启(走新会话流程)。"""
    from app.services.ai.moxiang_state import build_state_response

    repo = _FakeRepo(
        sessions={(3, "personal"): {
            **_personal_draft_session(),
            "journey_stage": "published",
        }},
        published_revisions={(3, "personal"): {"revision_id": 1}},
    )
    state = _await(build_state_response(user_id=3, repo=repo))
    assert state.ideal_partner.can_start_ideal_partner is True
    assert state.ideal_partner.session_id is None  # 没有会话,但可以开启


def test_modifying_personal_does_not_affect_ideal_partner_journey_stage() -> None:
    """修改个人画像不得使愿遇 narrative/会话阶段变化。"""
    from app.services.ai.moxiang_state import build_state_response

    # initial: personal editing, ideal_partner already at chatting with session
    repo_initial = _FakeRepo(
        sessions={
            (4, "personal"): _personal_draft_session(),
            (4, "ideal_partner"): _ideal_draft_session(),
        },
    )
    s_initial = _await(build_state_response(user_id=4, repo=repo_initial))
    ideal_initial_stage = s_initial.ideal_partner.journey_stage

    # mutate personal journey_stage (e.g. user confirmed a field) but keep repo shared
    repo_initial.sessions[(4, "personal")]["journey_stage"] = "ready"

    s_after = _await(build_state_response(user_id=4, repo=repo_initial))
    assert s_after.ideal_partner.journey_stage == ideal_initial_stage


def test_journey_stage_set_remains_frozen() -> None:
    """确保 Phase 2 没有新增 journey stage 字段。"""
    from app.schemas.ai_moxiang import JOURNEY_STAGES

    assert JOURNEY_STAGES == ("chatting", "building", "ready", "published")
