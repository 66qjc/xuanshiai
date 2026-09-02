"""三类推荐物化与 worker 任务（WP-P6c）真实库验收。

覆盖：三视图物化落库（rank/score/generation/engine）、coverage 不足与
无投影用户排除、授权证据过滤、generation 幂等递增、handler 门禁与落库。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.recommend import (
    RECOMMEND_TASK_TYPE,
    load_candidate_pool,
    materialize_recommendations,
    recommend_rebuild_handler,
)
from app.services.ai.tasks import (
    claim_tasks,
    complete_task,
    enqueue_task,
    get_task,
    start_task,
)

VIEWER_ID = 9_876_546_101
TARGET_ID = VIEWER_ID + 1
STRANGER_ID = VIEWER_ID + 2

_VECTOR = {
    "profile": 3,
    "preference": 2,
    "privacy": 1,
    "relationship": 0,
    "policy": 1,
}


async def _seed_user(
    db: AsyncSession, user_id: int, nickname: str, *, with_projections: bool
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:user_id, :nickname, 1, '1992-06-01', 1, 1)"
        ),
        {"user_id": user_id, "nickname": nickname},
    )
    await db.execute(
        text(
            "INSERT INTO user_profile (user_id, height, income, occupation, "
            "education_level, residence_city_code, interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 175, 15000, 'engineer', 4, '330100', :tags, '[]', :active)"
        ),
        {
            "user_id": user_id,
            "tags": json.dumps(["travel"], ensure_ascii=False),
            "active": now,
        },
    )
    await db.execute(
        text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
        {"user_id": user_id},
    )
    await db.execute(
        text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) "
            "VALUES (:user_id, 1, 1, 1)"
        ),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision) VALUES (:user_id, :profile, :preference, "
            ":privacy, :relationship, :policy)"
        ),
        {"user_id": user_id, **_VECTOR},
    )
    if not with_projections:
        return
    await db.execute(
        text(
            "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, 'profile_text_extract', 'profile-text-v1', "
            "'ai-policy-2026-08-07-v1', :granted_at)"
        ),
        {"user_id": user_id, "granted_at": now},
    )
    # Phase 4 P4-01: 测试隔离 — 先清后写，UNIQUE(user_id, kind) 跨用例幂等。
    await db.execute(
        text(
            "DELETE FROM ai_profile_projection_status WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    consent_snapshot = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    profile_fields = {
        "age": 31,
        "city_code": "330100",
        "marriage_status": "single",
        "education_level": 4,
        "height_cm": 175,
        "income_band": 15000,
        "interest_tags": ["travel", "hiking"],
        "relationship_goal": "marriage",
    }
    preference_fields = {
        "age": {"min": 25, "max": 40},
        "city_code": ["330100"],
        "marriage_status": "single",
        "education_level": {"min": 3},
        "height_cm": {"min": 160, "max": 185},
        "income_band": {"min": 8000},
        "interest_tags": ["travel"],
        "relationship_goal": "marriage",
    }
    for kind, fields in (
        ("personal_compatibility", profile_fields),
        ("ideal_partner_preference", preference_fields),
    ):
        await db.execute(
            text(
                "INSERT INTO ai_feature_projection "
                "(subject_user_id, projection_kind, source_hash, projection_version, fields_json, "
                "source_revision_json, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision, consent_snapshot_json, visibility_class, "
                "status, expires_at) "
                "VALUES (:user_id, :kind, :source_hash, 'proj-v1', :fields, :source_revision, "
                ":profile, :preference, :privacy, :relationship, :policy, :consent, "
                "'searchable', 'active', :expires_at)"
            ),
            {
                "user_id": user_id,
                "kind": kind,
                "source_hash": f"rec-{user_id}-{kind}",
                "fields": json.dumps(fields, ensure_ascii=False),
                "source_revision": json.dumps(_VECTOR),
                "profile": _VECTOR["profile"],
                "preference": _VECTOR["preference"],
                "privacy": _VECTOR["privacy"],
                "relationship": _VECTOR["relationship"],
                "policy": _VECTOR["policy"],
                "consent": json.dumps(consent_snapshot, ensure_ascii=False),
                "expires_at": now + timedelta(days=1),
            },
        )
        # Phase 4 P4-01: 投影准入位(测试 fixture 默认 active)
        await db.execute(
            text(
                "INSERT INTO ai_profile_projection_status "
                "(user_id, kind, status, source_revision, projection_id) "
                "VALUES (:user_id, :kind, 'active', 0, NULL)"
            ),
            {"user_id": user_id, "kind": kind},
        )


async def _seed_world(db: AsyncSession) -> None:
    await _seed_user(db, VIEWER_ID, "rec-viewer", with_projections=True)
    await _seed_user(db, TARGET_ID, "rec-target", with_projections=True)
    await _seed_user(db, STRANGER_ID, "rec-stranger", with_projections=False)
    await db.commit()


async def _rows(db: AsyncSession, view_kind: str, viewer_id: int = VIEWER_ID):
    result = await db.execute(
        text(
            "SELECT target_user_id, score, rank_no, generation, status, engine, source_hash "
            "FROM ai_recommendation_snapshot "
            "WHERE viewer_user_id = :viewer AND view_kind = :view_kind "
            "AND status = 'ready' "
            "ORDER BY rank_no"
        ),
        {"viewer": viewer_id, "view_kind": view_kind},
    )
    return result.mappings().all()


@pytest.mark.asyncio
async def test_real_materialize_persists_three_views_with_rank(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    snapshot_id = await materialize_recommendations(
        real_db_session, VIEWER_ID, trigger="integration"
    )
    assert snapshot_id.startswith("rc_")
    await real_db_session.commit()

    for view_kind in ("i_like", "likes_me", "similar"):
        rows = await _rows(real_db_session, view_kind)
        targets = {int(row["target_user_id"]) for row in rows}
        assert TARGET_ID in targets, f"{view_kind} 应含高匹配候选"
        assert STRANGER_ID not in targets, "无投影用户不得出现在任何列表"
        assert rows[0]["rank_no"] == 1
        assert rows[0]["status"] == "ready"
        assert rows[0]["engine"] == "rule-v1"
        assert float(rows[0]["score"]) > 0
        assert rows[0]["source_hash"] == f"rec-{VIEWER_ID}-personal_compatibility"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_materialize_generations_advance_and_supersede(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    await materialize_recommendations(real_db_session, VIEWER_ID, trigger="one")
    await materialize_recommendations(real_db_session, VIEWER_ID, trigger="two")
    await real_db_session.commit()
    rows = await _rows(real_db_session, "i_like")
    generations = {int(row["generation"]) for row in rows}
    assert generations == {2}, "读取面只见最新代；旧代必须全部被置 superseded"
    superseded = (
        await real_db_session.execute(
            text(
                "SELECT COUNT(*) FROM ai_recommendation_snapshot "
                "WHERE viewer_user_id = :viewer AND view_kind = 'i_like' "
                "AND generation = 1 AND status = 'superseded'"
            ),
            {"viewer": VIEWER_ID},
        )
    ).scalar_one()
    assert int(superseded) >= 1
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_pool_filters_invalid_consent_evidence(
    real_db_session: AsyncSession,
) -> None:
    """consent 证据 scope 不符的候选投影不进池（授权纪律，仿 _load_current_projection_rows）。"""
    await _seed_user(real_db_session, VIEWER_ID, "rec-viewer", with_projections=True)
    await _seed_user(real_db_session, TARGET_ID, "rec-target", with_projections=True)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await real_db_session.execute(
        text(
            "UPDATE ai_feature_projection SET consent_snapshot_json = :consent "
            "WHERE subject_user_id = :user_id"
        ),
        {
            "consent": json.dumps(
                {
                    "scope": "some_other_scope",
                    "version": "profile-text-v1",
                    "policy_revision": "ai-policy-2026-08-07-v1",
                    "granted_at": now.isoformat(),
                },
                ensure_ascii=False,
            ),
            "user_id": TARGET_ID,
        },
    )
    await real_db_session.commit()
    pool = await load_candidate_pool(real_db_session, VIEWER_ID, limit=50)
    assert TARGET_ID not in {item["user_id"] for item in pool}
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_handler_enqueues_gates_and_persists(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    task = await enqueue_task(
        db=real_db_session,
        owner_user_id=VIEWER_ID,
        task_type=RECOMMEND_TASK_TYPE,
        idempotency_key="rec-handler-it-1",
        request_hash="rec-handler-it-1",
    )
    await real_db_session.commit()
    record = await get_task(real_db_session, task.task_id)
    assert record is not None
    outcome = await recommend_rebuild_handler(real_db_session, record, "integration-worker")
    assert outcome is not None
    result_ref, revisions = outcome
    assert result_ref.startswith("recommend-snapshot:rc_")
    assert revisions.profile == _VECTOR["profile"]
    await real_db_session.commit()
    rows = await _rows(real_db_session, "i_like")
    assert rows, "handler 物化应落库"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_handler_fails_retryable_before_projection_lands(
    real_db_session: AsyncSession,
) -> None:
    """publish→projection→recommend 落库顺序未就绪时：投影新鲜度门禁挡下（可重试）。"""
    await _seed_world(real_db_session)
    # 投影仍携带旧版本向量：把 viewer 的 profile_revision 抬高一版模拟"发布未投影"。
    await real_db_session.execute(
        text(
            "UPDATE user_revision_state SET profile_revision = profile_revision + 1 "
            "WHERE user_id = :user_id"
        ),
        {"user_id": VIEWER_ID},
    )
    task = await enqueue_task(
        db=real_db_session,
        owner_user_id=VIEWER_ID,
        task_type=RECOMMEND_TASK_TYPE,
        idempotency_key="rec-handler-it-2",
        request_hash="rec-handler-it-2",
    )
    await real_db_session.commit()
    record = await get_task(real_db_session, task.task_id)
    outcome = await recommend_rebuild_handler(real_db_session, record, "integration-worker")
    assert outcome is None, "投影未就绪时 handler 不得物化"
    await real_db_session.rollback()


# ----------------------------------------------------------------------
# WP-P6d 触发接线：publish 入队 + 每日批量
# ----------------------------------------------------------------------

_PUBLISH_USER_ID = 9_876_547_501
_DAILY_USER_ID = 9_876_547_601


async def _recommend_task_count(db: AsyncSession, user_id: int) -> int:
    return int(
        (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM ai_task "
                    "WHERE owner_user_id = :user_id AND task_type = 'recommend_rebuild'"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_real_publish_enqueues_recommend_rebuild_idempotently(
    real_db_session: AsyncSession,
) -> None:
    from app.services.ai.profile import publish_profile_draft
    from tests.integration.ai.test_ai_profile_real_db import (
        _clean_user,
        _seed_publishable_draft,
    )

    # _seed_publishable_draft/_clean_user 绑定该模块的 USER_ID；改绑发布用户。
    from tests.integration.ai import test_ai_profile_real_db as publish_module

    publish_module.USER_ID = _PUBLISH_USER_ID
    try:
        await _clean_user(real_db_session)
        draft_id = "rec-publish-draft"
        await _seed_publishable_draft(
            real_db_session, _PUBLISH_USER_ID, draft_id, "rec-publish-turn"
        )
        await real_db_session.commit()
        submission = await publish_profile_draft(
            real_db_session,
            draft_id,
            _PUBLISH_USER_ID,
            expected_revision=0,
            idempotency_key="rec-publish-key-1",
        )
        assert submission.revision is not None
        await real_db_session.commit()
        assert await _recommend_task_count(real_db_session, _PUBLISH_USER_ID) == 1
        key = (
            await real_db_session.execute(
                text(
                    "SELECT idempotency_key FROM ai_task "
                    "WHERE owner_user_id = :user_id AND task_type = 'recommend_rebuild'"
                ),
                {"user_id": _PUBLISH_USER_ID},
            )
        ).scalar_one()
        assert key == "rec-publish-key-1-recommend"

        # 同 Idempotency-Key 重放 publish：不产生第二个 recommend 任务。
        await publish_profile_draft(
            real_db_session,
            draft_id,
            _PUBLISH_USER_ID,
            expected_revision=0,
            idempotency_key="rec-publish-key-1",
        )
        await real_db_session.commit()
        assert await _recommend_task_count(real_db_session, _PUBLISH_USER_ID) == 1
    finally:
        publish_module.USER_ID = 9_876_543_211
        await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_daily_batch_enqueues_idempotently(
    real_db_session: AsyncSession,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from scripts.recommend_daily_batch import enqueue_daily_batch

    await _seed_user(
        real_db_session, _DAILY_USER_ID, "rec-daily", with_projections=True
    )
    await real_db_session.commit()
    # 注入测试库工厂：脚本默认用全局 session_factory（指向 .env 的业务库）。
    test_factory = async_sessionmaker(
        real_db_session.bind, expire_on_commit=False
    )
    enqueued = await enqueue_daily_batch(50, session_factory=test_factory)
    assert enqueued >= 1
    assert await _recommend_task_count(real_db_session, _DAILY_USER_ID) == 1
    # 同日重跑：幂等键 recommend-daily-{user}-{UTC日期} 回放，不重复入队。
    await enqueue_daily_batch(50, session_factory=test_factory)
    assert await _recommend_task_count(real_db_session, _DAILY_USER_ID) == 1
    await real_db_session.rollback()


# ----------------------------------------------------------------------
# WP-P6e 读取服务：read_recommendations + miss 触发重建
# ----------------------------------------------------------------------


from app.services.ai.recommend import (  # noqa: E402
    enqueue_recommendation_rebuild,
    read_recommendations,
)


@pytest.mark.asyncio
async def test_real_read_recommendations_orders_and_filters(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    await materialize_recommendations(real_db_session, VIEWER_ID, trigger="read")
    # 制造一条已过期行：过期行不得出现在读取面。
    await real_db_session.execute(
        text(
            "UPDATE ai_recommendation_snapshot SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE "
            "WHERE viewer_user_id = :viewer AND view_kind = 'i_like' AND rank_no = 1"
        ),
        {"viewer": VIEWER_ID},
    )
    await real_db_session.commit()
    items = await read_recommendations(real_db_session, VIEWER_ID, "i_like", 20)
    ranks = [item["rank_no"] for item in items]
    assert ranks == sorted(ranks)
    assert all(item["target_user_id"] != STRANGER_ID for item in items)
    # rank1 已过期：读取面不再包含它（仅剩过期前 rank>=2 的行）。
    assert len(items) <= 1
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_get_miss_rebuild_survives_completion_gate(
    real_db_session: AsyncSession,
) -> None:
    """P1 回归：GET-miss 入队必须携带 revisions，否则 complete_task 的复核门禁
    判 superseded 并回滚全部物化行——GET-miss/每日批量重建永不落库。"""
    await _seed_world(real_db_session)
    # 走与路由一致的入队路径（enqueue_recommendation_rebuild 内部带向量）。
    task = await enqueue_recommendation_rebuild(real_db_session, VIEWER_ID)
    assert task is not None
    await real_db_session.commit()
    # 仿真实 worker 时序：claim(queued→leased) → start(leased→running) →
    # handler → complete（门禁复核围栏要求完整租约链）。
    from datetime import UTC, datetime as _dt

    claimed = await claim_tasks(
        real_db_session, "integration-worker", _dt.now(UTC), 10
    )
    assert [t.task_id for t in claimed] == [task.task_id]
    record = await get_task(real_db_session, task.task_id)
    await start_task(real_db_session, task.task_id, "integration-worker")
    outcome = await recommend_rebuild_handler(real_db_session, record, "integration-worker")
    assert outcome is not None
    result_ref, revisions = outcome
    # 关键差异：经由 complete_task 的门禁复核（而非直接 commit）。
    await complete_task(
        real_db_session, task.task_id, "integration-worker", result_ref, revisions
    )
    await real_db_session.commit()
    status_row = (
        await real_db_session.execute(
            text("SELECT status FROM ai_task WHERE task_id = :task_id"),
            {"task_id": task.task_id},
        )
    ).scalar_one()
    assert status_row == "succeeded"
    rows = await _rows(real_db_session, "i_like")
    assert rows, "完成门禁复核后物化结果必须幸存（不得被 supersede 回滚）"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_projection_currency_ignores_privacy_revision_bump(
    real_db_session: AsyncSession,
) -> None:
    """P1 回归：隐私/关系分量推进（拉黑、授权变更）不构成"投影过期"——
    投影内容只由 profile/preference 派生；旧实现会永久假阴性并让推荐静默死亡。"""
    await _seed_world(real_db_session)
    from app.services.ai.recommend import viewer_projection_is_current

    assert await viewer_projection_is_current(real_db_session, VIEWER_ID) is True
    await real_db_session.execute(
        text(
            "UPDATE user_revision_state SET privacy_revision = privacy_revision + 1, "
            "relationship_revision = relationship_revision + 1 WHERE user_id = :user_id"
        ),
        {"user_id": VIEWER_ID},
    )
    await real_db_session.commit()
    assert await viewer_projection_is_current(real_db_session, VIEWER_ID) is True
    # 内容分量推进（republish）仍然必须判过期：等投影任务落库前的窗口。
    await real_db_session.execute(
        text(
            "UPDATE user_revision_state SET profile_revision = profile_revision + 1 "
            "WHERE user_id = :user_id"
        ),
        {"user_id": VIEWER_ID},
    )
    await real_db_session.commit()
    assert await viewer_projection_is_current(real_db_session, VIEWER_ID) is False
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_handler_empty_pool_completes_honestly(
    real_db_session: AsyncSession,
) -> None:
    """P2 回归：门禁全过但候选池为空 → 任务以 recommend-empty 诚实完成，
    不产生幽灵 snapshot_id，也不以失败收场。"""
    await _seed_world(real_db_session)
    # 清空其他用户的投影：候选池为空，viewer 自身投影仍在（过门禁）。
    await real_db_session.execute(
        text("DELETE FROM ai_feature_projection WHERE subject_user_id <> :viewer"),
        {"viewer": VIEWER_ID},
    )
    await real_db_session.commit()
    task = await enqueue_task(
        db=real_db_session,
        owner_user_id=VIEWER_ID,
        task_type=RECOMMEND_TASK_TYPE,
        idempotency_key="rec-empty-it-1",
        request_hash="rec-empty-it-1",
    )
    await real_db_session.commit()
    record = await get_task(real_db_session, task.task_id)
    outcome = await recommend_rebuild_handler(real_db_session, record, "integration-worker")
    assert outcome is not None
    assert outcome[0] == "recommend-empty"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_read_miss_enqueues_view_rebuild_idempotently(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    accepted = await enqueue_recommendation_rebuild(real_db_session, VIEWER_ID)
    assert accepted is not None
    await real_db_session.commit()
    assert await _recommend_task_count(real_db_session, VIEWER_ID) == 1
    # 同日重复触发：幂等回放。
    await enqueue_recommendation_rebuild(real_db_session, VIEWER_ID)
    assert await _recommend_task_count(real_db_session, VIEWER_ID) == 1
    await real_db_session.rollback()


# ----------------------------------------------------------------------
# T10 平滑切换：i_like/likes_me 消费 llm 双向分
# ----------------------------------------------------------------------

from app.services.ai.compatibility import (  # noqa: E402
    BRAND_LABEL,
    CompatibilityResult,
    RevisionVector,
    write_shadow_snapshot,
)


def _llm_consent() -> dict:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    snapshot = {
        "scope": "compatibility_shadow",
        "version": "compatibility-shadow-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    return {"viewer": snapshot, "target": snapshot}


def _revisions() -> tuple[RevisionVector, RevisionVector]:
    vector = {"profile": 3, "preference": 2, "privacy": 1, "relationship": 0, "policy": 1}
    return (
        RevisionVector(**vector),
        RevisionVector(**{**vector, "profile": 5}),
    )


async def _write_llm_snapshot(
    db: AsyncSession, *, viewer_id: int, target_id: int, expired: bool = False
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    expires = now - timedelta(minutes=1) if expired else now + timedelta(days=7)
    result = CompatibilityResult.ready(
        pair_score=70.17,
        directions=(72.0, 68.0),
        coverage=0.75,
        reason_codes=(),
    )
    await write_shadow_snapshot(
        db,
        viewer_id,
        target_id,
        result,
        _revisions(),
        _llm_consent(),
        engine="llm-v1",
        brand_label=BRAND_LABEL,
        score_semantics="llm_pairwise_probability",
        ttl_minutes=7 * 24 * 60,
        direction_payload={
            "viewer_to_target": {"score": 72, "reasons": ["价值观一致", "年龄互配", "兴趣重叠"]},
            "target_to_viewer": {"score": 68, "reasons": ["目标一致", "城市相同", "作息相近"]},
        },
    )
    # write_shadow_snapshot 的 TTL 形参用于规则配置；llm 行过期时间按需覆盖。
    await db.execute(
        text(
            "UPDATE ai_compatibility_snapshot SET expires_at = :expires "
            "WHERE viewer_user_id = :viewer AND target_user_id = :target "
            "AND engine = 'llm-v1'"
        ),
        {"expires": expires, "viewer": viewer_id, "target": target_id},
    )


@pytest.mark.asyncio
async def test_real_i_like_likes_me_consume_fresh_llm_directions(
    real_db_session: AsyncSession,
) -> None:
    await _seed_world(real_db_session)
    await _write_llm_snapshot(
        real_db_session, viewer_id=VIEWER_ID, target_id=TARGET_ID
    )
    await real_db_session.commit()
    await materialize_recommendations(real_db_session, VIEWER_ID, trigger="llm")
    await real_db_session.commit()

    i_like = {int(r["target_user_id"]): r for r in await _rows(real_db_session, "i_like")}
    likes_me = {int(r["target_user_id"]): r for r in await _rows(real_db_session, "likes_me")}
    similar = {int(r["target_user_id"]): r for r in await _rows(real_db_session, "similar")}
    assert i_like[TARGET_ID]["engine"] == "llm-v1"
    assert float(i_like[TARGET_ID]["score"]) == 72.0
    assert likes_me[TARGET_ID]["engine"] == "llm-v1"
    assert float(likes_me[TARGET_ID]["score"]) == 68.0
    # similar 不受 llm 快照影响（纯规则）。
    assert similar[TARGET_ID]["engine"] == "rule-v1"

    items = await read_recommendations(real_db_session, VIEWER_ID, "i_like", 20)
    target_item = next(i for i in items if i["target_user_id"] == TARGET_ID)
    assert target_item["reason_texts"] == ["价值观一致", "年龄互配", "兴趣重叠"]

    # llm 快照过期 → 重物化回退规则单向打分（engine 回 rule-v1）。
    await real_db_session.execute(
        text(
            "UPDATE ai_compatibility_snapshot SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE "
            "WHERE viewer_user_id = :viewer AND engine = 'llm-v1'"
        ),
        {"viewer": VIEWER_ID},
    )
    await real_db_session.commit()
    await materialize_recommendations(real_db_session, VIEWER_ID, trigger="fallback")
    await real_db_session.commit()
    rows = {int(r["target_user_id"]): r for r in await _rows(real_db_session, "i_like")}
    assert rows[TARGET_ID]["engine"] == "rule-v1"
    assert float(rows[TARGET_ID]["score"]) > 0
    await real_db_session.rollback()
