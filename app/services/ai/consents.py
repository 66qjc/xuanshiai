"""Public AI consent grant/revoke service with revision and cleanup semantics."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_common import (
    CONSENT_SCOPES,
    AiConsentGrantRequest,
    AiConsentListResponse,
    AiConsentOperationResponse,
    AiConsentRead,
)
from app.services.ai.profile import PROFILE_POLICY_REVISION
from app.services.ai.tasks import enqueue_task
from app.services.revisions import RevisionKind, RevisionVector, increment_revision_and_enqueue

# 冻结的授权文案版本：客户端提交的 consent_version 必须匹配此值，否则拒绝授权。
# policy_revision 复用 profile.py 中的 PROFILE_POLICY_REVISION（单一事实源），避免漂移。
# 每个 scope 有独立的 consent_version（统一方案 §6.3），不再复用单一常量。
CONSENT_VERSIONS: dict[str, str] = {
    "profile_text_extract": "profile-text-v1",
    "search_parse": "search-parse-v1",
    "compatibility_shadow": "compatibility-shadow-v1",
    "compatibility_display": "compatibility-display-v1",
}


def get_consent_version(scope: str) -> str:
    """Return the frozen consent_version for the given scope."""
    if scope not in CONSENT_VERSIONS:
        raise ConsentError("AI_INPUT_INVALID", "AI consent scope is invalid", 400)
    return CONSENT_VERSIONS[scope]


# Backwards-compat alias used by other modules that imported the old constant.
# Tests assert this is no longer "profile-consent-v1".
PROFILE_CONSENT_VERSION = CONSENT_VERSIONS["profile_text_extract"]


class ConsentError(Exception):
    """Stable public consent error mapped by the route layer."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def validate_scope(scope: str) -> str:
    value = str(scope)
    if value not in CONSENT_SCOPES:
        raise ConsentError("AI_INPUT_INVALID", "AI consent scope is invalid", 400)
    return value


def _digest(operation: str, scope: str, body: AiConsentGrantRequest | None) -> str:
    payload = {
        "operation": operation,
        "scope": scope,
        "consent_version": body.consent_version if body else None,
        "policy_revision": body.policy_revision if body else None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _first(result: Any) -> dict[str, Any] | None:
    return result.mappings().first()


def _consent_read(row: dict[str, Any]) -> AiConsentRead:
    return AiConsentRead(
        scope=str(row["scope"]),
        version=str(row["version"]),
        policy_revision=str(row["policy_revision"]),
        granted_at=row["granted_at"],
    )


def _decode_operation(row: dict[str, Any], digest: str) -> AiConsentOperationResponse:
    if str(row["request_digest"]) != digest:
        raise ConsentError(
            "AI_CONSENT_IDEMPOTENCY_CONFLICT",
            "Idempotency-Key was used for a different consent request",
            409,
        )
    payload = row["response_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return AiConsentOperationResponse.model_validate(payload)


def _cleanup_payload(scope: str, user_id: int, revision: RevisionVector) -> dict[str, Any]:
    return {
        "scope": "consent",
        "resource_id": f"consent:{user_id}:{scope}",
        "version": revision.as_dict(),
        "purge_deadline": (
            datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=15)
        ).isoformat(),
    }


def _consent_tombstone(user_id: int, scope: str, granted_at: datetime) -> str:
    granted = granted_at.isoformat() if hasattr(granted_at, "isoformat") else str(granted_at)
    return hashlib.sha256(f"consent:{user_id}:{scope}:{granted}".encode()).hexdigest()


async def _find_operation(
    db: AsyncSession,
    user_id: int,
    operation: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT operation_id, request_digest, response_json "
            "FROM ai_consent_operation "
            "WHERE user_id = :user_id AND operation = :operation "
            "AND idempotency_key = :idempotency_key LIMIT 1"
        ),
        {
            "user_id": user_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
        },
    )
    return await _first(result)


