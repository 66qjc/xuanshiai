"""WP-C1c compatibility_llm 任务真实库验收：GET 触发 → llm 精算 → 降级。

覆盖：任务入队门禁（可见性/双授权/同日幂等）、粗排 coverage 不足不调 LLM、
llm 成功写快照（engine='llm-v1' + brand_label + 双向分/理由）、llm 失败降级
写规则快照并标注 LLM_FALLBACK_RULE、读取端永远有可用结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_compatibility import CompatibilitySnapshotStatus
from app.services.ai import compatibility as compat_mod
from app.services.ai.base import (
    CompatibilityCompareRequest,
    CompatibilityCompareResult,
    CompatibilityCompareDirection,
)
from app.services.ai.compatibility import (
    BRAND_LABEL,
    compatibility_llm_execute_handler,
    load_compatibility_prompt_inputs,
    read_compatibility_snapshot,
    request_compatibility_llm_refresh,
)
from app.services.ai.tasks import get_task

VIEWER_ID = 9_876_548_201
TARGET_ID = VIEWER_ID + 1

_VECTOR_VIEWER = {"profile": 3, "preference": 2, "privacy": 1, "relationship": 0, "policy": 1}
_VECTOR_TARGET = {"profile": 5, "preference": 4, "privacy": 2, "relationship": 0, "policy": 1}


async def _clean(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id IN (:v, :t) OR target_user_id IN (:v, :t)",
        "DELETE FROM ai_recommendation_snapshot WHERE viewer_user_id IN (:v, :t) OR target_user_id IN (:v, :t)",
        "DELETE FROM ai_task WHERE owner_user_id IN (:v, :t)",
        "DELETE FROM ai_feature_projection WHERE subject_user_id IN (:v, :t)",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:v, :t)",
        "DELETE FROM user_revision_state WHERE user_id IN (:v, :t)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:v, :t)",
        "DELETE FROM user_privacy WHERE user_id IN (:v, :t)",
        "DELETE FROM user_auth WHERE user_id IN (:v, :t)",
        "DELETE FROM user_profile WHERE user_id IN (:v, :t)",
        "DELETE FROM users WHERE id IN (:v, :t)",
    ):
        await db.execute(statement=text(statement), params={"v": VIEWER_ID, "t": TARGET_ID})
    await db.commit()


async def _seed_pair(db: AsyncSession, *, target_consent: bool = True) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:viewer, 'llm-viewer', 1, '1990-01-01', 1, 1), "
            "(:target, 'llm-target', 2, '1992-01-01', 1, 1)"
        ),
        {"viewer": VIEWER_ID, "target": TARGET_ID},
    )
    for user_id, vector in ((VIEWER_ID, _VECTOR_VIEWER), (TARGET_ID, _VECTOR_TARGET)):
        await db.execute(
            text(
                "INSERT INTO user_profile (user_id, height, income, occupation, "
                "education_level, residence_city_code, interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, 'engineer', 4, '330100', :tags, '[]', :active)"
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
            {"user_id": user_id, **vector},
        )
        grants = [
            {
                "user_id": user_id,
                "scope": "profile_text_extract",
                "version": "profile-text-v1",
                "policy": "ai-policy-2026-08-07-v1",
                "granted_at": now,
            },
            {
                "user_id": user_id,
                "scope": "compatibility_shadow",
                "version": "compatibility-shadow-v1",
                "policy": "ai-policy-2026-08-07-v1",
                "granted_at": now,
            },
        ]
        if not target_consent and user_id == TARGET_ID:
            grants = grants[:1]
        await db.execute(
            text(
                "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
                "VALUES (:user_id, :scope, :version, :policy, :granted_at)"
            ),
            grants,
        )

    profile_fields = {
        "age": 30,
        "city_code": "330100",
        "marriage_status": "single",
        "education_level": 4,
        "height_cm": 172,
        "income_band": 12000,
        "interest_tags": ["travel"],
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
    for user_id, vector in ((VIEWER_ID, _VECTOR_VIEWER), (TARGET_ID, _VECTOR_TARGET)):
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
                    "source_hash": f"llm-{user_id}-{kind}",
                    "fields": json.dumps(fields, ensure_ascii=False),
                    "source_revision": json.dumps(vector),
                    "profile": vector["profile"],
                    "preference": vector["preference"],
                    "privacy": vector["privacy"],
                    "relationship": vector["relationship"],
                    "policy": vector["policy"],
                    "consent": json.dumps(
                        {
                            "scope": "profile_text_extract",
                            "version": "profile-text-v1",
                            "policy_revision": "ai-policy-2026-08-07-v1",
                            "granted_at": now.isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                    "expires_at": now + timedelta(days=1),
                },
            )
    await db.commit()


def _fake_gateway(monkeypatch, *, fail: bool = False, called: dict | None = None):
    """给 compatibility 模块注入 fake 网关工厂；返回 outcome 由闭包构造。"""
    from app.services.ai.gateway import InvokeOutcome

    class _FakeGateway:
        async def compare_compatibility(self, context, request):
            if called is not None:
                called["count"] = called.get("count", 0) + 1
                called["request"] = request
            if fail:
                return InvokeOutcome(
                    error_code="AI_TEMPORARILY_UNAVAILABLE",
                    error_message="provider unavailable",
                    retryable=True,
                )
            return InvokeOutcome(
                result=CompatibilityCompareResult(
                    viewer_to_target=CompatibilityCompareDirection(
                        score=72,
                        reasons=("价值观一致", "年龄互配", "兴趣重叠"),
                    ),
                    target_to_viewer=CompatibilityCompareDirection(
                        score=68,
                        reasons=("目标一致", "城市相同", "作息相近"),
                    ),
                )
            )

    monkeypatch.setattr(compat_mod, "AIGateway", lambda **kwargs: _FakeGateway())


@pytest.mark.asyncio
async def test_real_llm_refresh_triggers_and_snapshot_written(
    real_db_session: AsyncSession, monkeypatch
) -> None:
    await _clean(real_db_session)
    await _seed_pair(real_db_session)
    called: dict = {}
    _fake_gateway(monkeypatch, called=called)

    accepted = await request_compatibility_llm_refresh(
        real_db_session, VIEWER_ID, TARGET_ID
    )
    assert accepted is not None
    await real_db_session.commit()
    task = await get_task(real_db_session, accepted.task_id)
    assert task is not None

    outcome = await compatibility_llm_execute_handler(
        real_db_session, task, "integration-worker"
    )
    assert outcome is not None
    assert outcome[0].startswith("compatibility-snapshot:")
    await real_db_session.commit()

    snapshot = await read_compatibility_snapshot(real_db_session, VIEWER_ID, TARGET_ID)
    assert snapshot.status is CompatibilitySnapshotStatus.READY
    assert snapshot.engine == "llm-v1"
    assert snapshot.brand_label == BRAND_LABEL
    assert snapshot.compatibility_index == pytest.approx(
        2 * 72 * 68 / (72 + 68), abs=0.01
    )
    assert snapshot.directions is not None
    assert snapshot.directions.viewer_to_target == 72.0
    assert snapshot.directions.target_to_viewer == 68.0
    assert snapshot.reason_texts.get("viewer_to_target") == ["价值观一致", "年龄互配", "兴趣重叠"]
    assert called["count"] == 1
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_llm_failure_degrades_to_rule_with_marker(
    real_db_session: AsyncSession, monkeypatch
) -> None:
    await _clean(real_db_session)
    await _seed_pair(real_db_session)
    _fake_gateway(monkeypatch, fail=True)

    accepted = await request_compatibility_llm_refresh(
        real_db_session, VIEWER_ID, TARGET_ID
    )
    await real_db_session.commit()
    task = await get_task(real_db_session, accepted.task_id)
    outcome = await compatibility_llm_execute_handler(
        real_db_session, task, "integration-worker"
    )
    assert outcome is not None, "降级路径任务仍应完成（读取端永远有可用结果）"
    await real_db_session.commit()

    snapshot = await read_compatibility_snapshot(real_db_session, VIEWER_ID, TARGET_ID)
    assert snapshot.status is CompatibilitySnapshotStatus.READY
    assert snapshot.engine == "rule-v1"
    assert snapshot.brand_label is None
    assert "LLM_FALLBACK_RULE" in snapshot.reason_codes
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_llm_skipped_when_coverage_insufficient(
    real_db_session: AsyncSession, monkeypatch
) -> None:
    """粗排 coverage 不足（无投影）→ 不调 LLM，直接写规则 blocked 快照收尾。"""
    await _clean(real_db_session)
    await _seed_pair(real_db_session)
    # 抹掉双方投影 → load_compatibility_features 得空 FeatureSet → coverage 0
    await real_db_session.execute(
        text("DELETE FROM ai_feature_projection WHERE subject_user_id IN (:v, :t)"),
        {"v": VIEWER_ID, "t": TARGET_ID},
    )
    await real_db_session.commit()
    called: dict = {}
    _fake_gateway(monkeypatch, called=called)

    accepted = await request_compatibility_llm_refresh(
        real_db_session, VIEWER_ID, TARGET_ID
    )
    assert accepted is not None, "入队不因 coverage 不足被拦（成本守门在 handler 内）"
    await real_db_session.commit()
    task = await get_task(real_db_session, accepted.task_id)
    outcome = await compatibility_llm_execute_handler(
        real_db_session, task, "integration-worker"
    )
    assert outcome is not None
    await real_db_session.commit()
    assert called.get("count", 0) == 0, "coverage 不足绝不触发 LLM（成本守门）"
    snapshot = await read_compatibility_snapshot(real_db_session, VIEWER_ID, TARGET_ID)
    # 投影缺失时读取端既有语义是 blocked（覆盖不足快照落库为 coverage_insufficient，
    # 读取端因投影 key 缺失统一映射 blocked）——两种非 ready 状态都算"未伪造分数"。
    assert snapshot.status is not CompatibilitySnapshotStatus.READY
    assert snapshot.engine == "rule-v1"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_llm_refresh_idempotent_per_day(real_db_session: AsyncSession) -> None:
    await _clean(real_db_session)
    await _seed_pair(real_db_session)
    first = await request_compatibility_llm_refresh(real_db_session, VIEWER_ID, TARGET_ID)
    await real_db_session.commit()
    assert first is not None
    second = await request_compatibility_llm_refresh(real_db_session, VIEWER_ID, TARGET_ID)
    assert second is not None and second.task_id == first.task_id
    count = (
        await real_db_session.execute(
            text(
                "SELECT COUNT(*) FROM ai_task WHERE owner_user_id = :v "
                "AND task_type = 'compatibility_llm'"
            ),
            {"v": VIEWER_ID},
        )
    ).scalar_one()
    assert int(count) == 1
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_llm_refresh_requires_dual_consent(real_db_session: AsyncSession) -> None:
    await _clean(real_db_session)
    await _seed_pair(real_db_session, target_consent=False)
    accepted = await request_compatibility_llm_refresh(
        real_db_session, VIEWER_ID, TARGET_ID
    )
    assert accepted is None, "对方未授权 compatibility_shadow → 不触发精算"
    await real_db_session.rollback()


@pytest.mark.asyncio
async def test_real_prompt_inputs_carry_digest(real_db_session: AsyncSession) -> None:
    """WP-C1 输入契约：阶段2 的 entry_digest 必须随投影进入精算 prompt。

    回归锚点：_load_projection_rows 的 SELECT 曾漏掉 entry_digest 列，摘要
    永远为 None（旧断言把缺陷固化成了"预期行为"）。
    """
    await _clean(real_db_session)
    await _seed_pair(real_db_session)
    digest_text = "价值观：欣赏真诚善良的人；作息：早睡早起"
    await real_db_session.execute(
        text(
            "UPDATE ai_feature_projection SET entry_digest = :digest "
            "WHERE subject_user_id = :user_id AND projection_kind = 'personal_compatibility'"
        ),
        {"digest": digest_text, "user_id": VIEWER_ID},
    )
    await real_db_session.commit()
    request = await load_compatibility_prompt_inputs(
        real_db_session, VIEWER_ID, TARGET_ID
    )
    assert request is not None
    assert isinstance(request, CompatibilityCompareRequest)
    assert "age" in request.viewer_personal
    assert request.viewer_personal_digest == digest_text
    assert request.target_personal_digest is None
    await real_db_session.rollback()
