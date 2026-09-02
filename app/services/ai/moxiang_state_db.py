"""Phase 2 P2-01 真实 SQL 仓储 —— 把 ``MoxiangStateRepository`` Protocol 落到 MySQL。

数据面（与 ``app/services/ai/moxiang_state.py`` 协议一一对应）:

- ``ai_profile_session`` —— 活动会话;``journey_stage`` 来自 Phase 1。
- ``ai_profile_candidate`` —— 候选池（仅 Phase 2 借用 effective_turn_count 与
  dimension 覆盖统计;真实 progress 用 confirmed 草稿,见 ``profile.py``）。
- ``ai_profile_build_invite`` —— 单 pending 邀请 + 已触发/已接受计数。
- ``ai_profile_revision`` —— 历史 revision(用于"老用户恢复"判定)。
- ``ai_consent_grant`` —— ``profile_text_extract`` 授权存在性。

只读：本模块不写任何行,不入普通日志;Phase 2 仅承担 state REST 装配所需
查询,事务性更新走 ``app/services/ai/profile.py``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession


# Phase 2 P2-01 —— 个人画像"是否已发布"的判定阈值（只数 revision 行,不限时间)。
# 直接复用 ``ai_profile_revision`` 任意一行即可:发布 = revision 落库。
_PERSONAL_REVISION_COLUMN = "user_id, subject, revision_no"


class MoxiangStateSqlRepository:
    """生产仓储(Phase 2)。单元测试用 ``_FakeRepo`` 替代。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def has_active_consent(
        self, user_id: int, scope: str, version: str
    ) -> bool:
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_consent_grant "
                "WHERE user_id = :user_id AND scope = :scope AND version = :version "
                "AND revoked_at IS NULL LIMIT 1"
            ),
            {"user_id": user_id, "scope": scope, "version": version},
        )
        return result.first() is not None

    async def find_active_session(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT session_id, user_id, subject, input_mode, session_kind, status, "
                "active_status, consent_version, policy_revision, current_question_id, "
                "skipped_field_keys, profile_revision, preference_revision, expires_at, "
                "ended_at, journey_stage, created_at, updated_at "
                "FROM ai_profile_session "
                "WHERE user_id = :user_id AND subject = :subject AND active_status = 1 "
                "LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return None
        # ``_first_row``-style normalization: 接受 RowMapping 与 tuple 两种形态。
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def find_published_revision(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT id AS revision_id, user_id, subject, revision_no, "
                "draft_id, published_at "
                "FROM ai_profile_revision "
                "WHERE user_id = :user_id AND subject = :subject "
                "ORDER BY revision_no DESC LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def find_pending_build_invite(
        self, user_id: int, session_id: str | None
    ) -> dict[str, Any] | None:
        if not session_id:
            return None
        result = await self._db.execute(
            sql_text(
                "SELECT invite_id, session_id, user_id, subject, status, "
                "trigger_kind, invite_no, summary_json "
                "FROM ai_profile_build_invite "
                "WHERE user_id = :user_id AND session_id = :session_id "
                "AND status = 'pending' LIMIT 1"
            ),
            {"user_id": user_id, "session_id": session_id},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def find_pending_confirm_card(self, session_id: str | None) -> bool:
        """检查会话是否存在 draft 中带 suggested 状态的字段(等待用户确认)。"""
        if not session_id:
            return False
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_profile_draft d "
                "JOIN ai_profile_draft_field f ON f.draft_id = d.draft_id "
                "WHERE d.session_id = :session_id "
                "AND f.confirmation_status = 'suggested' LIMIT 1"
            ),
            {"session_id": session_id},
        )
        return result.first() is not None

    async def count_dimension_confirmed(
        self, user_id: int, subject: str
    ) -> dict[str, int]:
        """每个维度已 confirmed 字段数(主进度唯一来源,Contract §7)。"""
        result = await self._db.execute(
            sql_text(
                "SELECT f.profile_dimension, COUNT(*) AS n "
                "FROM ai_profile_revision r "
                "JOIN ai_profile_revision_field f ON f.revision_id = r.id "
                "WHERE r.user_id = :user_id AND r.subject = :subject "
                "AND f.profile_dimension IS NOT NULL "
                "GROUP BY f.profile_dimension"
            ),
            {"user_id": user_id, "subject": subject},
        )
        rows = result.fetchall()
        out: dict[str, int] = {}
        for row in rows:
            try:
                m = row._mapping
            except AttributeError:
                m = row
            dim = str(m["profile_dimension"])
            out[dim] = int(m["n"])
        return out

    async def average_confidence(
        self, user_id: int, subject: str
    ) -> float:
        result = await self._db.execute(
            sql_text(
                "SELECT AVG(f.confidence) AS avg_conf "
                "FROM ai_profile_revision r "
                "JOIN ai_profile_revision_field f ON f.revision_id = r.id "
                "WHERE r.user_id = :user_id AND r.subject = :subject"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return 0.0
        try:
            m = row._mapping
        except AttributeError:
            m = row
        val = m["avg_conf"]
        return float(val or 0.0)

    async def confirmation_percent(
        self, user_id: int, subject: str
    ) -> float:
        """confirmed / (confirmed + suggested) over current active draft, *100。

        无活动草稿或无字段 → 返回 0(Contract §7)。
        """
        result = await self._db.execute(
            sql_text(
                "SELECT "
                "SUM(CASE WHEN f.confirmation_status = 'confirmed' THEN 1 ELSE 0 END) AS c, "
                "SUM(CASE WHEN f.confirmation_status IN ('confirmed','suggested') THEN 1 ELSE 0 END) AS t "
                "FROM ai_profile_draft d "
                "JOIN ai_profile_draft_field f ON f.draft_id = d.draft_id "
                "WHERE d.user_id = :user_id AND d.subject = :subject "
                "AND d.status NOT IN ('deleted', 'cancelled', 'stale')"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return 0.0
        try:
            m = row._mapping
        except AttributeError:
            m = row
        c = int(m["c"] or 0)
        t = int(m["t"] or 0)
        if t <= 0:
            return 0.0
        return c / t * 100.0

    async def list_session_turns(
        self,
        session_id: str,
        before_turn_no: int | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        params: dict[str, Any] = {"session_id": session_id, "limit": limit}
        where = "session_id = :session_id"
        if before_turn_no is not None:
            where += " AND turn_no < :before_turn_no"
            params["before_turn_no"] = int(before_turn_no)
        result = await self._db.execute(
            sql_text(
                "SELECT turn_id, session_id, client_turn_id, user_id, turn_no, role, "
                "answer_text, created_at "
                f"FROM ai_profile_turn WHERE {where} ORDER BY turn_no ASC LIMIT :limit"
            ),
            params,
        )
        rows = result.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                m = row._mapping
            except AttributeError:
                m = row
            out.append(
                {
                    "turn_id": str(m["turn_id"]),
                    "session_id": str(m["session_id"]),
                    "client_turn_id": str(m["client_turn_id"]),
                    "user_id": int(m["user_id"]),
                    "turn_no": int(m["turn_no"]),
                    "role": str(m["role"]),
                    "answer_text": str(m["answer_text"]),
                    "created_at": m["created_at"],
                }
            )
        return tuple(out)

    async def has_subject_history(self, user_id: int, subject: str) -> bool:
        """Phase 2 P2-01 老用户恢复判定:会话、草稿、revision 任一存在即 True。"""
        # 任意主体上是否有过会话(即使 active_status=0,即已关闭/取消)
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_profile_session "
                "WHERE user_id = :user_id AND subject = :subject LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        if result.first() is not None:
            return True
        # 是否有过 revision(发布过 → 也算历史)
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_profile_revision "
                "WHERE user_id = :user_id AND subject = :subject LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        if result.first() is not None:
            return True
        # 是否有 draft 残留(soft-deleted 也算,允许恢复查看)
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_profile_draft "
                "WHERE user_id = :user_id AND subject = :subject LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        return result.first() is not None