async def _lock_privacy_revision(db: AsyncSession, user_id: int) -> int:
    await db.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, "
            " relationship_revision, policy_revision, updated_at) "
            "VALUES (:user_id, 0, 0, 0, 0, 0, UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)"
        ),
        {"user_id": user_id},
    )
    result = await db.execute(
        text(
            "SELECT privacy_revision FROM user_revision_state "
            "WHERE user_id = :user_id FOR UPDATE"
        ),
        {"user_id": user_id},
    )
    row = await _first(result)
    return int(row["privacy_revision"] or 0) if row else 0


async def _read_privacy_revision(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        text(
            "SELECT privacy_revision FROM user_revision_state "
            "WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = await _first(result)
    return int(row["privacy_revision"] or 0) if row else 0


async def _write_operation(
    db: AsyncSession,
    user_id: int,
    scope: str,
    operation: str,
    idempotency_key: str,
    digest: str,
    response: AiConsentOperationResponse,
) -> None:
    await db.execute(
        text(
            "INSERT INTO ai_consent_operation "
            "(operation_id, user_id, scope, operation, idempotency_key, request_digest, response_json) "
            "VALUES (:operation_id, :user_id, :scope, :operation, :idempotency_key, :request_digest, :response_json)"
        ),
        {
            "operation_id": response.operation_id,
            "user_id": user_id,
            "scope": scope,
            "operation": operation,
            "idempotency_key": idempotency_key,
            "request_digest": digest,
            "response_json": json.dumps(response.model_dump(mode="json"), ensure_ascii=False),
        },
    )


async def _enqueue_consent_cleanup(
    db: AsyncSession,
    user_id: int,
    scope: str,
    idempotency_key: str,
    revision: RevisionVector,
) -> str:
    cleanup_key = f"consent:{scope}:{idempotency_key}"
    # 对完整键做 SHA256 再截断到 128 字符，避免长 Idempotency-Key 直接截断后碰撞
    # （原始键 [:128] 截断会让前 128 字符相同但尾部不同的键坍缩为同一个 task）。
    cleanup_idempotency_key = hashlib.sha256(cleanup_key.encode("utf-8")).hexdigest()[:128]
    digest = hashlib.sha256(
        f"consent-revoke:{scope}:{user_id}".encode()
    ).hexdigest()
    task = await enqueue_task(
        db,
        owner_user_id=user_id,
        task_type="cleanup",
        idempotency_key=cleanup_idempotency_key,
        request_hash=digest,
        revisions=revision,
        consent=None,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "task_id": task.task_id,
            "payload_summary": json.dumps(
                _cleanup_payload(scope, user_id, revision),
                ensure_ascii=False,
            ),
        },
    )
    return task.task_id


async def _invalidate_revoked_scope(
    db: AsyncSession,
    user_id: int,
    scope: str,
) -> None:
    """Make all affected read surfaces unavailable before the response returns."""
    await db.execute(
        text(
            "UPDATE ai_task SET status = 'cancelled', finished_at = UTC_TIMESTAMP(), "
            "lease_owner = NULL, lease_until = NULL, payload_summary = NULL, "
            "consent_snapshot_json = NULL, source_revision_json = NULL, result_ref = NULL, "
            "owner_tombstone = COALESCE(owner_tombstone, SHA2(CONCAT('ai-task-owner:', owner_user_id, ':', task_id), 256)), "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE owner_user_id = :user_id AND status IN ('queued', 'leased', 'running', 'retry_wait') "
            "AND JSON_UNQUOTE(JSON_EXTRACT(consent_snapshot_json, '$.scope')) = :scope"
        ),
        {"user_id": user_id, "scope": scope},
    )
    if scope == "profile_text_extract":
        await db.execute(
            text(
                "UPDATE ai_profile_draft SET status = 'deleted', updated_at = UTC_TIMESTAMP() "
                "WHERE user_id = :user_id AND status <> 'deleted'"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_profile_session SET status = 'cancelled', active_status = 0, "
                "ended_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
                "WHERE user_id = :user_id AND active_status = 1"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_feature_projection SET status = 'invalidated', "
                "invalidated_at = UTC_TIMESTAMP(), invalidated_reason = 'ai_consent_revoked', "
                "purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), updated_at = UTC_TIMESTAMP() "
                "WHERE subject_user_id = :user_id AND status = 'active'"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_search_result r "
                "JOIN ai_search_snapshot s ON s.snapshot_id = r.snapshot_id "
                "SET r.stale = 1, r.updated_at = UTC_TIMESTAMP() "
                "WHERE r.target_user_id = :user_id "
                "OR s.user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_compatibility_snapshot SET status = 'blocked', "
                "invalidated_at = UTC_TIMESTAMP(), purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
                "updated_at = UTC_TIMESTAMP() WHERE viewer_user_id = :user_id OR target_user_id = :user_id"
            ),
            {"user_id": user_id},
        )
    elif scope == "search_parse":
        await db.execute(
            text(
                "UPDATE ai_search_draft SET status = 'expired', updated_at = UTC_TIMESTAMP() "
                "WHERE user_id = :user_id AND status NOT IN ('expired', 'failed')"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_search_snapshot SET status = 'invalidated', invalidated_at = UTC_TIMESTAMP() "
                "WHERE user_id = :user_id AND invalidated_at IS NULL"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "UPDATE ai_search_result r JOIN ai_search_snapshot s ON s.snapshot_id = r.snapshot_id "
                "SET r.stale = 1, r.updated_at = UTC_TIMESTAMP() WHERE s.user_id = :user_id"
            ),
            {"user_id": user_id},
        )
    elif scope == "compatibility_shadow":
        await db.execute(
            text(
                "UPDATE ai_compatibility_snapshot SET status = 'blocked', "
                "invalidated_at = UTC_TIMESTAMP(), purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
                "updated_at = UTC_TIMESTAMP() WHERE viewer_user_id = :user_id OR target_user_id = :user_id"
            ),
            {"user_id": user_id},
        )


async def list_consents(db: AsyncSession, user_id: int) -> AiConsentListResponse:
    result = await db.execute(
        text(
            "SELECT scope, version, policy_revision, granted_at FROM ai_consent_grant "
            "WHERE user_id = :user_id AND revoked_at IS NULL ORDER BY scope"
        ),
        {"user_id": user_id},
    )
    rows = result.mappings().all()
    revision = await _read_privacy_revision(db, user_id)
    return AiConsentListResponse(
        consents=[_consent_read(row) for row in rows], privacy_revision=revision
    )


async def grant_consent(
    db: AsyncSession,
    user_id: int,
    scope: str,
    body: AiConsentGrantRequest,
    idempotency_key: str,
    expected_privacy_revision: int,
) -> AiConsentOperationResponse:
    scope = validate_scope(scope)
    digest = _digest("grant", scope, body)
    # Idempotency wins over payload validation for an already-used key.  A
    # replay with a different (even malformed/stale) payload must report the
    # stable idempotency conflict instead of leaking a version-validation
    # result for the new payload.
    existing = await _find_operation(db, user_id, "grant", idempotency_key)
    if existing:
        return _decode_operation(existing, digest)
    # 缺陷18：运行时门禁——客户端提交的 consent_version / policy_revision 必须匹配
    # 服务端冻结的 per-scope 版本，否则拒绝授权，防止客户端用过期/篡改的授权文案绕过。
    frozen_version = get_consent_version(scope)
    if body.consent_version != frozen_version:
        raise ConsentError(
            "AI_CONSENT_VERSION_CONFLICT",
            f"consent_version does not match the frozen value ({frozen_version})",
            409,
        )
    if body.policy_revision != PROFILE_POLICY_REVISION:
        raise ConsentError(
            "AI_CONSENT_VERSION_CONFLICT",
            f"policy_revision does not match the frozen value ({PROFILE_POLICY_REVISION})",
            409,
        )
    current_privacy = await _lock_privacy_revision(db, user_id)
    existing = await _find_operation(db, user_id, "grant", idempotency_key)
    if existing:
        return _decode_operation(existing, digest)
    if current_privacy != expected_privacy_revision:
        raise ConsentError("AI_CONSENT_VERSION_CONFLICT", "privacy revision is stale", 409)
    granted_at = datetime.now(UTC).replace(tzinfo=None)
    # 缺陷8：撤销同一 (user_id, scope) 下任何已有的活跃授权，使 grant 成为
    # 「撤销旧 + 授予新」的原子操作，避免唯一键含 granted_at 导致重复授予两行。
    await db.execute(
        text(
            "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP(), user_id = NULL "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL"
        ),
        {"user_id": user_id, "scope": scope},
    )
    # 缺陷40：捕获 INSERT 唯一键冲突（并发重复授予），回滚后回读既有操作记录，
    # 与 create_profile_session 的 IntegrityError→回读模式一致。
    try:
        await db.execute(
            text(
                "INSERT INTO ai_consent_grant "
                "(user_id, scope, version, policy_revision, granted_at) "
                "VALUES (:user_id, :scope, :version, :policy_revision, :granted_at)"
            ),
            {
                "user_id": user_id,
                "scope": scope,
                "version": body.consent_version,
                "policy_revision": body.policy_revision,
                "granted_at": granted_at,
            },
        )
    except IntegrityError:
        await db.rollback()
        existing = await _find_operation(db, user_id, "grant", idempotency_key)
        if existing is None:
            raise
        return _decode_operation(existing, digest)
    revision = await increment_revision_and_enqueue(
        db,
        user_id,
        RevisionKind.PRIVACY,
        (f"ai_consent_granted:{scope}",),
        "ai_consent_granted",
        priority=20,
    )
    response = AiConsentOperationResponse(
        operation_id=uuid.uuid4().hex,
        scope=scope,
        operation="grant",
        status="active",
        consent=AiConsentRead(
            scope=scope,
            version=body.consent_version,
            policy_revision=body.policy_revision,
            granted_at=granted_at,
        ),
        privacy_revision=revision.privacy,
    )
    await _write_operation(db, user_id, scope, "grant", idempotency_key, digest, response)
    return response


async def revoke_consent(
    db: AsyncSession,
    user_id: int,
    scope: str,
    idempotency_key: str,
    expected_privacy_revision: int,
) -> AiConsentOperationResponse:
    scope = validate_scope(scope)
    digest = _digest("revoke", scope, None)
    existing = await _find_operation(db, user_id, "revoke", idempotency_key)
    if existing:
        return _decode_operation(existing, digest)
    current_privacy = await _lock_privacy_revision(db, user_id)
    existing = await _find_operation(db, user_id, "revoke", idempotency_key)
    if existing:
        return _decode_operation(existing, digest)
    if current_privacy != expected_privacy_revision:
        raise ConsentError("AI_CONSENT_VERSION_CONFLICT", "privacy revision is stale", 409)
    result = await db.execute(
        text(
            "SELECT scope, version, policy_revision, granted_at FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1 FOR UPDATE"
        ),
        {"user_id": user_id, "scope": scope},
    )
    active = await _first(result)
    cleanup_task_id = None
    if active is None:
        status = "already_revoked"
        revision = RevisionVector(privacy=current_privacy)
        consent = None
    else:
        await db.execute(
            text(
                "UPDATE ai_consent_grant SET user_id = NULL, "
                "revoked_at = UTC_TIMESTAMP(), "
                "revoke_reason = 'public_api_revoke', updated_at = UTC_TIMESTAMP(), "
                "user_tombstone = :user_tombstone "
                "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL"
            ),
            {
                "user_id": user_id,
                "scope": scope,
                "user_tombstone": _consent_tombstone(
                    user_id, scope, active["granted_at"]
                ),
            },
        )
        revision = await increment_revision_and_enqueue(
            db,
            user_id,
            RevisionKind.PRIVACY,
            (f"ai_consent_revoked:{scope}",),
            "ai_consent_revoked",
            priority=10,
        )
        await _invalidate_revoked_scope(db, user_id, scope)
        cleanup_task_id = await _enqueue_consent_cleanup(
            db, user_id, scope, idempotency_key, revision
        )
        status = "revoked"
        consent = None
    response = AiConsentOperationResponse(
        operation_id=uuid.uuid4().hex,
        scope=scope,
        operation="revoke",
        status=status,
        consent=consent,
        cleanup_task_id=cleanup_task_id,
        privacy_revision=revision.privacy,
    )
    await _write_operation(db, user_id, scope, "revoke", idempotency_key, digest, response)
    return response
