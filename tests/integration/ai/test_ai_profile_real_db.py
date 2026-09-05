"""Real MySQL acceptance for the M04 publish -> revision -> projection chain."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.profile import (
    AIInputError,
    confirm_profile_narrative,
    hash_narrative_request,
    load_published_narrative,
    profile_projection_handler,
    publish_profile_draft,
    request_narrative_regenerate,
)
from app.services.ai.tasks import TaskError, enqueue_task, get_task

USER_ID = 9_876_543_211
NARRATIVE_USER = 9_988_700_001


async def _clean_user(db: AsyncSession) -> None:
    statements = (
        "DELETE FROM ai_profile_projection_status WHERE user_id = :user_id",
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN "
        "(SELECT draft_id FROM ai_profile_draft WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_draft WHERE user_id = :user_id",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN "
        "(SELECT id FROM ai_profile_revision WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_revision WHERE user_id = :user_id",
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
    )
    for statement in statements:
        await db.execute(text(statement), {"user_id": USER_ID})
    await db.commit()


_PUBLISH_DRAFT_FIELDS = [
    # 7 个确认字段对齐 WP-P2 发布门槛（默认 7/10 ≈ 67%）；interest_tags
    # 保留 turn/span 供 revision 溯源断言。
    ("interest_tags", ["旅行"], "旅行", "周末喜欢旅行"),
    ("city_code", "330100", "杭州", "现居杭州"),
    ("occupation_group", "technology", "互联网", "从事互联网行业"),
    ("education_level", 4, "本科", "本科学历"),
    ("height_cm", 175, "175cm", "身高175"),
    ("income_band", "high", "较高", "收入稳定"),
    ("marriage_status", "single", "未婚", "未婚"),
]


async def _seed_publishable_draft(
    db: AsyncSession, user_id: int, draft_id: str, turn_id: str
) -> None:
    """种出可直接 publish 的草稿：授权 + 草稿 + 7 个 confirmed 字段。"""
    # ai_consent_grant.granted_at is a MySQL DATETIME without fractional
    # precision; pin the fixture to the same canonical value used by reads.
    granted_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await db.execute(
        text(
            "INSERT INTO ai_consent_grant "
            "(user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, 'profile_text_extract', 'profile-text-v1', "
            "'ai-policy-2026-08-07-v1', :granted_at)"
        ),
        {"user_id": user_id, "granted_at": granted_at},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, status, expected_revision, "
            "consent_snapshot_json, policy_revision, prompt_version, schema_version) "
            "VALUES (:draft_id, :user_id, 'personal', 'draft', 0, :consent, "
            "'ai-policy-2026-08-07-v1', 'profile-extract-prompt-v1', 'profile-extract-v1')"
        ),
        {
            "draft_id": draft_id,
            "user_id": user_id,
            "consent": json.dumps(
                {
                    "scope": "profile_text_extract",
                    "version": "profile-text-v1",
                    "policy_revision": "ai-policy-2026-08-07-v1",
                    "granted_at": granted_at.isoformat(),
                },
                ensure_ascii=False,
            ),
        },
    )
    for index, (field_key, value, display, span) in enumerate(_PUBLISH_DRAFT_FIELDS):
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, value_json, display_value, source_type, "
                "source_turn_ids, source_span, confidence, visibility, consent_scope, "
                "schema_version, prompt_version, content_hash, confirmation_status) "
                "VALUES (:draft_id, :field_key, 'personal', :value_json, :display_value, "
                "'user_answer', :turn_ids, :source_span, 0.91, 'self', "
                "'profile_text_extract', 'profile-extract-v1', 'profile-extract-prompt-v1', "
                ":content_hash, 'confirmed')"
            ),
            {
                "draft_id": draft_id,
                "field_key": field_key,
                "value_json": json.dumps(value, ensure_ascii=False),
                "display_value": display,
                "turn_ids": json.dumps([turn_id], ensure_ascii=False),
                "source_span": span,
                "content_hash": f"{index:02d}" + "a" * 62,
            },
        )


@pytest.mark.asyncio
async def test_real_publish_pins_revision_consent_and_projection(
    real_db_session: AsyncSession,
) -> None:
    await _clean_user(real_db_session)
    draft_id = "real-draft-profile-phase2"
    await _seed_publishable_draft(
        real_db_session, USER_ID, draft_id, "real-turn-phase2"
    )
    await real_db_session.commit()

    submission = await publish_profile_draft(
        real_db_session,
        draft_id,
        USER_ID,
        expected_revision=0,
        idempotency_key="real-publish-phase2",
    )
    assert submission.revision is not None
    await real_db_session.commit()

    task = await get_task(real_db_session, submission.task_id)
    assert task is not None
    assert task.source_revision_json == {
        "profile": 1,
        "preference": 0,
        "privacy": 0,
        "relationship": 0,
        "policy": 0,
    }
    assert task.consent_snapshot_json["version"] == "profile-text-v1"
    assert task.payload_summary["published_revision_id"] == submission.revision.revision_id

    revision_field = (
        await real_db_session.execute(
            text(
                "SELECT source_turn_ids, source_span FROM ai_profile_revision_field "
                "WHERE revision_id = :revision_id AND field_key = 'interest_tags'"
            ),
            {"revision_id": submission.revision.revision_id},
        )
    ).mappings().one()
    assert "real-turn-phase2" in str(revision_field["source_turn_ids"])
    assert revision_field["source_span"] == "周末喜欢旅行"

    result_ref = await profile_projection_handler(
        real_db_session, task, "integration-worker"
    )
    assert result_ref is not None
    await real_db_session.commit()
    projection = (
        await real_db_session.execute(
            text(
                "SELECT projection_kind, fields_json, source_revision_json, "
                "consent_snapshot_json, visibility_class, status "
                "FROM ai_feature_projection WHERE subject_user_id = :user_id "
                "AND projection_kind = 'personal_searchable' AND status = 'active'"
            ),
            {"user_id": USER_ID},
        )
    ).mappings().one()
    assert json.loads(projection["fields_json"]) == {
        "interest_tags": ["旅行"],
        "city_code": "330100",
        "occupation_group": "technology",
        "education_level": 4,
        "height_cm": 175,
        "income_band": "high",
        "marriage_status": "single",
    }
    assert json.loads(projection["source_revision_json"]) == task.source_revision_json
    assert json.loads(projection["consent_snapshot_json"])["version"] == "profile-text-v1"
    assert projection["visibility_class"] == "searchable"
    projection_gate = (
        await real_db_session.execute(
            text(
                "SELECT status, source_revision, projection_id "
                "FROM ai_profile_projection_status "
                "WHERE user_id = :user_id AND kind = 'personal_searchable'"
            ),
            {"user_id": USER_ID},
        )
    ).mappings().one()
    assert projection_gate["status"] == "active"
    assert projection_gate["source_revision"] == submission.revision.revision_id
    assert projection_gate["projection_id"] is not None

    await _clean_user(real_db_session)


@pytest.mark.asyncio
async def test_real_projection_skips_entry_only_ideal_partner(
    real_db_session: AsyncSession,
) -> None:
    """纯旅程愿遇之相（只有 entry_* 叙事字段）投影按设计内跳过。

    2026-09-05 决策：entry_* 全部不在结构化白名单内属旅程产出的正常形态，
    该 kind 记 ``skipped(no_fields)`` 并让任务 succeeded，其余 kind 不受影响；
    不再走 3 次重试后 failed（旧表现是 worker 把 ProjectionBuildError 覆盖成
    可重试的 AI_TEMPORARILY_UNAVAILABLE）。
    """
    await _clean_user(real_db_session)
    granted_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    consent = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": granted_at.isoformat(),
    }
    await real_db_session.execute(
        text(
            "INSERT INTO ai_consent_grant "
            "(user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, 'profile_text_extract', 'profile-text-v1', "
            "'ai-policy-2026-08-07-v1', :granted_at)"
        ),
        {"user_id": USER_ID, "granted_at": granted_at},
    )
    # 对齐真实发布后的五维版本状态（build_feature_projection 以它为准）。
    await real_db_session.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, "
            " relationship_revision, policy_revision) "
            "VALUES (:user_id, 10, 1, 24, 0, 0)"
        ),
        {"user_id": USER_ID},
    )
    # 个人像已有发布（保证 personal 两种 kind 正常构建），愿遇之相发布只含 entry。
    source_revision = {
        "profile": 10, "preference": 1, "privacy": 24,
        "relationship": 0, "policy": 0,
    }
    for subject, revision_no in (("personal", 1), ("ideal_partner", 1)):
        await real_db_session.execute(
            text(
                "INSERT INTO ai_profile_revision "
                "(user_id, subject, revision_no, draft_id, source_revision_json, "
                " policy_revision, published_by, published_at, created_at) "
                "VALUES (:user_id, :subject, :revision_no, :draft_id, "
                " :source_revision_json, 'ai-policy-2026-08-07-v1', :user_id, "
                " :published_at, :published_at)"
            ),
            {
                "user_id": USER_ID,
                "subject": subject,
                "revision_no": revision_no,
                "draft_id": f"skip-{subject}",
                "source_revision_json": json.dumps(source_revision, ensure_ascii=False),
                "published_at": granted_at,
            },
        )
    revision_id = int(
        (
            await real_db_session.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = 'ideal_partner' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"user_id": USER_ID},
            )
        ).scalar_one()
    )
    personal_revision_id = int(
        (
            await real_db_session.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = 'personal' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"user_id": USER_ID},
            )
        ).scalar_one()
    )
    # personal 至少要有一个白名单字段，保证那两种 kind 照常构建。
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_revision_field "
            "(revision_id, field_key, subject, value_json, display_value, "
            " field_kind, category, content, confidence, "
            " schema_version, content_hash) "
            "VALUES (:revision_id, 'city_code', 'personal', '\"330100\"', '杭州', "
            " 'structured', '', '现居杭州', 0.95, "
            " 'profile-extract-v1', :content_hash)"
        ),
        {"revision_id": personal_revision_id, "content_hash": "00" + "c" * 62},
    )
    for index, (field_key, category, content) in enumerate(
        [
            ("entry_personality_social_a1", "personality_social", "希望她情绪稳定"),
            ("entry_lifestyle_b2", "lifestyle", "作息规律，喜欢户外"),
            ("entry_intimacy_pattern_c3", "intimacy_pattern", "中等黏度"),
        ]
    ):
        await real_db_session.execute(
            text(
                "INSERT INTO ai_profile_revision_field "
                "(revision_id, field_key, subject, value_json, display_value, "
                " field_kind, category, content, confidence, "
                " schema_version, content_hash) "
                "VALUES (:revision_id, :field_key, 'ideal_partner', :value_json, "
                " :content, 'entry', :category, :content, 0.9, "
                " 'profile-extract-v1', :content_hash)"
            ),
            {
                "revision_id": revision_id,
                "field_key": field_key,
                "value_json": json.dumps({"text": content}, ensure_ascii=False),
                "category": category,
                "content": content,
                "content_hash": f"{index:02d}" + "b" * 62,
            },
        )
    # 修正 personal 字段的 content_hash 为合法 64 位十六进制。
    await real_db_session.execute(
        text(
            "UPDATE ai_profile_revision_field SET content_hash = :h "
            "WHERE field_key = 'city_code' AND revision_id = :revision_id"
        ),
        {"h": "00" + "c" * 62, "revision_id": personal_revision_id},
    )
    await real_db_session.commit()

    task = await enqueue_task(
        real_db_session,
        USER_ID,
        "profile_projection",
        idempotency_key="skip-ideal-entry-only",
        request_hash="skip-ideal-entry-only",
        revisions=source_revision,
        consent=consent,
    )
    await real_db_session.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload WHERE task_id = :task_id"
        ),
        {
            "payload": json.dumps(
                {
                    "published_revision_id": revision_id,
                    "revision_id": revision_id,
                    "draft_id": "skip-ideal_partner",
                    "subject": "ideal_partner",
                    "user_id": USER_ID,
                    "projection_target": "user_partner_preference",
                    "source_revision": source_revision,
                    "consent_snapshot": consent,
                },
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await real_db_session.commit()
    stored = await get_task(real_db_session, task.task_id)
    assert stored is not None

    result_ref = await profile_projection_handler(
        real_db_session, stored, "integration-worker"
    )
    assert result_ref is not None
    assert "ideal_partner_preference:skipped(no_fields)" in result_ref[0]
    await real_db_session.commit()

    kinds = (
        await real_db_session.execute(
            text(
                "SELECT projection_kind, status FROM ai_feature_projection "
                "WHERE subject_user_id = :user_id"
            ),
            {"user_id": USER_ID},
        )
    ).all()
    # 个人像两种照常构建；愿遇之相不落投影、也不把任务打成 failed。
    assert {row.projection_kind for row in kinds} == {
        "personal_searchable",
        "personal_compatibility",
    }

    await _clean_user(real_db_session)


async def _clean_narrative(db: AsyncSession) -> None:
    # 叙事链路用例会按需种 revision/consent/draft（回放与成功路径用例），
    # 清理范围对齐 _clean_user，另加 ai_profile_summary。
    statements = (
        "DELETE FROM ai_profile_summary WHERE user_id = :u",
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :u",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN "
        "(SELECT draft_id FROM ai_profile_draft WHERE user_id = :u)",
        "DELETE FROM ai_profile_draft WHERE user_id = :u",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN "
        "(SELECT id FROM ai_profile_revision WHERE user_id = :u)",
        "DELETE FROM ai_profile_revision WHERE user_id = :u",
        "DELETE FROM ai_task WHERE owner_user_id = :u",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :u",
        "DELETE FROM user_revision_state WHERE user_id = :u",
        "DELETE FROM ai_consent_grant WHERE user_id = :u",
    )
    for statement in statements:
        await db.execute(text(statement), {"u": NARRATIVE_USER})
    await db.commit()


def _summary_row(status: str) -> dict:
    data = json.dumps(
        {"persona_title": "温和笃定的人", "insight": "测试", "dimensions": [],
         "ideal_weights": [], "persona_tags": []},
        ensure_ascii=False,
    )
    return {
        "user_id": NARRATIVE_USER,
        "subject": "personal",
        "summary_text": data,
        "status": status,
        "content_hash": "0" * 64,
    }


@pytest.mark.asyncio
async def test_real_narrative_confirmation_loop(real_db_session: AsyncSession) -> None:
    await _clean_narrative(real_db_session)
    row = _summary_row("pending_confirmation")
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_summary (user_id, subject, summary_text, status, "
            " content_hash) VALUES (:user_id, :subject, :summary_text, :status, "
            " :content_hash)"
        ),
        row,
    )
    await real_db_session.commit()

    loaded = await load_published_narrative(real_db_session, NARRATIVE_USER, "personal")
    assert loaded is not None and loaded["status"] == "pending_confirmation"

    changed = await confirm_profile_narrative(
        real_db_session, NARRATIVE_USER, "personal"
    )
    assert changed is True
    confirmed = await load_published_narrative(
        real_db_session, NARRATIVE_USER, "personal"
    )
    assert confirmed["status"] == "confirmed"

    missing = await confirm_profile_narrative(
        real_db_session, NARRATIVE_USER, "ideal_partner"
    )
    assert missing is False


async def _insert_task_row(
    db: AsyncSession, index: int, *, task_type: str = "profile_narrative"
) -> None:
    await db.execute(
        text(
            "INSERT INTO ai_task (task_id, owner_user_id, task_type, scene, "
            " idempotency_key, status) "
            "VALUES (:task_id, :owner_user_id, :task_type, 'profile_narrative', "
            " :idempotency_key, 'queued')"
        ),
        {
            "task_id": f"real-narr-regen-{index:04d}",
            "owner_user_id": NARRATIVE_USER,
            "task_type": task_type,
            "idempotency_key": f"real-narr-regen-{index:04d}",
        },
    )


async def _seed_latest_revision(db: AsyncSession, user_id: int, revision_no: int) -> int:
    """种一条最小 revision 并返回其 id（regenerate 的放行前提与 digest 输入）。"""
    await db.execute(
        text(
            "INSERT INTO ai_profile_revision (user_id, subject, revision_no, "
            " policy_revision) VALUES (:u, 'personal', :no, 'ai-policy-2026-08-07-v1')"
        ),
        {"u": user_id, "no": revision_no},
    )
    row = (
        await db.execute(
            text(
                "SELECT id FROM ai_profile_revision "
                "WHERE user_id = :u AND subject = 'personal' "
                "ORDER BY revision_no DESC, id DESC LIMIT 1"
            ),
            {"u": user_id},
        )
    ).mappings().one()
    return int(row["id"])


@pytest.mark.asyncio
async def test_real_narrative_regenerate_rate_limit(
    real_db_session: AsyncSession,
) -> None:
    """24h 内 profile_narrative 任务满 5 次后 regenerate 被拒（UTC 窗口 + task_type 过滤）。

    回放先行重排后：无 revision 时在限频前就命中"尚未生成过"前置；场景 2
    需种 revision 才能触达限频分支。
    """
    await _clean_narrative(real_db_session)

    # 4 条 narrative + 1 条其他 task_type：限频未触发，卡在前置校验上
    # （该用户无 revision → AIInputError"尚未生成过"，不含"上限"）。
    for index in range(4):
        await _insert_task_row(real_db_session, index)
    await _insert_task_row(real_db_session, 99, task_type="profile_projection")
    await real_db_session.commit()
    with pytest.raises(AIInputError) as opened:
        await request_narrative_regenerate(
            real_db_session, NARRATIVE_USER, "personal", "real-regen-open-1"
        )
    assert "上限" not in str(opened.value)

    # 第 5 条 narrative（24h 窗口内）+ 种 revision → 触发每日限频。
    revision_id = await _seed_latest_revision(real_db_session, NARRATIVE_USER, 1)
    await _insert_task_row(real_db_session, 4)
    await real_db_session.commit()
    with pytest.raises(AIInputError, match="上限"):
        await request_narrative_regenerate(
            real_db_session, NARRATIVE_USER, "personal", "real-regen-blocked"
        )

    # 清掉 revision 恢复"无 revision"前置，再划一条出窗口验证窗口语义。
    await real_db_session.execute(
        text("DELETE FROM ai_profile_revision WHERE id = :rid"), {"rid": revision_id}
    )
    await real_db_session.commit()
    await real_db_session.execute(
        text(
            "UPDATE ai_task SET created_at = UTC_TIMESTAMP() - INTERVAL 25 HOUR "
            "WHERE owner_user_id = :u AND idempotency_key = :key"
        ),
        {"u": NARRATIVE_USER, "key": "real-narr-regen-0000"},
    )
    await real_db_session.commit()
    with pytest.raises(AIInputError) as reopened:
        await request_narrative_regenerate(
            real_db_session, NARRATIVE_USER, "personal", "real-regen-open-2"
        )
    assert "上限" not in str(reopened.value)

    await _clean_narrative(real_db_session)


@pytest.mark.asyncio
async def test_real_narrative_regenerate_replays_idempotency_key_under_limit(
    real_db_session: AsyncSession,
) -> None:
    """终审 Important-1：满限后同 Idempotency-Key 重试必须回放原任务而非 400 上限。

    digest 一致（同 revision+subject）→ 回放；digest 不一致（最新 revision
    变化）→ 409 TASK_IDEMPOTENCY_CONFLICT。
    """
    await _clean_narrative(real_db_session)
    revision_id = await _seed_latest_revision(real_db_session, NARRATIVE_USER, 1)
    # 5 条窗口内 narrative（含同 key 的既有任务）→ 已达限频上限。
    for index in range(4):
        await _insert_task_row(real_db_session, index)
    await real_db_session.execute(
        text(
            "INSERT INTO ai_task (task_id, owner_user_id, task_type, scene, "
            " idempotency_key, status, request_digest) "
            "VALUES ('real-regen-replay-0004', :u, 'profile_narrative', "
            " 'profile_narrative', 'real-regen-replay-narrative', 'queued', :digest)"
        ),
        {
            "u": NARRATIVE_USER,
            "digest": hash_narrative_request(revision_id, "personal"),
        },
    )
    await real_db_session.commit()

    replayed = await request_narrative_regenerate(
        real_db_session, NARRATIVE_USER, "personal", "real-regen-replay"
    )
    assert replayed.task_id == "real-regen-replay-0004"

    # 新增 revision_no=2 → 最新 revision 变化 → digest 不一致 → 409。
    await _seed_latest_revision(real_db_session, NARRATIVE_USER, 2)
    await real_db_session.commit()
    with pytest.raises(TaskError) as conflict:
        await request_narrative_regenerate(
            real_db_session, NARRATIVE_USER, "personal", "real-regen-replay"
        )
    assert conflict.value.status_code == 409

    await _clean_narrative(real_db_session)


@pytest.mark.asyncio
async def test_real_narrative_regenerate_success_enqueues_task(
    real_db_session: AsyncSession,
) -> None:
    """终审 Important-2：regenerate 成功路径（resolution 分支 + 入队回填）真库覆盖。"""
    await _clean_narrative(real_db_session)
    await _seed_publishable_draft(
        real_db_session, NARRATIVE_USER, "real-draft-regen-success", "real-turn-regen"
    )
    submission = await publish_profile_draft(
        real_db_session,
        "real-draft-regen-success",
        NARRATIVE_USER,
        expected_revision=0,
        idempotency_key="real-regen-success-publish",
    )
    assert submission.revision is not None
    await real_db_session.commit()

    task = await request_narrative_regenerate(
        real_db_session, NARRATIVE_USER, "personal", "real-regen-success"
    )
    await real_db_session.commit()

    assert task.task_type == "profile_narrative"
    assert task.status.value == "queued"
    assert task.idempotency_key == "real-regen-success-narrative"
    # payload/source/consent 回填发生在 enqueue 之后（助手内 UPDATE），
    # 返回的内存对象不含它们——契约验收点是库内回填，回读断言。
    stored = await get_task(real_db_session, task.task_id)
    assert stored is not None
    assert stored.consent_snapshot_json is not None
    assert stored.consent_snapshot_json["version"] == "profile-text-v1"
    assert stored.source_revision_json is not None
    assert stored.payload_summary is not None
    assert stored.payload_summary["subject"] == "personal"
    assert (
        stored.payload_summary["published_revision_id"]
        == submission.revision.revision_id
    )

    await _clean_narrative(real_db_session)
