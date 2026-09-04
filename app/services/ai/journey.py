"""Durable natural-conversation candidate pipeline for the Moxiang journey.

The module deliberately reuses profile sessions, consent/revision checks, turns
and the shared task runtime.  It does *not* create a draft: active candidates
remain private understanding evidence until a build invitation is accepted.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.ai_schema import PROFILE_DIMENSIONS
from app.schemas.ai_moxiang import CandidateRecord, HIGH_CONFIDENCE_THRESHOLD
from app.services.ai.base import AITaskContext, StructuredExtractRequest
from app.services.ai.build_invite import (
    build_invite_summary,
    should_offer_invite,
)
from app.services.ai.candidates import candidates_from_master_result
from app.services.ai.gateway import AIGateway
from app.services.ai.journey_progress import (
    JourneyDimensionProgress,
    calculate_journey_progress,
)
from app.services.ai.prompts.moxiang_master import build_build_context
from app.services.ai.profile import (
    AIConsentRequired,
    AIInputError,
    PROFILE_POLICY_REVISION,
    PROFILE_PROMPT_VERSION,
    PROFILE_SCHEMA_VERSION,
    ProfileSessionNotFound,
    ProfileSessionStale,
    ProfileTurn,
    _insert_turn,
    find_turn_by_client_id,
    hash_request,
    load_owned_active_session,
    moderate_text,
    normalize_profile_answer,
)
from app.services.ai.tasks import AiTaskRecord, enqueue_task, fail_task
from app.services.revisions import RevisionVector


logger = logging.getLogger(__name__)

TASK_TYPE = "moxiang_candidate_extract"
_SOURCE_TYPE = "moxiang_journey"

# 批次3 #12：单维度活跃 entry 候选上限。E2E 实测单维度可无限堆积
# （用户2 relationship_boundaries 4 条且语义偏 lifestyle），进度与草稿
# 质量都被稀释。超出时保留置信度最高的 N 条，其余转 dismissed；
# structured 字段不受此限（白名单字段本身有界，且是发布底线依赖）。
MAX_ACTIVE_ENTRIES_PER_DIMENSION = 3


@dataclass(frozen=True)
class JourneyTurnSubmission:
    """Persisted final turn and its candidate task, with replay semantics."""

    turn: ProfileTurn
    task_id: str | None
    replayed: bool


@dataclass(frozen=True)
class JourneyInvite:
    """Safe invitation projection for the WebSocket, never raw candidate rows."""

    invite_id: str
    session_id: str
    subject: str
    status: str
    summary_items: tuple[dict[str, str], ...]
    effective_turn_count: int
    dimension_count: int
    candidate_count: int


def _safe_payload(
    *, session_id: str, turn_id: str, client_turn_id: str, subject: str
) -> str:
    """Return task metadata without user text or provider payloads."""
    return json.dumps(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "client_turn_id": client_turn_id,
            "subject": subject,
        },
        ensure_ascii=False,
    )


def journey_task_key(session_id: str, client_turn_id: str) -> str:
    """Stable task key for one client turn, independent of its answer text."""
    return f"moxiang-{hash_request(session_id, client_turn_id, '')}"


async def submit_journey_turn(
    db: AsyncSession,
    *,
    session_id: str,
    owner_user_id: int,
    client_turn_id: str,
    answer_text: str,
) -> JourneyTurnSubmission:
    """Persist one final turn and enqueue the dedicated candidate task.

    The sequence mirrors ``submit_profile_turn`` but intentionally avoids the
    legacy extracting/draft state transition.  A replay returns the original
    task through the task runtime's idempotency key and never inserts another
    turn or candidate task.
    """
    normalized = normalize_profile_answer(answer_text)
    session = await load_owned_active_session(db, session_id, owner_user_id)
    existing = await find_turn_by_client_id(db, session_id, client_turn_id)
    digest = hash_request(session_id, client_turn_id, normalized)
    task_key = journey_task_key(session_id, client_turn_id)
    if existing is not None:
        task = await enqueue_task(
            db=db,
            owner_user_id=owner_user_id,
            task_type=TASK_TYPE,
            idempotency_key=task_key,
            request_hash=digest,
            revisions=session.revision_vector,
            consent=session.consent_snapshot,
        )
        return JourneyTurnSubmission(existing, task.task_id, True)

    moderation = await moderate_text(db, normalized, field="墨相师对话")
    if moderation.action == "reject":
        raise AIInputError("回答内容包含违规信息,请修改后重试")
    if moderation.action == "replace" and moderation.display_content:
        normalized = moderation.display_content
        digest = hash_request(session_id, client_turn_id, normalized)

    try:
        turn = await _insert_turn(
            db,
            session_id,
            owner_user_id,
            client_turn_id,
            normalized,
            source_type=_SOURCE_TYPE,
        )
    except IntegrityError:
        await db.rollback()
        existing = await find_turn_by_client_id(db, session_id, client_turn_id)
        if existing is None:
            raise
        task = await enqueue_task(
            db=db,
            owner_user_id=owner_user_id,
            task_type=TASK_TYPE,
            idempotency_key=task_key,
            request_hash=digest,
            revisions=session.revision_vector,
            consent=session.consent_snapshot,
        )
        return JourneyTurnSubmission(existing, task.task_id, True)

    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=TASK_TYPE,
        idempotency_key=task_key,
        request_hash=digest,
        revisions=session.revision_vector,
        consent=session.consent_snapshot,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": _safe_payload(
                session_id=session_id,
                turn_id=turn.turn_id,
                client_turn_id=client_turn_id,
                subject=session.subject.value,
            ),
            "task_id": task.task_id,
        },
    )
    await db.flush()
    return JourneyTurnSubmission(turn, task.task_id, False)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _candidate_from_row(row: Any) -> CandidateRecord:
    source_turn_ids = _json_value(row.get("source_turn_ids")) or []
    return CandidateRecord(
        candidate_id=str(row["candidate_id"]),
        session_id=str(row["session_id"]),
        user_id=int(row["user_id"]),
        subject=str(row["subject"]),
        profile_dimension=str(row["profile_dimension"]),
        field_kind=str(row["field_kind"]),
        field_key=row.get("field_key"),
        category=row.get("category"),
        content=row.get("content"),
        value=_json_value(row.get("value_json")),
        confidence=float(row.get("confidence") or 0.0),
        source_turn_ids=tuple(str(value) for value in source_turn_ids),
        source_span=row.get("source_span"),
        consent_version=str(row["consent_version"]),
        policy_revision=str(row["policy_revision"]),
        status=str(row.get("status") or "active"),
        content_hash=str(row["content_hash"]),
    )


async def list_session_candidates(
    db: AsyncSession, *, session_id: str, active_only: bool = True
) -> tuple[CandidateRecord, ...]:
    """Read candidates for progress projection without exposing raw turn text."""
    where = "session_id = :session_id"
    if active_only:
        where += " AND status = 'active'"
    result = await db.execute(
        text(
            "SELECT candidate_id, session_id, user_id, subject, profile_dimension, "
            "field_kind, field_key, category, content, value_json, confidence, "
            "source_turn_ids, source_span, consent_version, policy_revision, "
            "status, content_hash FROM ai_profile_candidate WHERE "
            + where
        ),
        {"session_id": session_id},
    )
    return tuple(_candidate_from_row(dict(row._mapping)) for row in result.fetchall())


async def _upsert_candidate(
    db: AsyncSession, candidate: CandidateRecord, *, source_turn_id: str
) -> None:
    """Merge evidence by content hash without reviving terminal candidates."""
    await db.execute(
        text(
            "INSERT INTO ai_profile_candidate "
            "(candidate_id, session_id, user_id, subject, profile_dimension, "
            "field_kind, field_key, category, content, value_json, confidence, "
            "source_turn_ids, source_span, consent_version, policy_revision, "
            "status, content_hash, created_at, updated_at) "
            "VALUES (:candidate_id, :session_id, :user_id, :subject, "
            ":profile_dimension, :field_kind, :field_key, :category, :content, "
            ":value_json, :confidence, JSON_ARRAY(:source_turn_id), :source_span, "
            ":consent_version, :policy_revision, 'active', :content_hash, "
            "UTC_TIMESTAMP(), UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            "confidence = GREATEST(confidence, VALUES(confidence)), "
            "source_span = COALESCE(source_span, VALUES(source_span)), "
            "source_turn_ids = CASE "
            "WHEN JSON_CONTAINS(source_turn_ids, JSON_QUOTE(:source_turn_id)) "
            "THEN source_turn_ids "
            "ELSE JSON_ARRAY_APPEND(source_turn_ids, '$', :source_turn_id) END, "
            "updated_at = UTC_TIMESTAMP()"
        ),
        {
            "candidate_id": candidate.candidate_id,
            "session_id": candidate.session_id,
            "user_id": candidate.user_id,
            "subject": candidate.subject,
            "profile_dimension": candidate.profile_dimension,
            "field_kind": candidate.field_kind,
            "field_key": candidate.field_key,
            "category": candidate.category,
            "content": candidate.content,
            "value_json": json.dumps(candidate.value, ensure_ascii=False),
            "confidence": candidate.confidence,
            "source_turn_id": source_turn_id,
            "source_span": candidate.source_span,
            "consent_version": candidate.consent_version,
            "policy_revision": candidate.policy_revision,
            "content_hash": candidate.content_hash,
        },
    )


async def _enforce_entry_dimension_cap(db: AsyncSession, session_id: str) -> int:
    """Dismiss active entry candidates beyond the per-dimension cap (#12).

    Keeps the ``MAX_ACTIVE_ENTRIES_PER_DIMENSION`` highest-confidence rows per
    dimension (ties broken by recency, then id) and moves the rest to the
    terminal ``dismissed`` status.  Returns the number of dismissed rows.
    """
    result = await db.execute(
        text(
            "UPDATE ai_profile_candidate c "
            "JOIN ("
            "  SELECT candidate_id FROM ("
            "    SELECT candidate_id, "
            "           ROW_NUMBER() OVER ("
            "             PARTITION BY profile_dimension "
            "             ORDER BY confidence DESC, updated_at DESC, id DESC"
            "           ) AS rn "
            "    FROM ai_profile_candidate "
            "    WHERE session_id = :session_id AND status = 'active' "
            "      AND field_kind = 'entry'"
            "  ) ranked WHERE ranked.rn > :cap"
            ") excess ON c.candidate_id = excess.candidate_id "
            "SET c.status = 'dismissed', c.updated_at = UTC_TIMESTAMP()"
        ),
        {"session_id": session_id, "cap": MAX_ACTIVE_ENTRIES_PER_DIMENSION},
    )
    dismissed = int(result.rowcount or 0)
    if dismissed:
        logger.info(
            "moxiang_entry_cap_dismissed session_id=%s count=%d cap=%d",
            session_id,
            dismissed,
            MAX_ACTIVE_ENTRIES_PER_DIMENSION,
        )
    return dismissed


_DIMENSION_LABELS = {
    "personality_social": "性格与社交",
    "intimacy_pattern": "亲密模式",
    "lifestyle": "生活方式",
    "emotional_expression": "情绪表达",
    "relationship_boundaries": "关系边界",
    "future_expectations": "未来期待",
}


def _dimension_status(progress: JourneyDimensionProgress) -> str:
    """把单维进度渲染成知遇可读的状态词（空白/部分理解/已理解）。"""
    if progress.percent >= 100.0:
        return "已理解"
    if progress.evidence_count >= 1:
        return f"部分理解（{progress.evidence_count}条）"
    return "空白"


def _existing_candidates_digest(candidates: tuple[CandidateRecord, ...]) -> str:
    """Render active candidates into a compact dedup digest for the extractor.

    Structured fields show ``field_key = value``; entries show ``category：content``.
    Content is truncated and the list capped so the prompt stays bounded.
    """
    lines: list[str] = []
    for candidate in candidates[:40]:
        label = _DIMENSION_LABELS.get(candidate.profile_dimension, candidate.profile_dimension)
        if candidate.field_kind == "structured":
            detail = f"{candidate.field_key} = {candidate.value}"
        else:
            content = (candidate.content or "").strip().replace("\n", " ")
            if len(content) > 40:
                content = content[:40] + "…"
            detail = f"{candidate.category}：{content}"
        lines.append(f"- [{label}] {detail}")
    return "\n".join(lines)


def _structured_display_value(value: Any) -> str:
    """Render a structured field value for user-facing display.

    Lists/tuples (tags) join with 、 instead of leaking a Python repr like
    ``['旅行', '看展']``; scalars stringify. Enum codes stay as-is so the
    frontend dictionary (#18) can translate them.
    """
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value if str(item).strip())
    return "" if value is None else str(value)


# 建构模式需要问齐的个人硬字段（与 moxiang_master._MISSING_FIELD_LABELS 对齐）。
_HARD_PROFILE_FIELDS = (
    "age",
    "city_code",
    "marriage_status",
    "education_level",
    "height_cm",
    "income_band",
    "occupation_group",
)


async def compose_journey_build_context(
    db: AsyncSession, *, session_id: str, subject: str
) -> str:
    """Compose 知遇's build-mode context: progress + missing hard fields + confirmed summary.

    Reads the session's active/promoted candidates and projects them into the
    independent system segment the master prompt injects, so the conversation
    steers toward what is still unknown instead of re-asking settled facts.
    """
    candidates = await list_session_candidates(
        db, session_id=session_id, active_only=False
    )
    usable = tuple(
        candidate
        for candidate in candidates
        if candidate.status in {"active", "promoted"}
    )
    progress = calculate_journey_progress(usable)
    percent = progress.overall_percent
    dimension_lines = [
        f"- {_DIMENSION_LABELS.get(dim, dim)}：{_dimension_status(progress.dimensions[dim])}"
        for dim in PROFILE_DIMENSIONS
    ]
    missing_hard: list[str] = []
    if subject == "personal":
        present = {
            candidate.field_key
            for candidate in usable
            if candidate.field_kind == "structured"
        }
        missing_hard = [key for key in _HARD_PROFILE_FIELDS if key not in present]
    confirmed_summary = _existing_candidates_digest(usable)
    return build_build_context(
        missing_hard,
        confirmed_summary,
        percent,
        subject=subject,
        dimension_lines=dimension_lines,
    )


async def extract_journey_candidates(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """Worker handler: turn -> active candidate pool, never draft fields."""
    payload = task.payload_summary or {}
    session_id = str(payload.get("session_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    client_turn_id = str(payload.get("client_turn_id") or "")
    if not session_id or not turn_id or not client_turn_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    try:
        session = await load_owned_active_session(db, session_id, task.owner_user_id)
        turn = await find_turn_by_client_id(db, session_id, client_turn_id)
    except (AIConsentRequired, ProfileSessionNotFound, ProfileSessionStale):
        # 只有授权/会话终态才是不可重试的语义。瞬时 DB 故障放行给 worker
        # 边界，由它转成可重试的 AI_TEMPORARILY_UNAVAILABLE——否则网络抖一下
        # 就会把用户这句话的候选抽取永久判死刑（turn 已落库但不再重抽）。
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_CONSENT_REQUIRED", retryable=False,
        )
        return None
    if turn is None or turn.turn_id != turn_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    context = AITaskContext(
        task_id=task.task_id,
        request_id=uuid.uuid4().hex,
        scene="moxiang_candidate_extract",
        provider=settings.ai_provider_name,
        model=settings.ai_model_name,
        prompt_version=PROFILE_PROMPT_VERSION,
        schema_version=PROFILE_SCHEMA_VERSION,
        input_revision=task.source_revision_json or {},
        policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
    )
    active_candidates = await list_session_candidates(
        db, session_id=session_id, active_only=True
    )
    request = StructuredExtractRequest(
        subject=session.subject.value,
        turn_texts=(turn.answer_text,),
        consent_version=session.consent_version,
        policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
        session_kind="master",
        existing_digest=_existing_candidates_digest(active_candidates) or None,
    )
    outcome = await AIGateway(
        timeout_seconds=settings.ai_gateway_timeout_seconds
    ).structured_extract(context, request)
    if outcome.result is None:
        await fail_task(
            db,
            task.task_id,
            worker_id,
            error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            retryable=outcome.retryable,
        )
        return None
    try:
        if outcome.result.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError("provider result schema version does not match")
        extracted = candidates_from_master_result(
            subject=session.subject.value,
            result=outcome.result,
            consent_version=session.consent_version,
            policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
            source_turn_id=turn.turn_id,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        # 单条候选违规会让整轮抽取失败，但根因是供应商输出漂移（dots 抖动），
        # 重试很可能成功：留痕后放行给 worker 边界转成可重试失败，不再把用户
        # 这一轮的有效证据永久丢弃（旧行为 retryable=False 等于整轮陪葬）。
        logger.warning(
            "moxiang_candidate_result_rejected task_id=%s prompt_version=%s error=%s",
            task.task_id,
            PROFILE_PROMPT_VERSION,
            type(exc).__name__,
        )
        raise
    for item in extracted:
        candidate = replace(
            item,
            candidate_id=uuid.uuid4().hex,
            session_id=session.session_id,
            user_id=task.owner_user_id,
        )
        await _upsert_candidate(db, candidate, source_turn_id=turn.turn_id)
    # #12：本轮 upsert 后统一裁剪，保证任一维度的活跃 entry 证据不超上限。
    await _enforce_entry_dimension_cap(db, session.session_id)
    return f"moxiang-candidate:{task.task_id}", session.revision_vector


def _mapping(row: Any) -> dict[str, Any]:
    try:
        return dict(row._mapping)
    except AttributeError:
        return dict(row)


def _invite_from_row(row: Any) -> JourneyInvite:
    data = _mapping(row)
    raw_summary = _json_value(data.get("summary_json")) or []
    summary_items = tuple(
        {
            "profile_dimension": str(item.get("profile_dimension") or ""),
            "content": str(item.get("content") or ""),
        }
        for item in raw_summary
        if isinstance(item, dict)
    )
    return JourneyInvite(
        invite_id=str(data["invite_id"]),
        session_id=str(data["session_id"]),
        subject=str(data["subject"]),
        status=str(data["status"]),
        summary_items=summary_items,
        effective_turn_count=int(data.get("effective_turn_count_at_create") or 0),
        dimension_count=int(data.get("dimension_count") or 0),
        candidate_count=int(data.get("candidate_count") or 0),
    )


async def maybe_create_build_invite(
    db: AsyncSession, *, session_id: str, user_id: int, subject: str
) -> JourneyInvite | None:
    """Create one pending invitation once the shared threshold is reached."""
    existing = await db.execute(
        text(
            "SELECT invite_id, session_id, subject, status, summary_json, "
            "effective_turn_count_at_create, dimension_count, candidate_count "
            "FROM ai_profile_build_invite WHERE session_id = :session_id "
            "AND user_id = :user_id AND status = 'pending' LIMIT 1 FOR UPDATE"
        ),
        {"session_id": session_id, "user_id": user_id},
    )
    row = existing.first()
    if row is not None:
        return _invite_from_row(row)

    candidates = await list_session_candidates(db, session_id=session_id)
    eligible = tuple(
        candidate
        for candidate in candidates
        if candidate.confidence >= HIGH_CONFIDENCE_THRESHOLD
    )
    # 维度门槛按会话全量高置信候选计算：首邀接受时旧理解全部转 promoted，
    # 若维度仍只数 active 候选，第二张邀请的维度门槛会被"清零"，在 auto
    # 上限内几乎不可再触发，用户被卡死在无法整理的状态。新证据门槛
    # （high_confidence_candidate_count）仍只数未晋升的 active 候选。
    all_candidates = await list_session_candidates(
        db, session_id=session_id, active_only=False
    )
    understood_dimensions = {
        candidate.profile_dimension
        for candidate in all_candidates
        if candidate.confidence >= HIGH_CONFIDENCE_THRESHOLD
    }
    turn_row = await db.execute(
        text(
            "SELECT COUNT(*) AS n FROM ai_profile_turn WHERE session_id = :session_id "
            "AND role = 'user' AND status = 'saved'"
        ),
        {"session_id": session_id},
    )
    effective_turn_count = int(turn_row.scalar() or 0)
    dimension_count = len(understood_dimensions)
    auto_row = await db.execute(
        text(
            "SELECT COUNT(*) AS n FROM ai_profile_build_invite "
            "WHERE session_id = :session_id AND trigger_kind = 'auto'"
        ),
        {"session_id": session_id},
    )
    auto_invite_count = int(auto_row.scalar() or 0)
    if not should_offer_invite(
        effective_turn_count=effective_turn_count,
        dimension_count=dimension_count,
        high_confidence_candidate_count=len(eligible),
        auto_invite_count=auto_invite_count,
    ):
        return None

    invite_id = f"invite-{uuid.uuid4().hex}"
    summary_items = tuple(item.model_dump() for item in build_invite_summary(eligible))
    invite_no = auto_invite_count + 1
    try:
        await db.execute(
            text(
                "INSERT INTO ai_profile_build_invite "
                "(invite_id, session_id, user_id, subject, status, trigger_kind, "
                "invite_no, summary_json, effective_turn_count_at_create, "
                "dimension_count, candidate_count, created_at, updated_at) "
                "VALUES (:invite_id, :session_id, :user_id, :subject, 'pending', 'auto', "
                ":invite_no, :summary_json, :effective_turn_count, :dimension_count, "
                ":candidate_count, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "invite_id": invite_id,
                "session_id": session_id,
                "user_id": user_id,
                "subject": subject,
                "invite_no": invite_no,
                "summary_json": json.dumps(summary_items, ensure_ascii=False),
                "effective_turn_count": effective_turn_count,
                "dimension_count": dimension_count,
                "candidate_count": len(eligible),
            },
        )
    except IntegrityError:
        # The single-pending unique key is the concurrency authority.  Re-read
        # the winner rather than issuing a duplicate invitation.
        row = (
            await db.execute(
                text(
                    "SELECT invite_id, session_id, subject, status, summary_json, "
                    "effective_turn_count_at_create, dimension_count, candidate_count "
                    "FROM ai_profile_build_invite WHERE session_id = :session_id "
                    "AND status = 'pending' LIMIT 1"
                ),
                {"session_id": session_id},
            )
        ).first()
        return _invite_from_row(row) if row is not None else None
    await db.execute(
        text(
            "UPDATE ai_profile_session SET journey_stage = 'building', "
            "updated_at = UTC_TIMESTAMP() WHERE session_id = :session_id"
        ),
        {"session_id": session_id},
    )
    return JourneyInvite(
        invite_id=invite_id,
        session_id=session_id,
        subject=subject,
        status="pending",
        summary_items=summary_items,
        effective_turn_count=effective_turn_count,
        dimension_count=dimension_count,
        candidate_count=len(eligible),
    )


async def resolve_journey_invite(
    db: AsyncSession,
    *,
    invite_id: str,
    user_id: int,
    resolution: str,
) -> tuple[JourneyInvite, str | None]:
    """Resolve a pending invite and promote candidates only on acceptance."""
    if resolution not in {"accepted", "snoozed"}:
        raise AIInputError("邀请操作非法")
    row = (
        await db.execute(
            text(
                "SELECT invite_id, session_id, user_id, subject, status, summary_json, "
                "effective_turn_count_at_create, dimension_count, candidate_count "
                "FROM ai_profile_build_invite WHERE invite_id = :invite_id FOR UPDATE"
            ),
            {"invite_id": invite_id},
        )
    ).first()
    if row is None or int(_mapping(row).get("user_id") or 0) != user_id:
        raise AIInputError("邀请不存在或无权操作")
    invite = _invite_from_row(row)
    if invite.status != "pending":
        raise AIInputError("该邀请已处理")
    if resolution == "snoozed":
        await db.execute(
            text(
                "UPDATE ai_profile_build_invite SET status = 'snoozed', "
                "snoozed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
                "WHERE invite_id = :invite_id AND status = 'pending'"
            ),
            {"invite_id": invite_id},
        )
        await db.execute(
            text(
                "UPDATE ai_profile_session SET journey_stage = 'chatting', "
                "updated_at = UTC_TIMESTAMP() WHERE session_id = :session_id"
            ),
            {"session_id": invite.session_id},
        )
        return JourneyInvite(**{**invite.__dict__, "status": "snoozed"}), None

    session = await load_owned_active_session(db, invite.session_id, user_id)
    candidates = tuple(
        candidate
        for candidate in await list_session_candidates(db, session_id=invite.session_id)
        if candidate.confidence >= HIGH_CONFIDENCE_THRESHOLD
    )
    draft_row = (
        await db.execute(
            text(
                "SELECT draft_id FROM ai_profile_draft WHERE session_id = :session_id "
                "AND status = 'draft' ORDER BY updated_at DESC LIMIT 1 FOR UPDATE"
            ),
            {"session_id": invite.session_id},
        )
    ).first()
    draft_id = str(_mapping(draft_row)["draft_id"]) if draft_row is not None else uuid.uuid4().hex
    if draft_row is None:
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft "
                "(draft_id, user_id, subject, session_id, status, expected_revision, "
                "consent_snapshot_json, policy_revision, prompt_version, schema_version, "
                "created_at, updated_at) VALUES "
                "(:draft_id, :user_id, :subject, :session_id, 'draft', 0, "
                ":consent_snapshot_json, :policy_revision, 'profile-extract-prompt-v1', "
                "'profile-extract-v1', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "draft_id": draft_id,
                "user_id": user_id,
                "subject": session.subject.value,
                "session_id": invite.session_id,
                "consent_snapshot_json": json.dumps(session.consent_snapshot or {}, ensure_ascii=False),
                "policy_revision": session.policy_revision or "ai-policy-2026-08-07-v1",
            },
        )
    for candidate in candidates:
        field_key = candidate.field_key or f"entry_{candidate.profile_dimension}_{candidate.content_hash[:12]}"
        field_kind = candidate.field_kind if candidate.field_kind in {"structured", "entry"} else "entry"
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, field_kind, profile_dimension, category, content, "
                "value_json, display_value, source_type, source_turn_ids, source_span, confidence, "
                "visibility, consent_scope, schema_version, prompt_version, content_hash, "
                "confirmation_status, created_at, updated_at) VALUES "
                "(:draft_id, :field_key, :subject, :field_kind, :profile_dimension, :category, :content, "
                ":value_json, :display_value, 'moxiang_journey', :source_turn_ids, :source_span, "
                ":confidence, 'self', 'profile_text_extract', 'profile-extract-v1', "
                "'profile-extract-prompt-v1', :content_hash, 'suggested', UTC_TIMESTAMP(), UTC_TIMESTAMP()) "
                "ON DUPLICATE KEY UPDATE "
                "profile_dimension = IF(confirmation_status = 'suggested', VALUES(profile_dimension), profile_dimension), "
                "value_json = IF(confirmation_status = 'suggested', VALUES(value_json), value_json), "
                "content = IF(confirmation_status = 'suggested', VALUES(content), content), "
                "display_value = IF(confirmation_status = 'suggested', VALUES(display_value), display_value), "
                "confidence = GREATEST(confidence, VALUES(confidence)), updated_at = UTC_TIMESTAMP()"
            ),
            {
                "draft_id": draft_id,
                "field_key": field_key,
                "subject": session.subject.value,
                "field_kind": field_kind,
                "profile_dimension": candidate.profile_dimension,
                "category": candidate.category,
                "content": candidate.content,
                "value_json": json.dumps(candidate.value, ensure_ascii=False),
                "display_value": candidate.content or _structured_display_value(candidate.value),
                "source_turn_ids": json.dumps(candidate.source_turn_ids, ensure_ascii=False),
                "source_span": candidate.source_span,
                "confidence": candidate.confidence,
                "content_hash": candidate.content_hash,
            },
        )
    await db.execute(
        text(
            "UPDATE ai_profile_candidate SET status = 'promoted', updated_at = UTC_TIMESTAMP() "
            "WHERE session_id = :session_id AND status = 'active' "
            "AND confidence >= :threshold"
        ),
        {"session_id": invite.session_id, "threshold": HIGH_CONFIDENCE_THRESHOLD},
    )
    await db.execute(
        text(
            "UPDATE ai_profile_build_invite SET status = 'accepted', accepted_at = UTC_TIMESTAMP(), "
            "updated_at = UTC_TIMESTAMP() WHERE invite_id = :invite_id AND status = 'pending'"
        ),
        {"invite_id": invite_id},
    )
    await db.execute(
        text(
            "UPDATE ai_profile_session SET journey_stage = 'building', updated_at = UTC_TIMESTAMP() "
            "WHERE session_id = :session_id"
        ),
        {"session_id": invite.session_id},
    )
    return JourneyInvite(**{**invite.__dict__, "status": "accepted"}), draft_id
