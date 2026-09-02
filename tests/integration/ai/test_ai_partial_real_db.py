"""WP-S2 中途模糊候选真实库集成测试。

覆盖：hard 条件查询在 filtering 收尾物化 generation=0 初筛集（上限 50、
matched 仅统计 hard、脱敏口径不变）→ partial 阶段读取端 is_fuzzy=true；
完整集物化后切换 full、is_fuzzy=false 且 partial 行被清理；纯 soft 查询不
物化 partial；partial 物化失败不中断主流程。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.search import (
    _evidence_for_row,
    _load_condition_rows,
    _load_projections,
    _materialize_partial_results,
    compile_search_conditions,
    confirm_search_draft,
    materialize_search_snapshot,
    read_materialized_search_results,
)

OWNER_ID = 9_876_543_231
CANDIDATE_A = OWNER_ID + 1
CANDIDATE_B = OWNER_ID + 2


async def _clean(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id = :owner)",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id = :owner)",
        "DELETE FROM ai_search_snapshot WHERE user_id = :owner",
        "DELETE FROM ai_search_draft WHERE user_id = :owner",
        "DELETE FROM ai_task WHERE owner_user_id = :owner",
        "DELETE FROM ai_feature_projection WHERE subject_user_id IN (:candidate, :candidate_b)",
        "DELETE FROM ai_profile_projection_status WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM user_revision_state WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM user_privacy WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM user_auth WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM user_profile WHERE user_id IN (:owner, :candidate, :candidate_b)",
        "DELETE FROM users WHERE id IN (:owner, :candidate, :candidate_b)",
    ):
        await db.execute(
            text(statement),
            {"owner": OWNER_ID, "candidate": CANDIDATE_A, "candidate_b": CANDIDATE_B},
        )
    await db.commit()


async def _seed(db: AsyncSession, *, hard_condition: bool) -> str:
    """Seed owner + 2 候选 + 投影 + 草稿；返回 draft_id。"""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    owner_consent = {
        "scope": "search_parse",
        "version": "search-parse-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    candidate_consent = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    vector = {"profile": 1, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0}
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:id, :nickname, :gender, '1990-01-01', 1, 1)"
        ),
        [
            {"id": OWNER_ID, "nickname": "partial-owner", "gender": 1},
            {"id": CANDIDATE_A, "nickname": "partial-a", "gender": 2},
            {"id": CANDIDATE_B, "nickname": "partial-b", "gender": 2},
        ],
    )
    for user_id, tags in (
        (OWNER_ID, ["户外"]),
        (CANDIDATE_A, ["户外"]),
        (CANDIDATE_B, ["阅读"]),
    ):
        await db.execute(
            text(
                "INSERT INTO user_profile "
                "(user_id, height, income, occupation, education_level, residence_city_code, "
                "interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
            ),
            {
                "user_id": user_id,
                "tags": json.dumps(tags, ensure_ascii=False),
                "active": now,
            },
        )
        await db.execute(
            text(
                "INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"
            ),
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
                "relationship_revision, policy_revision) VALUES (:user_id, 1, 0, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
        if user_id == OWNER_ID:
            continue
        await db.execute(
            text(
                "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
                "VALUES (:user_id, :scope, :version, :policy, :granted_at)"
            ),
            {
                "user_id": user_id,
                "scope": candidate_consent["scope"],
                "version": candidate_consent["version"],
                "policy": candidate_consent["policy_revision"],
                "granted_at": now,
            },
        )
        await db.execute(
            text(
                "INSERT INTO ai_feature_projection "
                "(subject_user_id, projection_kind, source_hash, projection_version, fields_json, "
                "source_revision_json, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision, consent_snapshot_json, visibility_class, "
                "status, expires_at) VALUES (:user_id, 'personal_searchable', :source_hash, "
                "'profile-extract-v1', :fields, :source_revision, 1, 0, 0, 0, 0, :consent, "
                "'searchable', 'active', :expires_at)"
            ),
            {
                "user_id": user_id,
                "source_hash": f"partial-hash-{user_id}",
                "fields": json.dumps({"interest_tags": tags}, ensure_ascii=False),
                "source_revision": json.dumps(vector),
                "consent": json.dumps(candidate_consent),
                "expires_at": now + timedelta(days=1),
            },
        )
        # Phase 4 P4-01: 投影准入位
        await db.execute(
            text(
                "DELETE FROM ai_profile_projection_status "
                "WHERE user_id = :user_id AND kind = 'personal_searchable'"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO ai_profile_projection_status "
                "(user_id, kind, status, source_revision) "
                "VALUES (:user_id, 'personal_searchable', 'active', 0)"
            ),
            {"user_id": user_id},
        )
    await db.execute(
        text(
            "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, :scope, :version, :policy, :granted_at)"
        ),
        {
            "user_id": OWNER_ID,
            "scope": owner_consent["scope"],
            "version": owner_consent["version"],
            "policy": owner_consent["policy_revision"],
            "granted_at": now,
        },
    )
    draft_id = f"partial-draft-{int(hard_condition)}"
    await db.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, status, condition_revision, policy_revision, consent_snapshot_json) "
            "VALUES (:draft_id, :user_id, '杭州 户外', 'awaiting_confirmation', 0, :policy, :consent)"
        ),
        {
            "draft_id": draft_id,
            "user_id": OWNER_ID,
            "policy": owner_consent["policy_revision"],
            "consent": json.dumps(owner_consent),
        },
    )
    conditions = [
        ("city_code", "eq", json.dumps("330100"), "hard"),
    ]
    if not hard_condition:
        conditions = [
            ("interest_tags", "contains", json.dumps("户外", ensure_ascii=False), "soft"),
        ]
    else:
        conditions.append(
            ("interest_tags", "contains", json.dumps("户外", ensure_ascii=False), "soft")
        )
    for no, (field_key, operator, value, kind) in enumerate(conditions):
        await db.execute(
            text(
                "INSERT INTO ai_search_condition "
                "(draft_id, condition_revision, condition_no, field_key, operator, value_json, "
                "condition_kind, confidence, user_action) VALUES (:draft_id, 0, :no, "
                ":field_key, :operator, :value, :kind, 1, 'confirmed')"
            ),
            {
                "draft_id": draft_id,
                "no": no,
                "field_key": field_key,
                "operator": operator,
                "value": value,
                "kind": kind,
            },
        )
    return draft_id


@pytest.mark.asyncio
async def test_real_partial_visible_lifecycle(real_db_session: AsyncSession) -> None:
    await _clean(real_db_session)
    draft_id = await _seed(real_db_session, hard_condition=True)

    snapshot = await confirm_search_draft(
        real_db_session,
        draft_id,
        OWNER_ID,
        expected_condition_revision=0,
        idempotency_key="partial-confirm-1",
    )
    await real_db_session.commit()

    # 完整集未就绪时读取：空结果，partial_visible 未进入 partial。
    early = await read_materialized_search_results(
        real_db_session, snapshot.snapshot_id, OWNER_ID, None, 20
    )
    assert early.items == []

    # 模拟 filtering 收尾（进度 30%）：直接物化初筛集。
    condition_rows = await _load_condition_rows(real_db_session, draft_id)
    condition_objects = [
        row if hasattr(row, "user_action") else row
        for row in condition_rows
    ]
    # _load_condition_rows 返回 dict 行，转成条件对象。
    from app.services.ai.search import _condition_from_row

    condition_objects = [_condition_from_row(row) for row in condition_rows]
    compiled = compile_search_conditions(condition_objects)
    assert not compiled.conflicts
    projections = await _load_projections(real_db_session, [CANDIDATE_A, CANDIDATE_B])
    visible = []
    for baseline_index, candidate_id in enumerate((CANDIDATE_A, CANDIDATE_B)):
        row = {"user_id": candidate_id}
        evidence = _evidence_for_row(
            row, condition_objects, compiled, projections.get(candidate_id)
        )
        visible.append((baseline_index, row, evidence))
    from datetime import timedelta as _td

    expires = datetime.now(UTC).replace(tzinfo=None) + _td(minutes=10)
    partial_count = await _materialize_partial_results(
        real_db_session, snapshot.snapshot_id, visible, expires
    )
    await real_db_session.commit()
    assert partial_count == 2

    # partial 阶段读取：is_fuzzy=true、matched 仅统计 hard 条件。
    partial_page = await read_materialized_search_results(
        real_db_session, snapshot.snapshot_id, OWNER_ID, None, 20
    )
    assert partial_page.status not in {"stale"}
    assert {item.user_id for item in partial_page.items} == {CANDIDATE_A, CANDIDATE_B}
    assert all(item.is_fuzzy for item in partial_page.items)
    a_item = next(i for i in partial_page.items if i.user_id == CANDIDATE_B)
    # B 的 soft 条件（户外）不命中：模糊集仍包含（仅 hard 判定），但 matched
    # 计数只统计 hard——脱敏与口径和完整集一致。
    assert a_item.matched_conditions == ["city_code"]
    assert a_item.reason_codes == ["HARD_CONDITION_MATCH"]

    # 完整集物化：partial_visible='full'，partial 行被清理，is_fuzzy=false。
    materialized = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    assert materialized.items
    full_page = await read_materialized_search_results(
        real_db_session, snapshot.snapshot_id, OWNER_ID, None, 20
    )
    assert all(not item.is_fuzzy for item in full_page.items)
    status_row = (
        await real_db_session.execute(
            text(
                "SELECT partial_visible FROM ai_search_snapshot "
                "WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).scalar_one()
    assert status_row == "full"
    gen0 = (
        await real_db_session.execute(
            text(
                "SELECT COUNT(*) FROM ai_search_result "
                "WHERE snapshot_id = :snapshot_id AND generation = 0"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).scalar_one()
    assert gen0 == 0

    # 共享测试库纪律：清场（confirm 入队的 search_execute 任务属活跃任务，
    # 不清会污染迁移 down 守卫与后续运行的 claim 断言）。
    await _clean(real_db_session)


@pytest.mark.asyncio
async def test_real_partial_skipped_for_soft_only_and_failure_isolated(
    real_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.ai import search as search_module

    # 纯 soft 查询：不物化 partial。
    await _clean(real_db_session)
    draft_id = await _seed(real_db_session, hard_condition=False)
    snapshot = await confirm_search_draft(
        real_db_session,
        draft_id,
        OWNER_ID,
        expected_condition_revision=0,
        idempotency_key="partial-confirm-2",
    )
    await real_db_session.commit()
    materialized = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    assert materialized.items
    status_row = (
        await real_db_session.execute(
            text(
                "SELECT partial_visible FROM ai_search_snapshot "
                "WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).scalar_one()
    assert status_row == "full"  # 无 hard 条件：从不进入 partial（保持 none→full）

    # partial 物化失败注入：主流程不受影响。
    await _clean(real_db_session)
    draft_id = await _seed(real_db_session, hard_condition=True)
    snapshot = await confirm_search_draft(
        real_db_session,
        draft_id,
        OWNER_ID,
        expected_condition_revision=0,
        idempotency_key="partial-confirm-3",
    )
    await real_db_session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("partial exploded")

    monkeypatch.setattr(search_module, "_materialize_partial_results", _boom)
    materialized = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    assert materialized.items  # 主流程成功
    status_row = (
        await real_db_session.execute(
            text(
                "SELECT partial_visible FROM ai_search_snapshot "
                "WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).scalar_one()
    assert status_row == "full"

    # 清场。
    await _clean(real_db_session)
