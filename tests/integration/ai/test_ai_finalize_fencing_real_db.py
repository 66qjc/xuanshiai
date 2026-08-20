"""G2-C 场景 1/2：finalize 门禁真实竞态测试。

场景 1：Provider 计算完成、持久化前撤权 —— 门禁必须 supersede，不得留下草稿。
场景 2：handler 计算后、finalize 前 Worker 被杀 —— 恢复后只有一份业务结果。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import ProfileSubject
from app.services.ai.consents import grant_consent
from app.services.ai.profile import (
    create_profile_session,
    extract_profile_turn,
    submit_profile_turn,
)
from app.services.ai.tasks import (
    AiTaskRecord,
    AiTaskStatus,
    claim_tasks,
    complete_task,
    reap_expired_leases,
    start_task,
)

POLICY_REVISION = "ai-policy-2026-08-07-v1"
CONSENT_VERSION = "profile-text-v1"
USER_REVOKED = 9_876_544_340
USER_KILLED = 9_876_544_341


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def _clean(db: AsyncSession, user_id: int) -> None:
    for statement in (
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_draft WHERE user_id = :user_id",
        "DELETE FROM ai_profile_turn WHERE user_id = :user_id",
        "DELETE FROM ai_profile_session WHERE user_id = :user_id",
        "DELETE FROM ai_profile_summary WHERE user_id = :user_id",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN (SELECT id FROM ai_profile_revision WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_revision WHERE user_id = :user_id",
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
        "DELETE FROM ai_consent_operation WHERE user_id = :user_id",
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
    ):
        await db.execute(text(statement), {"user_id": user_id})
    await db.commit()


async def _seed_user_and_task(
    db: AsyncSession, user_id: int
) -> tuple[str, str]:
    """Seed revision state + consent + session + turn; return (session_id, task_id)."""
    await db.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision) "
            "VALUES (:user_id, 0, 0, 0, 0, 0)"
        ),
        {"user_id": user_id},
    )
    await grant_consent(
        db,
        user_id,
        "profile_text_extract",
        AiConsentGrantRequest(
            consent_version=CONSENT_VERSION,
            policy_revision=POLICY_REVISION,
        ),
        f"fence-grant-{user_id}",
        0,
    )
    await db.commit()
    session = await create_profile_session(
        db, user_id, ProfileSubject.PERSONAL, CONSENT_VERSION, f"fence-session-{user_id}"
    )
    accepted = await submit_profile_turn(
        db,
        session.session_id,
        user_id,
        f"fence-turn-{user_id}",
        "按固定 mock 夹具生成可确认字段。",
        f"fence-extract-{user_id}",
    )
    await db.commit()
    return session.session_id, str(accepted.task_id)


async def _claim_and_start(
    factory: async_sessionmaker[AsyncSession], worker_id: str
) -> AiTaskRecord:
    async with factory() as claim_db:
        claimed = await claim_tasks(claim_db, worker_id, _now(), 10)
        await claim_db.commit()
    assert len(claimed) == 1
    async with factory() as start_db:
        started = await start_task(start_db, claimed[0].task_id, worker_id)
        await start_db.commit()
    return started


@pytest.mark.asyncio
async def test_consent_revoked_after_compute_leaves_no_draft(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    user_id = USER_REVOKED
    await _clean(real_db_session, user_id)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as seed_db:
        _, task_id = await _seed_user_and_task(seed_db, user_id)

    started = await _claim_and_start(factory, "worker-a")

    async with factory() as handler_db:
        result = await extract_profile_turn(handler_db, started, "worker-a")
        assert result is not None
        result_ref, revisions = result
        # Provider 已计算完成、草稿尚未持久化：此刻另一会话撤权。
        async with factory() as revoke_db:
            await revoke_db.execute(
                text(
                    "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP(), "
                    "user_id = NULL, updated_at = UTC_TIMESTAMP() "
                    "WHERE user_id = :user_id AND scope = 'profile_text_extract' "
                    "AND revoked_at IS NULL"
                ),
                {"user_id": user_id},
            )
            await revoke_db.commit()
        # finalize 门禁必须 supersede；handler 会话随后整体回滚。
        async with factory() as finalize_db:
            completed = await complete_task(
                finalize_db, task_id, "worker-a", result_ref, revisions
            )
            assert completed.status is AiTaskStatus.SUPERSEDED
            await finalize_db.commit()
        await handler_db.rollback()

    async with factory() as check_db:
        draft_count = await check_db.scalar(
            text("SELECT COUNT(*) FROM ai_profile_draft WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        assert int(draft_count or 0) == 0, "撤权后不得新增 draft"
        task_status = await check_db.scalar(
            text("SELECT status FROM ai_task WHERE task_id = :task_id"),
            {"task_id": task_id},
        )
        assert task_status == "superseded"
    await _clean(real_db_session, user_id)


@pytest.mark.asyncio
async def test_worker_killed_after_compute_yields_single_business_result(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    user_id = USER_KILLED
    await _clean(real_db_session, user_id)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as seed_db:
        session_id, task_id = await _seed_user_and_task(seed_db, user_id)

    started = await _claim_and_start(factory, "worker-a")

    # 计算完成、finalize 前 Worker 被杀：handler 会话整体回滚，无任何持久化。
    async with factory() as handler_db:
        result = await extract_profile_turn(handler_db, started, "worker-a")
        assert result is not None
        await handler_db.rollback()

    # 租约过期 → reaper 回收 → worker-b 重领并重跑。
    async with factory() as expire_db:
        await expire_db.execute(
            text(
                "UPDATE ai_task SET lease_until = '2000-01-01 00:00:00' "
                "WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        )
        await expire_db.commit()
    async with factory() as reap_db:
        reaped = await reap_expired_leases(reap_db, _now(), 10)
        await reap_db.commit()
    assert reaped == [task_id]

    restarted = await _claim_and_start(factory, "worker-b")
    assert restarted.task_id == task_id

    async with factory() as handler_db:
        result = await extract_profile_turn(handler_db, restarted, "worker-b")
        assert result is not None
        result_ref, revisions = result
        async with factory() as finalize_db:
            completed = await complete_task(
                finalize_db, task_id, "worker-b", result_ref, revisions
            )
            assert completed.status is AiTaskStatus.SUCCEEDED
            await finalize_db.commit()
        await handler_db.commit()

    async with factory() as check_db:
        drafts = (
            await check_db.execute(
                text(
                    "SELECT draft_id FROM ai_profile_draft "
                    "WHERE user_id = :user_id AND session_id = :session_id"
                ),
                {"user_id": user_id, "session_id": session_id},
            )
        ).scalars().all()
        assert len(drafts) == 1, f"恢复后应只有一份业务结果，实际 drafts={drafts}"
        field_keys = (
            await check_db.execute(
                text(
                    "SELECT field_key FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id ORDER BY field_key"
                ),
                {"draft_id": drafts[0]},
            )
        ).scalars().all()
        assert field_keys
        assert len(field_keys) == len(set(field_keys)), "草稿字段出现重复"
        task_status = await check_db.scalar(
            text("SELECT status FROM ai_task WHERE task_id = :task_id"),
            {"task_id": task_id},
        )
        assert task_status == "succeeded"
    await _clean(real_db_session, user_id)
