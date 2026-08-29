"""Versioned minimal feature projections (Task 9, 统一方案 §5.5/§10.3/§19.5).

A feature projection is the only derived representation of confirmed AI profile
fields that downstream search (Task 10) and compatibility (Task 11) consumers
may read.  It is rebuilt from ``ai_profile_revision`` / ``ai_profile_revision_field``
— both are confirmed-only by construction (Task 8 ``insert_immutable_profile_revision``
never writes unconfirmed fields) — and its validity is pinned to the five
dimension revision vector: a projection is current only when the stored vector
equals the user's current vector, exactly like the spec's ``valid(result)``
condition.  The legacy ``user_feature_vector.interest_vector`` JSON is never used
as a substitute for this versioned projection (§19.5).

``projection_kind`` is frozen to three values (spec §10.3):
``personal_searchable`` and ``personal_compatibility`` are built from the user's
``personal`` confirmed facts; ``ideal_partner_preference`` is built from the
user's ``ideal_partner`` confirmed facts and is marked ``self_only`` so it can
only be read by the owner's own preference computation — it is never returned
as a candidate profile.

None of the functions in this module call ``commit()`` — the caller's transaction
owns durability, mirroring the rest of the AI core.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_common import ProjectionKind, ProjectionVisibility
from app.schemas.ai_profile import PROFILE_ENTRY_CATEGORY_LABELS
from app.services.revisions import RevisionVector

# 投影 schema 版本：与 M04 抽取 Schema 同源冻结（Task 1/5），不新增版本。
PROJECTION_SCHEMA_VERSION = "profile-extract-v1"
# 投影授权 scope：个人画像确认字段来自 M04 文字抽取会话。
PROFILE_CONSENT_SCOPE = "profile_text_extract"
# 投影 TTL：与失效后的 purge_after 窗口一致，超过即视为过期需重建。
_PROJECTION_TTL_DAYS = 30

# 字段可见性白名单（Task 1 冻结；与 ai_common.AI_FIELD_ALLOWLIST 同源）。
PROFILE_ALLOWLIST = frozenset(
    {
        "age",
        "city_code",
        "marriage_status",
        "education_level",
        "height_cm",
        "income_band",
        "occupation_group",
        "interest_tags",
        "lifestyle_tags",
        "relationship_goal",
    }
)

# projection_kind -> 数据主体（决定读取哪个 subject 的已确认版本字段）。
_PROJECTION_SUBJECT: dict[ProjectionKind, str] = {
    ProjectionKind.PERSONAL_SEARCHABLE: "personal",
    ProjectionKind.PERSONAL_COMPATIBILITY: "personal",
    ProjectionKind.IDEAL_PARTNER_PREFERENCE: "ideal_partner",
}

# projection_kind -> 可见性类。ideal_partner_preference 仅本人偏好计算可读。
_PROJECTION_VISIBILITY: dict[ProjectionKind, ProjectionVisibility] = {
    ProjectionKind.PERSONAL_SEARCHABLE: ProjectionVisibility.SEARCHABLE,
    ProjectionKind.PERSONAL_COMPATIBILITY: ProjectionVisibility.SEARCHABLE,
    ProjectionKind.IDEAL_PARTNER_PREFERENCE: ProjectionVisibility.SELF_ONLY,
}


class ProjectionBuildError(Exception):
    """投影构建失败：缺少已确认字段或授权，绝不生成空白「成功」投影。

    调用方（profile_projection 任务的 worker handler）应据此把任务标为 failed
    /stale，而不是落一条空投影。错误信息只含受控原因码，不携带字段原文。
    """

    code = "AI_FEATURE_PROJECTION_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class FeatureProjection:
    """The minimal, versioned, consent-gated projection of confirmed fields."""

    id: int | None
    subject_user_id: int
    projection_kind: ProjectionKind
    source_hash: str
    projection_version: str
    fields: dict[str, Any]
    source_revision: RevisionVector
    consent_snapshot: dict[str, Any]
    visibility_class: ProjectionVisibility
    status: str
    expires_at: datetime | None
    purge_after: datetime | None


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _subject_for_kind(projection_kind: ProjectionKind) -> str:
    try:
        return _PROJECTION_SUBJECT[projection_kind]
    except (KeyError, TypeError):
        raise ProjectionBuildError(
            f"unsupported projection_kind: {projection_kind!r}"
        ) from None


def _visibility_for_kind(projection_kind: ProjectionKind) -> ProjectionVisibility:
    try:
        return _PROJECTION_VISIBILITY[projection_kind]
    except (KeyError, TypeError):
        raise ProjectionBuildError(
            f"unsupported projection_kind: {projection_kind!r}"
        ) from None


# ----------------------------------------------------------------------
# 纯函数契约（简报 Step 3）
# ----------------------------------------------------------------------


def build_projection_payload(subject: str, confirmed_fields: list[dict]) -> dict:
    """Build the minimal projection payload from confirmed field dicts.

    Only allowlisted fields enter the payload; authentication/verification
    fields (``realname_status`` etc.) are always rejected.  ``subject`` must be
    ``personal`` or ``ideal_partner`` (统一方案 §7.4 主体隔离).
    """
    if subject not in {"personal", "ideal_partner"}:
        raise ValueError("unsupported profile subject")
    payload: dict[str, Any] = {}
    for field in confirmed_fields:
        key = field["field_key"]
        if key not in PROFILE_ALLOWLIST:
            # Projection is a privacy boundary: tolerate polluted historical
            # revisions but never copy authentication/verification fields out.
            continue
        field_subject = field.get("subject")
        if field_subject is not None and str(field_subject) != subject:
            raise ProjectionBuildError("projection contains a cross-subject field")
        payload[key] = field["value"]
    return payload


def projection_is_current(stored: RevisionVector, current: RevisionVector) -> bool:
    """Return True only when the full five-dimension vector is equal (§5.5).

    Any dimension mismatch — including a newer source — means the stored
    projection is stale and must not be surfaced as current data.
    """
    return stored == current


_REVISION_VECTOR_DIMENSIONS = (
    "profile",
    "preference",
    "privacy",
    "relationship",
    "policy",
)


def _revision_record_matches(
    recorded: dict[str, Any],
    current: dict[str, Any],
    *,
    pinned: bool,
) -> bool:
    """Check a published revision's recorded vector against the build vector.

    A pinned call claims one exact enqueue-time vector, so the revision must
    have been published under exactly that vector.  An unpinned call stores the
    current vector and may legitimately project content from a revision that
    predates later bumps of unrelated dimensions — it is rejected only when the
    recorded vector is ahead of the current one in any dimension, which a real
    immutable revision can never be.
    """
    if pinned:
        return recorded == current
    return all(
        int(recorded.get(key, 0) or 0) <= int(current.get(key, 0) or 0)
        for key in _REVISION_VECTOR_DIMENSIONS
    )


def projection_source_hash(
    projection_kind: ProjectionKind,
    subject: str,
    payload: dict[str, Any],
    revision: RevisionVector,
) -> str:
    """Deterministic content hash over kind, subject, fields and revision."""
    raw = json.dumps(
        {
            "projection_kind": projection_kind.value,
            "subject": subject,
            "fields": payload,
            "revision": revision.as_dict(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ----------------------------------------------------------------------
# 内部读取/写入辅助
# ----------------------------------------------------------------------


async def _first_row(result: Any) -> dict[str, Any] | None:
    return result.mappings().first()


async def _load_latest_revision(
    db: AsyncSession, user_id: int, subject: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT r.id, r.user_id, r.subject, r.revision_no, r.policy_revision, "
            "       r.source_revision_json, r.published_at "
            "FROM ai_profile_revision r "
            "WHERE r.user_id = :user_id AND r.subject = :subject "
            "ORDER BY r.revision_no DESC, r.id DESC LIMIT 1"
        ),
        {"user_id": user_id, "subject": subject},
    )
    return await _first_row(result)


async def _load_revision_by_id(
    db: AsyncSession, user_id: int, subject: str, revision_id: int
) -> dict[str, Any] | None:
    """Load the exact published revision pinned by a projection task."""
    result = await db.execute(
        text(
            "SELECT r.id, r.user_id, r.subject, r.revision_no, r.policy_revision, "
            "       r.source_revision_json, r.published_at "
            "FROM ai_profile_revision r "
            "WHERE r.id = :revision_id AND r.user_id = :user_id AND r.subject = :subject "
            "LIMIT 1"
        ),
        {"revision_id": revision_id, "user_id": user_id, "subject": subject},
    )
    return await _first_row(result)


async def _load_revision_fields(
    db: AsyncSession, revision_id: int
) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            "SELECT field_key, subject, value_json, field_kind, category, content, "
            "schema_version "
            "FROM ai_profile_revision_field WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )
    return result.mappings().all()


# entry_digest 单行内容截断长度：摘要只服务搜索/匹配度与调试，保留头 100 字。
_ENTRY_DIGEST_LINE_MAX = 100


async def _compute_first_seen_revision(
    db: AsyncSession, field_rows: list[dict[str, Any]]
) -> int | None:
    """物化 isNew 条目群组的最早来源 revision_no（WP-P4）。

    对本次发布包含的条目 field_key 在历史中求 MIN(revision_no)，取其最小值
    写入投影行；纯 structured 投影返回 NULL。is_new 的读取端判定仍以
    「MIN(revision_no) == 最新 revision_no」为准（锁定语义），此列仅作为
    投影层的群组锚，供调试与后续增量读取优化。
    """
    entry_keys = [
        str(row.get("field_key"))
        for row in field_rows
        if str(row.get("field_kind") or "structured") == "entry"
    ]
    if not entry_keys:
        return None
    placeholders = ", ".join(f":k{idx}" for idx in range(len(entry_keys)))
    params: dict[str, Any] = {
        f"k{idx}": key for idx, key in enumerate(entry_keys)
    }
    result = await db.execute(
        text(
            "SELECT MIN(r.revision_no) AS first_no "
            "FROM ai_profile_revision_field f "
            "JOIN ai_profile_revision r ON r.id = f.revision_id "
            f"WHERE f.field_kind = 'entry' AND f.field_key IN ({placeholders})"
        ),
        params,
    )
    row = await _first_row(result)
    return int(row["first_no"]) if row and row.get("first_no") is not None else None


def build_entry_digest(field_rows: list[dict[str, Any]]) -> str | None:
    """把已发布条目行折成紧凑摘要（每行“分类：内容”），无条目返回 NULL。

    供搜索（猜你喜欢）与匹配度消费；行内容全部来自用户已确认条目，不含
    原始回答文本。ideal_partner 维度的摘要落在 self_only 投影行，天然不外
    泄（visibility_class 纪律不变）。
    """
    lines: list[str] = []
    for row in field_rows:
        if str(row.get("field_kind") or "structured") != "entry":
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        category = str(row.get("category") or "")
        label = PROFILE_ENTRY_CATEGORY_LABELS.get(category, category)
        if len(content) > _ENTRY_DIGEST_LINE_MAX:
            content = content[:_ENTRY_DIGEST_LINE_MAX] + "…"
        lines.append(f"{label}：{content}")
    if not lines:
        return None
    return "\n".join(lines)


async def _load_consent_snapshot(
    db: AsyncSession, user_id: int, scope: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT user_id, scope, version, policy_revision, granted_at "
            "FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "scope": scope},
    )
    row = await _first_row(result)
    if row is None:
        return None
    granted_at = row.get("granted_at")
    return {
        "scope": row.get("scope") or scope,
        "version": row.get("version") or "",
        "policy_revision": row.get("policy_revision") or "",
        "granted_at": granted_at.isoformat() if granted_at else None,
    }


async def _consent_snapshot_is_current(
    db: AsyncSession, user_id: int, snapshot: dict[str, Any]
) -> bool:
    """Require the pinned grant/version to still be active before projection write."""
    scope = str(snapshot.get("scope") or "")
    version = str(snapshot.get("version") or "")
    if scope != PROFILE_CONSENT_SCOPE or not version:
        return False
    result = await db.execute(
        text(
            "SELECT version, policy_revision, granted_at FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND version = :version "
            "AND revoked_at IS NULL ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "scope": scope, "version": version},
    )
    row = await _first_row(result)
    if row is None:
        return False
    if snapshot.get("policy_revision") and str(row.get("policy_revision") or "") != str(
        snapshot["policy_revision"]
    ):
        return False
    granted_at = row.get("granted_at")
    current_granted_at = (
        granted_at.isoformat() if hasattr(granted_at, "isoformat") else str(granted_at or "")
    )
    return current_granted_at == str(snapshot.get("granted_at") or "")


async def _load_current_revision(db: AsyncSession, user_id: int) -> RevisionVector:
    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = await _first_row(result)
    if row is None:
        return RevisionVector()
    return RevisionVector(
        profile=int(row["profile_revision"] or 0),
        preference=int(row["preference_revision"] or 0),
        privacy=int(row["privacy_revision"] or 0),
        relationship=int(row["relationship_revision"] or 0),
        policy=int(row["policy_revision"] or 0),
    )


async def _load_revision_vector_or_default(
    db: AsyncSession,
    user_id: int,
    revision_vector: RevisionVector | None,
) -> RevisionVector:
    """Return the passed vector, or reload the current one from the DB."""
    if revision_vector is not None:
        return revision_vector
    return await _load_current_revision(db, user_id)


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------


async def invalidate_projection(
    db: AsyncSession,
    user_id: int,
    reason: str,
    source_revision: RevisionVector,
    projection_kind: ProjectionKind | None = None,
) -> int:
    """Invalidate active projections whose version vector is behind (§5.6).

    A projection is stale exactly when it was built from an older snapshot than
    ``source_revision`` — component-wise behind, i.e. every stored dimension is
    <= the source dimension and at least one is strictly lower.  A projection
    whose vector equals the source (already current) or is ahead in any
    dimension is never touched, so an old event can never invalidate a newer
    projection.  ``projection_kind`` scopes the invalidation to one kind (used
    by rebuild paths).  Returns the number of rows invalidated.
    """
    kind_filter = ""
    params: dict[str, Any] = {
        "user_id": user_id,
        "reason": reason,
        "profile_revision": source_revision.profile,
        "preference_revision": source_revision.preference,
        "privacy_revision": source_revision.privacy,
        "relationship_revision": source_revision.relationship,
        "policy_revision": source_revision.policy,
    }
    if projection_kind is not None:
        kind_filter = " AND projection_kind = :projection_kind"
        params["projection_kind"] = projection_kind.value
    result = await db.execute(
        text(
            "UPDATE ai_feature_projection "
            "SET status = 'invalidated', invalidated_at = UTC_TIMESTAMP(), "
            "    invalidated_reason = :reason, "
            "    purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
            "    updated_at = UTC_TIMESTAMP() "
            "WHERE subject_user_id = :user_id AND status = 'active'"
            + kind_filter
            + " AND (profile_revision < :profile_revision "
            "OR preference_revision < :preference_revision "
            "OR privacy_revision < :privacy_revision "
            "OR relationship_revision < :relationship_revision "
            "OR policy_revision < :policy_revision) "
            "AND profile_revision <= :profile_revision "
            "AND preference_revision <= :preference_revision "
            "AND privacy_revision <= :privacy_revision "
            "AND relationship_revision <= :relationship_revision "
            "AND policy_revision <= :policy_revision"
        ),
        params,
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def build_feature_projection(
    db: AsyncSession,
    user_id: int,
    projection_kind: ProjectionKind,
    revision_vector: RevisionVector | None,
    *,
    published_revision_id: int | None = None,
    consent_snapshot: dict[str, Any] | None = None,
) -> FeatureProjection:
    """Build and persist one minimal, versioned feature projection.

    Reads the latest immutable ``ai_profile_revision`` (confirmed fields only by
    Task 8 construction), keeps only allowlisted fields, snapshots the consent
    grant and the full five-dimension revision vector, then upserts an
    ``active`` row: active projections of the same kind with a different
    ``source_hash`` are invalidated, while the row with the same
    ``source_hash`` + ``projection_version`` is updated in place so a same-version
    rebuild never collides with ``uk_ai_feature_projection``.  Raises
    :class:`ProjectionBuildError` (never writes an empty projection) when the
    user has no published revision for the kind's subject, no allowlisted
    confirmed fields, or no active consent.  Does not commit.
    """
    subject = _subject_for_kind(projection_kind)
    visibility = _visibility_for_kind(projection_kind)
    revision = await _load_revision_vector_or_default(db, user_id, revision_vector)

    if consent_snapshot is None:
        consent_snapshot = await _load_consent_snapshot(
            db, user_id, PROFILE_CONSENT_SCOPE
        )
    if not consent_snapshot:
        raise ProjectionBuildError("no active profile_text_extract consent")
    if not await _consent_snapshot_is_current(db, user_id, consent_snapshot):
        raise ProjectionBuildError("pinned consent is no longer active")

    latest = (
        await _load_revision_by_id(db, user_id, subject, published_revision_id)
        if published_revision_id is not None
        else await _load_latest_revision(db, user_id, subject)
    )
    if latest is None:
        raise ProjectionBuildError(f"no published {subject} revision")
    revision_id = int(latest["id"])
    latest_source_revision = _maybe_json(latest.get("source_revision_json"))
    if not isinstance(latest_source_revision, dict):
        raise ProjectionBuildError("published revision has no source revision")
    if not _revision_record_matches(
        latest_source_revision,
        revision.as_dict(),
        pinned=revision_vector is not None,
    ):
        raise ProjectionBuildError("published revision vector is stale")
    if consent_snapshot.get("policy_revision") and str(
        latest.get("policy_revision") or ""
    ) != str(consent_snapshot["policy_revision"]):
        raise ProjectionBuildError("published revision policy is stale")
    field_rows = await _load_revision_fields(db, revision_id)

    confirmed_fields: list[dict[str, Any]] = []
    schema_version = PROJECTION_SCHEMA_VERSION
    for field in field_rows:
        confirmed_fields.append(
            {
                "field_key": str(field["field_key"]),
                "subject": str(field.get("subject") or subject),
                "value": _maybe_json(field.get("value_json")),
            }
        )
        if field.get("schema_version"):
            schema_version = str(field["schema_version"])

    payload = build_projection_payload(subject, confirmed_fields)
    if not payload:
        raise ProjectionBuildError(f"no allowlisted confirmed fields for {subject}")
    entry_digest = build_entry_digest(field_rows)
    first_seen_revision = await _compute_first_seen_revision(db, field_rows)

    source_hash = projection_source_hash(projection_kind, subject, payload, revision)
    expires_at = _now_utc() + timedelta(days=_PROJECTION_TTL_DAYS)

    # 唯一键 uk_ai_feature_projection
    # (subject_user_id, projection_kind, source_hash, projection_version)
    # 不含 status：无条件失效同 kind 全部 active 行会把同 source_hash 的旧行
    # 也标 invalidated，随后同版本重建（同 revision + 同 payload → 同
    # source_hash）的 INSERT 会撞唯一键报 IntegrityError，且旧 active 行已被
    # 失效 → 用户出现无任何 active 投影的状态。因此重建只失效「同 kind 不同
    # source_hash」的旧行；同 source_hash 的行由下方 INSERT ... ON DUPLICATE
    # KEY UPDATE 原位更新回 active（含 invalidated_at/invalidated_reason/
    # purge_after 清空）。删除事件路径的版本守卫语义由 invalidate_projection
    # 单独承担。
    await db.execute(
        text(
            "UPDATE ai_feature_projection "
            "SET status = 'invalidated', invalidated_at = UTC_TIMESTAMP(), "
            "    invalidated_reason = 'rebuild', "
            "    purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
            "    updated_at = UTC_TIMESTAMP() "
            "WHERE subject_user_id = :subject_user_id "
            "AND projection_kind = :projection_kind AND status = 'active' "
            "AND source_hash <> :source_hash"
        ),
        {
            "subject_user_id": user_id,
            "projection_kind": projection_kind.value,
            "source_hash": source_hash,
        },
    )

    result = await db.execute(
        text(
            "INSERT INTO ai_feature_projection "
            "(subject_user_id, projection_kind, source_hash, projection_version, "
            " fields_json, entry_digest, first_seen_revision, source_revision_json, "
            " profile_revision, "
            " preference_revision, privacy_revision, relationship_revision, "
            " policy_revision, consent_snapshot_json, visibility_class, "
            " status, expires_at, created_at, updated_at) "
            "VALUES (:subject_user_id, :projection_kind, :source_hash, "
            " :projection_version, :fields_json, :entry_digest, "
            " :first_seen_revision, :source_revision_json, "
            " :profile_revision, :preference_revision, :privacy_revision, "
            " :relationship_revision, :policy_revision, :consent_snapshot_json, "
            " :visibility_class, 'active', :expires_at, UTC_TIMESTAMP(), "
            " UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            " fields_json = VALUES(fields_json), "
            " entry_digest = VALUES(entry_digest), "
            " first_seen_revision = VALUES(first_seen_revision), "
            " source_revision_json = VALUES(source_revision_json), "
            " profile_revision = VALUES(profile_revision), "
            " preference_revision = VALUES(preference_revision), "
            " privacy_revision = VALUES(privacy_revision), "
            " relationship_revision = VALUES(relationship_revision), "
            " policy_revision = VALUES(policy_revision), "
            " consent_snapshot_json = VALUES(consent_snapshot_json), "
            " visibility_class = VALUES(visibility_class), "
            " status = 'active', "
            " invalidated_at = NULL, invalidated_reason = NULL, "
            " purge_after = NULL, expires_at = VALUES(expires_at), "
            " updated_at = UTC_TIMESTAMP()"
        ),
        {
            "subject_user_id": user_id,
            "projection_kind": projection_kind.value,
            "source_hash": source_hash,
            "projection_version": schema_version,
            "fields_json": json.dumps(payload, ensure_ascii=False),
            "entry_digest": entry_digest,
            "first_seen_revision": first_seen_revision,
            "source_revision_json": json.dumps(revision.as_dict(), ensure_ascii=False),
            "profile_revision": revision.profile,
            "preference_revision": revision.preference,
            "privacy_revision": revision.privacy,
            "relationship_revision": revision.relationship,
            "policy_revision": revision.policy,
            "consent_snapshot_json": json.dumps(consent_snapshot, ensure_ascii=False),
            "visibility_class": visibility.value,
            "expires_at": expires_at,
        },
    )
    projection_id = int(getattr(result, "lastrowid", 0) or 0)

    return FeatureProjection(
        id=projection_id if projection_id else None,
        subject_user_id=user_id,
        projection_kind=projection_kind,
        source_hash=source_hash,
        projection_version=schema_version,
        fields=payload,
        source_revision=revision,
        consent_snapshot=consent_snapshot,
        visibility_class=visibility,
        status="active",
        expires_at=expires_at,
        purge_after=None,
    )


def _maybe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value
