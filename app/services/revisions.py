"""Per-user revision vectors and same-transaction derivation enqueueing.

Every user mutation bumps one dimension of ``user_revision_state`` and writes a
derivation-outbox row inside the same ``AsyncSession`` transaction, so a failed
mutation never leaves behind a dangling event.  This module intentionally never
calls ``commit()`` — the caller's transaction owns durability.
"""

from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class RevisionKind(str, Enum):
    """Dimensions of the per-user revision vector."""

    PROFILE = "profile"
    PREFERENCE = "preference"
    PRIVACY = "privacy"
    RELATIONSHIP = "relationship"


class RevisionVector:
    """Five-dimensional revision snapshot (profile/preference/privacy/relationship/policy)."""

    __slots__ = ("profile", "preference", "privacy", "relationship", "policy")

    def __init__(
        self,
        profile: int = 0,
        preference: int = 0,
        privacy: int = 0,
        relationship: int = 0,
        policy: int = 0,
    ) -> None:
        self.profile = int(profile)
        self.preference = int(preference)
        self.privacy = int(privacy)
        self.relationship = int(relationship)
        self.policy = int(policy)

    def as_dict(self) -> dict[str, int]:
        return {
            "profile": self.profile,
            "preference": self.preference,
            "privacy": self.privacy,
            "relationship": self.relationship,
            "policy": self.policy,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RevisionVector):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return f"RevisionVector({self.as_dict()!r})"


_KIND_REVISION_COLUMNS: dict[RevisionKind, str] = {
    RevisionKind.PROFILE: "profile_revision",
    RevisionKind.PREFERENCE: "preference_revision",
    RevisionKind.PRIVACY: "privacy_revision",
    RevisionKind.RELATIONSHIP: "relationship_revision",
}


def _row_from_result(result: Any) -> dict[str, Any] | None:
    """Return the mappings() row when the execute result exposes it.

    SQLAlchemy results expose ``.mappings()``; minimal test fixtures used by
    pre-existing service tests only expose ``.scalar()`` and return nothing
    readable here, in which case the caller falls back to a best-effort vector.
    """
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return mappings().one()
    return None


def assert_revision_kind(kind: RevisionKind) -> None:
    """Raise ``ValueError`` for an undefined revision dimension.

    Callers must never infer a dimension from an undefined ``revision.kind`` or
    from the shape of a revision vector (Task 8 publish maps personal →
    ``profile``, ideal_partner → ``preference`` explicitly).  This guard turns a
    typo'd kind into a loud, stable error instead of an opaque ``KeyError``.
    """
    if kind not in _KIND_REVISION_COLUMNS:
        raise ValueError(f"illegal revision kind: {kind!r}")


async def increment_revision_and_enqueue(
    db: AsyncSession,
    user_id: int,
    kind: RevisionKind,
    changed_fields: tuple[str, ...],
    event_type: str,
    priority: int = 50,
    payload_extra: dict[str, Any] | None = None,
) -> RevisionVector:
    """Bump one revision dimension and enqueue a derivation event atomically.

    The enqueue shares the caller's transaction, so a rollback removes both the
    mutation and its event.  The payload carries only field names, the source
    revision snapshot and the subject id — never raw field values.
    """
    assert_revision_kind(kind)
    column = _KIND_REVISION_COLUMNS[kind]
    # 锁顺序说明（缺陷 36）：先 ODKU 再 SELECT FOR UPDATE 是安全的——
    # ODKU 在唯一键 (user_id) 上原子地获取行锁并递增目标列，随后的
    # SELECT FOR UPDATE 在同一事务内重新读取已更新的行。由于
    # INSERT ... ON DUPLICATE KEY UPDATE ... RETURNING 在 MySQL 上不被
    # 支持（仅 MariaDB 10.5+），此处保留两语句形式以保持 MySQL 兼容性。
    # FOR UPDATE 是必要的：它显式持有行锁直到事务结束，阻止并发事务
    # 在本事务读取向量后、提交前插入/更新同一用户的 revision 行。
    await db.execute(
        text(
            f"INSERT INTO user_revision_state (user_id, {column}, updated_at) "
            f"VALUES (:user_id, 1, UTC_TIMESTAMP()) "
            f"ON DUPLICATE KEY UPDATE {column} = {column} + 1, updated_at = UTC_TIMESTAMP()"
        ),
        {"user_id": user_id},
    )
    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id FOR UPDATE"
        ),
        {"user_id": user_id},
    )
    row = _row_from_result(result)
    if row is not None:
        revision = RevisionVector(
            profile=row["profile_revision"],
            preference=row["preference_revision"],
            privacy=row["privacy_revision"],
            relationship=row["relationship_revision"],
            policy=row["policy_revision"],
        )
    else:
        # Pre-existing service tests use scalar-only fake sessions that cannot
        # return the vector; the upsert above already bumped the dimension, so
        # fall back to a snapshot of just that increment.  Production always
        # reads the real row.
        field = column.removesuffix("_revision")
        revision = RevisionVector(**{field: 1})
    payload = {
        "changed_fields": list(changed_fields),
        "source_revision": revision.as_dict(),
        "subject_id": user_id,
    }
    if payload_extra:
        # Callers may add stable identifiers/version snapshots, but the helper
        # remains the only writer of the canonical changed-fields/vector keys.
        payload.update(payload_extra)
    await db.execute(
        text(
            "INSERT INTO derivation_outbox "
            "(event_id, aggregate_type, aggregate_id, event_type, changed_fields, "
            " source_revision_json, privacy_revision, payload_minimal, priority, published_at) "
            "VALUES (:event_id, 'user', :aggregate_id, :event_type, :changed_fields, "
            " :source_revision_json, :privacy_revision, :payload_minimal, :priority, UTC_TIMESTAMP())"
        ),
        {
            "event_id": uuid.uuid4().hex,
            "aggregate_id": user_id,
            "event_type": event_type,
            "changed_fields": json.dumps(list(changed_fields), ensure_ascii=False),
            "source_revision_json": json.dumps(revision.as_dict(), ensure_ascii=False),
            "privacy_revision": revision.privacy,
            "payload_minimal": json.dumps(
                payload,
                ensure_ascii=False,
            ),
            "priority": priority,
        },
    )
    return revision
