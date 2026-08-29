"""WP-P1b entry 条目链路真实库集成测试。

用真库验证三件事（fake 单测覆盖不到的持久化与 SQL 过滤语义）：
1. 抽取 handler 在网关注入 entry 结果后，草稿落出 field_kind='entry' 行
   （value_json 恒 NULL、正文在 content），structured 行不受影响；
2. entry 确认/编辑走真实 PATCH 事务并持久化（commit 后新会话重读成立）；
3. ``_load_field_keys`` 与发布门槛对 entry 的过滤防回归——entry 确认数
   不计入题目推进、进度与发布门槛（Global Constraint）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSessionStatus,
    ProfileSubject,
)
from app.services.ai import profile as profile_module
from app.services.ai.base import ExtractedEntry, ExtractedField
from app.services.ai.consents import grant_consent
from app.services.ai.profile import (
    _load_field_keys,
    confirm_profile_draft,
    create_profile_session,
    extract_profile_turn,
    publish_profile_draft,
    submit_profile_turn,
)
from app.services.ai.tasks import AiTaskRecord, claim_tasks, start_task

POLICY_REVISION = "ai-policy-2026-08-07-v1"
CONSENT_VERSION = "profile-text-v1"
USER_EXTRACT = 9_880_000_101
USER_EDIT = 9_880_000_102


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


class _FakeOutcome:
    def __init__(self, result: object) -> None:
        self.result = result
        self.error_code = None
        self.retryable = False


class _FakeEntryGateway:
    """注入 entry+structured 固定结果，替代 AIGateway.structured_extract。"""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        del timeout_seconds

    async def structured_extract(self, context: object, request: object) -> _FakeOutcome:
        del context, request
        return _FakeOutcome(_build_result())


def _build_result():
    from app.services.ai.base import StructuredExtractResult

    return StructuredExtractResult(
        schema_version=profile_module.PROFILE_SCHEMA_VERSION,
        fields=(
            ExtractedField(
                field_key="interest_tags",
                subject=ProfileSubject.PERSONAL,
                value=["旅行"],
                source_quote="周末喜欢旅行",
                confidence=0.9,
                schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
        ),
        entries=(
            ExtractedEntry(
                category="values",
                content="欣赏阳光开朗、品行端正的人",
                subject=ProfileSubject.PERSONAL,
                source_quote="我喜欢阳光开朗品行端正的",
                confidence=0.88,
                schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
        ),
    )


async def _seed_session_with_turn(
    db: AsyncSession, user_id: int, idem_prefix: str
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
        f"{idem_prefix}-grant-{user_id}",
        0,
    )
    await db.commit()
    session = await create_profile_session(
        db, user_id, ProfileSubject.PERSONAL, CONSENT_VERSION, f"{idem_prefix}-session-{user_id}"
    )
    accepted = await submit_profile_turn(
        db,
        session.session_id,
        user_id,
        f"{idem_prefix}-turn-{user_id}",
        "周末喜欢旅行，我欣赏阳光开朗品行端正的人。",
        f"{idem_prefix}-extract-{user_id}",
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
async def test_real_extract_persists_entry_rows(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _clean(real_db_session, USER_EXTRACT)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as seed_db:
        session_id, _task_id = await _seed_session_with_turn(seed_db, USER_EXTRACT, "entry-x")

    started = await _claim_and_start(factory, "worker-entry-a")

    async with factory() as handler_db:
        monkeypatch.setattr(profile_module, "AIGateway", _FakeEntryGateway)
        result = await extract_profile_turn(handler_db, started, "worker-entry-a")
        assert result is not None
        result_ref, _revisions = result
        assert result_ref.startswith("profile-draft:")
        draft_id = result_ref.split(":", 1)[1]
        # handler 不 commit；提交由 worker finalize 承担——此处显式提交等价。
        await handler_db.commit()

    async with factory() as check_db:
        entry_row = (
            await check_db.execute(
                text(
                    "SELECT field_kind, category, content, value_json, display_value, "
                    "confirmation_status FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id AND field_kind = 'entry'"
                ),
                {"draft_id": draft_id},
            )
        ).one()
        assert entry_row[0] == "entry"
        assert entry_row[1] == "values"
        assert entry_row[2] == "欣赏阳光开朗、品行端正的人"
        assert entry_row[3] is None
        assert entry_row[4] == entry_row[2]
        assert entry_row[5] == "suggested"
        structured_row = (
            await check_db.execute(
                text(
                    "SELECT field_kind, value_json FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id AND field_key = 'interest_tags'"
                ),
                {"draft_id": draft_id},
            )
        ).one()
        assert structured_row[0] == "structured"
        assert structured_row[1] is not None
        # entry 不进入题目推进/进度口径（_load_field_keys 的 SQL 过滤）。
        field_keys, confirmed_keys = await _load_field_keys(check_db, session_id)
        entry_keys = {k for k in field_keys if k.startswith("entry_")}
        assert not entry_keys
        assert "interest_tags" in field_keys
        assert "interest_tags" not in confirmed_keys  # 仍是 suggested

    # 共享测试库纪律：清掉本测试的全部行（含 running 态 task），否则
    # 迁移 down 守卫（refusing down while AI tasks are active）与其他
    # 测试的 claim 断言会被残留行污染。
    async with factory() as cleanup_db:
        await _clean(cleanup_db, USER_EXTRACT)


@pytest.mark.asyncio
async def test_real_entry_edit_and_publish_threshold(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    await _clean(real_db_session, USER_EDIT)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    draft_id = f"dr-entry-{USER_EDIT % 100000}"
    session_id = f"sess-entry-{USER_EDIT % 100000}"

    async with factory() as seed_db:
        await seed_db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, 0, 0, 0, 0, 0)"
            ),
            {"user_id": USER_EDIT},
        )
        await seed_db.execute(
            text(
                "INSERT INTO ai_profile_session "
                "(session_id, user_id, subject, status, input_mode, consent_version, "
                " policy_revision, active_status, created_at, updated_at) "
                "VALUES (:session_id, :user_id, 'personal', 'awaiting_confirmation', "
                " 'text', :consent_version, :policy_revision, 1, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "session_id": session_id,
                "user_id": USER_EDIT,
                "consent_version": CONSENT_VERSION,
                "policy_revision": POLICY_REVISION,
            },
        )
        await seed_db.execute(
            text(
                "INSERT INTO ai_profile_draft "
                "(draft_id, user_id, subject, session_id, status, expected_revision, "
                " policy_revision, schema_version, created_at, updated_at) "
                "VALUES (:draft_id, :user_id, 'personal', :session_id, 'draft', 0, "
                " :policy_revision, 'profile-extract-v1', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "draft_id": draft_id,
                "user_id": USER_EDIT,
                "session_id": session_id,
                "policy_revision": POLICY_REVISION,
            },
        )
        structured = [
            ("interest_tags", '["看展"]', "看展", "confirmed"),
            ("city_code", '"330100"', "杭州", "confirmed"),
            ("occupation_group", '"technology"', "互联网", "confirmed"),
            ("education_level", "4", "本科", "confirmed"),
            ("height_cm", "175", "175", "confirmed"),
            ("income_band", '"high"', "高", "confirmed"),
            ("marriage_status", '"single"', "单身", "suggested"),
        ]
        for key, value_json, display, status in structured:
            await seed_db.execute(
                text(
                    "INSERT INTO ai_profile_draft_field "
                    "(draft_id, field_key, subject, field_kind, value_json, display_value, "
                    " confidence, content_hash, confirmation_status, created_at, updated_at) "
                    "VALUES (:draft_id, :field_key, 'personal', 'structured', :value_json, "
                    " :display_value, 0.9, :content_hash, :status, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
                ),
                {
                    "draft_id": draft_id,
                    "field_key": key,
                    "value_json": value_json,
                    "display_value": display,
                    "content_hash": f"hash-{key}",
                    "status": status,
                },
            )
        for idx, category in enumerate(("values", "interests", "life_plan")):
            await seed_db.execute(
                text(
                    "INSERT INTO ai_profile_draft_field "
                    "(draft_id, field_key, subject, field_kind, category, content, "
                    " value_json, display_value, confidence, content_hash, "
                    " confirmation_status, created_at, updated_at) "
                    "VALUES (:draft_id, :field_key, 'personal', 'entry', :category, :content, "
                    " NULL, :content, 0.88, :content_hash, 'suggested', "
                    " UTC_TIMESTAMP(), UTC_TIMESTAMP())"
                ),
                {
                    "draft_id": draft_id,
                    "field_key": f"entry_{category}_0000000{idx}",
                    "category": category,
                    "content": f"条目内容 {idx}",
                    "content_hash": f"hash-entry-{idx}",
                },
            )
        await seed_db.commit()

    # 确认 + 编辑两个 entry：走真实 PATCH 事务（成功路径由本测试显式提交）。
    async with factory() as act_db:
        await confirm_profile_draft(
            act_db,
            draft_id,
            USER_EDIT,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="entry_values_00000000",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=0,
                ),
                ProfileDraftFieldPatchRequest(
                    field_key="entry_interests_00000001",
                    action=ProfileFieldPatchAction.REPLACE,
                    value="喜欢户外、露营和看展",
                    expected_revision=0,
                ),
            ],
            expected_revision=0,
        )
        await act_db.commit()

    async with factory() as check_db:
        rows = (
            await check_db.execute(
                text(
                    "SELECT field_key, confirmation_status, content, content_hash "
                    "FROM ai_profile_draft_field WHERE draft_id = :draft_id "
                    "AND field_kind = 'entry' ORDER BY field_key"
                ),
                {"draft_id": draft_id},
            )
        ).all()
        by_key = {row[0]: row for row in rows}
        assert by_key["entry_values_00000000"][1] == "confirmed"
        assert by_key["entry_values_00000000"][2] == "条目内容 0"
        assert by_key["entry_interests_00000001"][1] == "confirmed"
        assert by_key["entry_interests_00000001"][2] == "喜欢户外、露营和看展"
        assert by_key["entry_interests_00000001"][3] != "hash-entry-1"
        # 已确认的 entry 仍不出现在进度/门槛口径（_load_field_keys 过滤）。
        _field_keys, confirmed_keys = await _load_field_keys(check_db, session_id)
        assert not {k for k in confirmed_keys if k.startswith("entry_")}
        assert confirmed_keys == {
            "interest_tags",
            "city_code",
            "occupation_group",
            "education_level",
            "height_cm",
            "income_band",
        }

    # 6 个 structured 确认 + 2 个 entry 确认：门槛不满足（entry 不计入）。
    async with factory() as gate_db:
        with pytest.raises(profile_module.AIInputError):
            await publish_profile_draft(
                gate_db, draft_id, USER_EDIT, expected_revision=1,
                idempotency_key="entry-gate-publish",
            )
        # 补齐第 7 个 structured 确认后发布成立。
        await confirm_profile_draft(
            gate_db,
            draft_id,
            USER_EDIT,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="marriage_status",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )
        submission = await publish_profile_draft(
            gate_db, draft_id, USER_EDIT, expected_revision=2,
            idempotency_key="entry-ok-publish",
        )
        await gate_db.commit()
        assert submission.task_id

    # 同上：发布入队的 projection/cleanup 任务一并清理，不留活跃任务。
    async with factory() as cleanup_db:
        await _clean(cleanup_db, USER_EDIT)


@pytest.mark.asyncio
async def test_real_projection_entry_digest_and_structured_only_null(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    """发布含条目的 revision → 投影行 entry_digest 非空且带分类前缀；
    纯 structured 用户的投影 entry_digest 为 NULL。rollback-then-assert。"""
    from app.schemas.ai_common import ProjectionKind
    from app.services.ai.features import build_feature_projection

    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    user_entries = 9_880_000_201
    user_plain = 9_880_000_202
    await _clean(real_db_session, user_entries)
    await _clean(real_db_session, user_plain)
    zero_vector = (
        '{"profile": 0, "preference": 0, "privacy": 0, '
        '"relationship": 0, "policy": 0}'
    )

    async def _seed_user(db: AsyncSession, user_id: int, with_entries: bool) -> int:
        """Seed consent + revision(+fields)；返回 revision_id。"""
        await db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, 0, 0, 0, 0, 0)"
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
            f"digest-grant-{user_id}",
            0,
        )
        await db.execute(
            text(
                "INSERT INTO ai_profile_revision "
                "(user_id, subject, revision_no, draft_id, source_revision_json, "
                " policy_revision, published_by) "
                "VALUES (:user_id, 'personal', 1, NULL, :source_json, "
                " :policy_revision, :user_id)"
            ),
            {
                "user_id": user_id,
                "source_json": zero_vector,
                "policy_revision": POLICY_REVISION,
            },
        )
        row = (
            await db.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = 'personal' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"user_id": user_id},
            )
        ).one()
        revision_id = int(row[0])
        fields = [
            ("height_cm", None, None, "175", '"175"'),
            ("city_code", None, None, "杭州", '"330100"'),
        ]
        if with_entries:
            fields.extend(
                [
                    ("entry_values_digest01", "values", "欣赏阳光开朗、品行端正的人", None, None),
                    ("entry_interests_digest02", "interests", "周末旅行与看展", None, None),
                ]
            )
        for field_key, category, content, display, value_json in fields:
            await db.execute(
                text(
                    "INSERT INTO ai_profile_revision_field "
                    "(revision_id, field_key, subject, field_kind, category, content, "
                    " value_json, display_value, confidence, content_hash) "
                    "VALUES (:revision_id, :field_key, 'personal', "
                    " :field_kind, :category, :content, :value_json, :display_value, "
                    " 0.9, :content_hash)"
                ),
                {
                    "revision_id": revision_id,
                    "field_key": field_key,
                    "field_kind": "entry" if category else "structured",
                    "category": category,
                    "content": content,
                    "value_json": value_json,
                    "display_value": display or content,
                    "content_hash": f"hash-{field_key}",
                },
            )
        return revision_id

    async with factory() as seed_db:
        await _seed_user(seed_db, user_entries, with_entries=True)
        await _seed_user(seed_db, user_plain, with_entries=False)
        await seed_db.commit()

    async with factory() as build_db:
        projection_a = await build_feature_projection(
            build_db, user_entries, ProjectionKind.PERSONAL_SEARCHABLE,
            revision_vector=None,
        )
        projection_b = await build_feature_projection(
            build_db, user_plain, ProjectionKind.PERSONAL_SEARCHABLE,
            revision_vector=None,
        )
        assert projection_a.id is not None
        await build_db.commit()

    async with factory() as check_db:
        digest_row = (
            await check_db.execute(
                text(
                    "SELECT entry_digest FROM ai_feature_projection "
                    "WHERE subject_user_id = :user_id AND status = 'active' "
                    "AND projection_kind = 'personal_searchable'"
                ),
                {"user_id": user_entries},
            )
        ).one()
        digest = digest_row[0]
        assert digest is not None
        assert "价值观：欣赏阳光开朗、品行端正的人" in digest
        assert "兴趣爱好：周末旅行与看展" in digest
        plain_row = (
            await check_db.execute(
                text(
                    "SELECT entry_digest FROM ai_feature_projection "
                    "WHERE subject_user_id = :user_id AND status = 'active' "
                    "AND projection_kind = 'personal_searchable'"
                ),
                {"user_id": user_plain},
            )
        ).one()
        assert plain_row[0] is None

    # 共享测试库纪律：清场。
    async with factory() as cleanup_db:
        await _clean(cleanup_db, user_entries)
        await _clean(cleanup_db, user_plain)


@pytest.mark.asyncio
async def test_real_update_session_clarify_loop_and_patch_draft(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WP-P4a/b 真库全流程：update-intent → 澄清追问(assistant turn) →
    答复 → patch 候选（add+modify）落更新草稿（旧字段 confirmed、旧行不动）。"""
    from app.services.ai.base import ExtractedPatch, StructuredExtractResult
    from app.services.ai.consents import grant_consent
    from app.services.ai.profile import (
        _insert_assistant_turn,  # noqa: F401  (导入即冒烟)
        create_update_session,
        load_owned_session,
    )
    from app.services.ai.tasks import complete_task

    user_id = 9_880_000_301
    await _clean(real_db_session, user_id)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)

    # 种子：授权 + 已发布 revision（2 structured + 1 entry，modify 目标）。
    async with factory() as seed_db:
        await seed_db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, 1, 0, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
        await grant_consent(
            seed_db,
            user_id,
            "profile_text_extract",
            AiConsentGrantRequest(
                consent_version=CONSENT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
            f"upd-grant-{user_id}",
            0,
        )
        await seed_db.execute(
            text(
                "INSERT INTO ai_profile_revision "
                "(user_id, subject, revision_no, draft_id, source_revision_json, "
                " policy_revision, published_by) "
                "VALUES (:user_id, 'personal', 1, NULL, "
                " '{\"profile\": 1, \"preference\": 0, \"privacy\": 0, "
                "\"relationship\": 0, \"policy\": 0}', :policy_revision, :user_id)"
            ),
            {"user_id": user_id, "policy_revision": POLICY_REVISION},
        )
        revision_id = (
            await seed_db.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = 'personal' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
        for field_key, value_json, display in (
            ("interest_tags", '["旅行"]', "旅行"),
            ("height_cm", "175", "175"),
        ):
            await seed_db.execute(
                text(
                    "INSERT INTO ai_profile_revision_field "
                    "(revision_id, field_key, subject, field_kind, value_json, "
                    " display_value, confidence, content_hash) "
                    "VALUES (:revision_id, :field_key, 'personal', 'structured', "
                    " :value_json, :display_value, 0.9, :content_hash)"
                ),
                {
                    "revision_id": revision_id,
                    "field_key": field_key,
                    "value_json": value_json,
                    "display_value": display,
                    "content_hash": f"hash-{field_key}",
                },
            )
        await seed_db.execute(
            text(
                "INSERT INTO ai_profile_revision_field "
                "(revision_id, field_key, subject, field_kind, category, content, "
                " value_json, display_value, confidence, content_hash) "
                "VALUES (:revision_id, 'entry_values_seed01', 'personal', 'entry', "
                " 'values', '欣赏踏实上进的人', NULL, '欣赏踏实上进的人', 0.9, "
                " 'hash-entry-seed01')"
            ),
            {"revision_id": revision_id},
        )
        await seed_db.commit()

    # 澄清 fake：第一次问追问，第二次产出 add+modify patch。
    calls: list[str] = []

    class _UpdateGateway:
        def __init__(self, timeout_seconds: float | None = None) -> None:
            del timeout_seconds

        async def structured_extract(self, context: object, request: object) -> _FakeOutcome:
            calls.append(getattr(request, "entry_digest", None) or "")
            if len(calls) == 1:
                return _FakeOutcome(
                    StructuredExtractResult(
                        schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                        clarifying_question="偏向音乐、绘画还是舞蹈？",
                    )
                )
            return _FakeOutcome(
                StructuredExtractResult(
                    schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                    patches=(
                        ExtractedPatch(
                            action="add",
                            category="interests",
                            content="希望对方热爱艺术，愿意一起看展、听音乐会",
                            subject=ProfileSubject.PERSONAL,
                            source_quote="希望对方是搞艺术的，能陪我看展",
                            confidence=0.86,
                            schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                            prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                            policy_revision=POLICY_REVISION,
                        ),
                        ExtractedPatch(
                            action="modify",
                            category="values",
                            content="欣赏有艺术修养、踏实上进的人",
                            replaces_field_key="entry_values_seed01",
                            subject=ProfileSubject.PERSONAL,
                            source_quote="还是得有艺术修养",
                            confidence=0.9,
                            schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                            prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                            policy_revision=POLICY_REVISION,
                        ),
                    ),
                )
            )

    async with factory() as intent_db:
        session, submission = await create_update_session(
            intent_db, user_id, ProfileSubject.PERSONAL,
            "希望对方是艺术家，还是得有艺术修养", CONSENT_VERSION, "upd-intent-001",
        )
        assert session.session_kind == "update"
        assert session.status is ProfileSessionStatus.EXTRACTING
        await intent_db.commit()
        first_task_id = submission.task_id

    started = await _claim_and_start(factory, "worker-upd-a")
    assert started.task_id == first_task_id

    # 第一轮：clarifying_question → assistant turn + 会话回 draft。
    async with factory() as handler_db:
        monkeypatch.setattr(profile_module, "AIGateway", _UpdateGateway)
        result = await extract_profile_turn(handler_db, started, "worker-upd-a")
        assert result is not None
        assert result[0].startswith("profile-update:question:")
        await handler_db.commit()
    async with factory() as finalize_db:
        await complete_task(
            finalize_db, first_task_id, "worker-upd-a", result[0], result[1]
        )
        await finalize_db.commit()
    async with factory() as check_db:
        reloaded = await load_owned_session(check_db, session.session_id, user_id)
        assert reloaded.status is ProfileSessionStatus.DRAFT
        turns = (
            await check_db.execute(
                text(
                    "SELECT role, answer_text FROM ai_profile_turn "
                    "WHERE session_id = :session_id ORDER BY turn_no"
                ),
                {"session_id": session.session_id},
            )
        ).all()
        assert [row[0] for row in turns] == ["user", "assistant"]
        assert "偏向音乐、绘画还是舞蹈" in turns[1][1]
        # prompt 输入带条目摘要（modify 可定位）。
        assert any("entry_values_seed01" in digest for digest in calls)

    # 第二轮：答复 → patch 草稿（旧字段 confirmed，add/modify suggested）。
    async with factory() as answer_db:
        accepted = await submit_profile_turn(
            answer_db,
            session.session_id,
            user_id,
            "upd-turn-client-002",
            "偏向看展和摄影，还是得有艺术修养",
            "upd-extract-002",
        )
        await answer_db.commit()
    second_started = await _claim_and_start(factory, "worker-upd-a")
    assert second_started.task_id == accepted.task_id
    async with factory() as handler_db:
        result2 = await extract_profile_turn(handler_db, second_started, "worker-upd-a")
        assert result2 is not None
        assert result2[0].startswith("profile-draft:")
        draft_id = result2[0].split(":", 1)[1]
        await handler_db.commit()
    async with factory() as finalize_db:
        await complete_task(
            finalize_db, accepted.task_id, "worker-upd-a", result2[0], result2[1]
        )
        await finalize_db.commit()

    async with factory() as check_db:
        reloaded = await load_owned_session(check_db, session.session_id, user_id)
        assert reloaded.status is ProfileSessionStatus.AWAITING_CONFIRMATION
        rows = (
            await check_db.execute(
                text(
                    "SELECT field_kind, category, content, confirmation_status, "
                    "replaces_field_key FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id ORDER BY field_kind, field_key"
                ),
                {"draft_id": draft_id},
            )
        ).all()
        base_structured = [r for r in rows if r[0] == "structured"]
        base_entries = [r for r in rows if r[0] == "entry" and r[3] == "confirmed"]
        patch_rows = [r for r in rows if r[0] == "entry" and r[3] == "suggested"]
        # 底稿：旧 structured 全部 confirmed（发布不丢字段）。
        assert len(base_structured) == 2
        assert all(r[3] == "confirmed" for r in base_structured)
        # 底稿：旧 entry confirmed 且原行未动（追加不覆盖）。
        assert len(base_entries) == 1
        assert base_entries[0][2] == "欣赏踏实上进的人"
        assert base_entries[0][4] is None
        # patch：add 无 replaces；modify 指向被改写条目。
        assert len(patch_rows) == 2
        modify_rows = [r for r in patch_rows if r[4] is not None]
        add_rows = [r for r in patch_rows if r[4] is None]
        assert len(modify_rows) == 1 and modify_rows[0][4] == "entry_values_seed01"
        assert len(add_rows) == 1 and (add_rows[0][1] or "") == "interests"
        assert add_rows[0][2] == "希望对方热爱艺术，愿意一起看展、听音乐会"

    # 共享测试库纪律：清场。
    async with factory() as cleanup_db:
        await _clean(cleanup_db, user_id)
       


@pytest.mark.asyncio
async def test_real_published_fields_is_new_across_two_revisions(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    """WP-P4b 真库：两轮发布后第二轮新增条目 is_new=true 置顶，首轮条目
    is_new=false 且仍在（追加不覆盖）；structured 恒 False；投影物化
    first_seen_revision。"""
    from app.schemas.ai_common import ProjectionKind
    from app.services.ai.features import build_feature_projection
    from app.services.ai.profile import list_published_profile_fields

    user_id = 9_880_000_401
    await _clean(real_db_session, user_id)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)

    async def _seed_revision(db, revision_no: int, entries: list[tuple[str, str]]) -> int:
        await db.execute(
            text(
                "INSERT INTO ai_profile_revision "
                "(user_id, subject, revision_no, draft_id, source_revision_json, "
                " policy_revision, published_by) "
                "VALUES (:user_id, 'personal', :revision_no, NULL, "
                " '{\"profile\": 1, \"preference\": 0, \"privacy\": 0, "
                "\"relationship\": 0, \"policy\": 0}', :policy_revision, :user_id)"
            ),
            {
                "user_id": user_id,
                "revision_no": revision_no,
                "policy_revision": POLICY_REVISION,
            },
        )
        revision_id = (
            await db.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = 'personal' "
                    "AND revision_no = :revision_no LIMIT 1"
                ),
                {"user_id": user_id, "revision_no": revision_no},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO ai_profile_revision_field "
                "(revision_id, field_key, subject, field_kind, value_json, "
                " display_value, confidence, content_hash) "
                "VALUES (:revision_id, 'height_cm', 'personal', 'structured', "
                " '175', '175', 0.9, 'hash-height')"
            ),
            {"revision_id": revision_id},
        )
        for field_key, content in entries:
            await db.execute(
                text(
                    "INSERT INTO ai_profile_revision_field "
                    "(revision_id, field_key, subject, field_kind, category, content, "
                    " value_json, display_value, confidence, content_hash) "
                    "VALUES (:revision_id, :field_key, 'personal', 'entry', 'values', "
                    " :content, NULL, :content, 0.9, :content_hash)"
                ),
                {
                    "revision_id": revision_id,
                    "field_key": field_key,
                    "content": content,
                    "content_hash": f"hash-{field_key}-{revision_no}",
                },
            )
        return revision_id

    async with factory() as seed_db:
        await seed_db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, 2, 0, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
        await grant_consent(
            seed_db,
            user_id,
            "profile_text_extract",
            AiConsentGrantRequest(
                consent_version=CONSENT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
            f"new-grant-{user_id}",
            0,
        )
        await _seed_revision(seed_db, 1, [("entry_values_r1", "欣赏踏实上进的人")])
        # 真实 update 流程会把旧字段（含条目）以 confirmed 拷入新草稿再发布，
        # 因此第二轮 revision 同时包含旧条目与新条目。
        await _seed_revision(
            seed_db,
            2,
            [
                ("entry_values_r1", "欣赏踏实上进的人"),
                ("entry_interests_r2", "热爱艺术愿意看展"),
            ],
        )
        await seed_db.commit()

    async with factory() as read_db:
        fields = await list_published_profile_fields(read_db, user_id, "personal")
        by_key = {item["field_key"]: item for item in fields}
        # 第二轮新增条目：is_new=True。
        assert by_key["entry_interests_r2"]["is_new"] is True
        # 首轮条目：is_new=False 但仍在（追加不覆盖）。
        assert by_key["entry_values_r1"]["is_new"] is False
        assert by_key["entry_values_r1"]["content"] == "欣赏踏实上进的人"
        # structured 恒 False；New 条目排在最前。
        assert by_key["height_cm"]["is_new"] is False
        assert fields[0]["field_key"] == "entry_interests_r2"
        # 投影物化：isNew 群组最早来源 revision_no = 1。
        projection = await build_feature_projection(
            read_db, user_id, ProjectionKind.PERSONAL_SEARCHABLE, revision_vector=None
        )
        assert projection.id is not None
        await read_db.commit()
    async with factory() as check_db:
        first_seen = (
            await check_db.execute(
                text(
                    "SELECT first_seen_revision FROM ai_feature_projection "
                    "WHERE subject_user_id = :user_id AND status = 'active' "
                    "AND projection_kind = 'personal_searchable'"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
        assert first_seen == 1

    # 共享测试库纪律：清场（含 projection 入队外的行）。
    async with factory() as cleanup_db:
        await cleanup_db.execute(
            text("DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id"),
            {"user_id": user_id},
        )
        await cleanup_db.commit()
        await _clean(cleanup_db, user_id)


@pytest.mark.asyncio
async def test_real_voice_mode_switch_and_extract_persistence(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    """WP-P5 真库：voice 模式切换与语音抽取落库走文字模式同一状态机。"""
    from app.services.ai.base import (
        ExtractedEntry,
        ExtractedField,
        StructuredExtractResult,
    )
    from app.services.ai.profile import (
        _load_field_keys,
        create_profile_session,
        persist_voice_extract_result,
        submit_profile_turn,
        update_session_input_mode,
    )

    user_id = 9_880_000_501
    await _clean(real_db_session, user_id)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)

    async with factory() as seed_db:
        await seed_db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, 0, 0, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
        await grant_consent(
            seed_db,
            user_id,
            "profile_text_extract",
            AiConsentGrantRequest(
                consent_version=CONSENT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
            "voice-grant-501",
            0,
        )
        await seed_db.commit()
        session = await create_profile_session(
            seed_db, user_id, ProfileSubject.PERSONAL, CONSENT_VERSION,
            "voice-session-501",
        )
        await submit_profile_turn(
            seed_db, session.session_id, user_id,
            "voice-text-turn-501", "周末喜欢旅行和看展。",
            "voice-text-extract-501",
        )
        await seed_db.commit()
        session_id = session.session_id

    voice_result = StructuredExtractResult(
        schema_version=profile_module.PROFILE_SCHEMA_VERSION,
        fields=(
            ExtractedField(
                field_key="city_code",
                subject=ProfileSubject.PERSONAL,
                value="330100",
                source_quote="我住在杭州",
                confidence=0.9,
                schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
        ),
        entries=(
            ExtractedEntry(
                category="interests",
                content="喜欢户外和摄影，常去西湖徒步",
                subject=ProfileSubject.PERSONAL,
                source_quote="周末常去西湖徒步拍照",
                confidence=0.85,
                schema_version=profile_module.PROFILE_SCHEMA_VERSION,
                prompt_version=profile_module.PROFILE_PROMPT_VERSION,
                policy_revision=POLICY_REVISION,
            ),
        ),
    )

    async with factory() as voice_db:
        reloaded = await update_session_input_mode(
            voice_db, session_id, user_id, "voice"
        )
        assert reloaded.input_mode == "voice"
        draft_id = await persist_voice_extract_result(
            voice_db, session_id, user_id,
            "我住在杭州，周末常去西湖徒步拍照。",
            "voice-finish-501-001",
            voice_result,
        )
        await voice_db.commit()
        assert draft_id

    async with factory() as check_db:
        row = (
            await check_db.execute(
                text(
                    "SELECT status, input_mode FROM ai_profile_session "
                    "WHERE session_id = :session_id"
                ),
                {"session_id": session_id},
            )
        ).one()
        assert row[0] == "awaiting_confirmation"
        assert row[1] == "voice"
        turn_row = (
            await check_db.execute(
                text(
                    "SELECT source_type, role FROM ai_profile_turn "
                    "WHERE session_id = :session_id "
                    "AND source_type = 'voice_transcript'"
                ),
                {"session_id": session_id},
            )
        ).one()
        assert turn_row[1] == "user"
        entry_rows = (
            await check_db.execute(
                text(
                    "SELECT field_kind FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id"
                ),
                {"draft_id": draft_id},
            )
        ).all()
        assert {r[0] for r in entry_rows} == {"structured", "entry"}
        # 重复 finish（同 client_turn_id）幂等：不产生第二份草稿。
        draft_id_2 = await persist_voice_extract_result(
            check_db, session_id, user_id,
            "我住在杭州，周末常去西湖徒步拍照。",
            "voice-finish-501-001",
            voice_result,
        )
        assert draft_id_2 == draft_id
        # 切回 text：进度与已确认字段延续（同一行状态机）。
        reloaded = await update_session_input_mode(
            check_db, session_id, user_id, "text"
        )
        assert reloaded.input_mode == "text"
        _field_keys, _confirmed = await _load_field_keys(check_db, session_id)
        assert "city_code" in _field_keys

    # 共享测试库纪律：清场。
    async with factory() as cleanup_db:
        await _clean(cleanup_db, user_id)
