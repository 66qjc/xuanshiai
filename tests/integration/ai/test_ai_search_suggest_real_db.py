"""WP-S3 猜你喜欢 AI 化真实库集成测试。

覆盖：生成任务（含同日幂等回放与频控）→ worker handler 归纳并写 24h Redis
缓存 → GET 读取端 source='ai'；空投影降级不建任务；Redis 未命中回退标签。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.schemas.ai_common import AiConsentGrantRequest
from app.services.ai import search as search_module
from app.services.ai.base import SearchSuggestResult
from app.services.ai.consents import grant_consent
from app.services.ai.profile import AIInputError
from app.services.ai.search import (
    SEARCH_SUGGEST_TASK_TYPE,
    generate_search_suggestions,
    get_search_suggestions,
    search_suggest_handler,
)
from app.services.ai.tasks import (
    AiTaskRecord,
    claim_tasks,
    complete_task,
    enqueue_task,
    start_task,
)

POLICY_REVISION = "ai-policy-2026-08-07-v1"
CONSENT_VERSION = "profile-text-v1"
USER_SUGGEST = 9_880_000_601
USER_DEGRADED = 9_880_000_602
USER_RATE = 9_880_000_603


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def _clean(db: AsyncSession, user_id: int) -> None:
    # claim 是全局抢占（不按 user 过滤）：先清掉所有 search_suggest 残留
    # 任务，避免上一轮失败运行污染本文件的 claim 数量断言。
    await db.execute(text("DELETE FROM ai_task WHERE task_type = 'search_suggest'"))
    for statement in (
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM ai_consent_operation WHERE user_id = :user_id",
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
    ):
        await db.execute(text(statement), {"user_id": user_id})
    await db.commit()


async def _seed_projection(
    db: AsyncSession, user_id: int, *, with_entries: bool = True
) -> None:
    fields = {"interest_tags": ["旅行", "看展"], "lifestyle_tags": ["户外"]}
    entry_digest = "价值观：欣赏踏实上进的人\n兴趣爱好：周末旅行与看展" if with_entries else None
    for kind, visibility in (
        ("personal_searchable", "searchable"),
        ("ideal_partner_preference", "self_only"),
    ):
        await db.execute(
            text(
                "INSERT INTO ai_feature_projection "
                "(subject_user_id, projection_kind, source_hash, projection_version, "
                " fields_json, entry_digest, source_revision_json, consent_snapshot_json, "
                " visibility_class, status) "
                "VALUES (:user_id, :kind, :source_hash, 'profile-extract-v1', "
                " :fields_json, :entry_digest, '{}', '{}', :visibility, 'active')"
            ),
            {
                "user_id": user_id,
                "kind": kind,
                "source_hash": f"hash-{user_id}-{kind}",
                "fields_json": json.dumps(
                    fields if kind == "personal_searchable"
                    else {"occupation_group": ["technology", "education"]},
                    ensure_ascii=False,
                ),
                "entry_digest": entry_digest if kind == "personal_searchable" else None,
                "visibility": visibility,
            },
        )


class _SuggestGateway:
    """注入固定建议结果；重复词与空白行用于校验去重/过滤。"""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        del timeout_seconds
        self.calls: list[tuple[str, ...]] = []

    async def generate_search_suggestions(self, context, request):
        del context
        self.calls.append(tuple(request.context_lines))
        return _outcome(
            SearchSuggestResult(
                schema_version="search-suggest-v1",
                suggestions=(
                    "喜欢旅行的女生",
                    "热爱看展的",
                    "喜欢旅行的女生",
                    "  ",
                    "户外爱好者",
                ),
            )
        )


def _outcome(result):
    from app.services.ai.gateway import InvokeOutcome

    return InvokeOutcome(result=result)


@pytest.mark.asyncio
async def test_real_search_suggest_generate_handler_read_and_replay(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    real_redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _clean(real_db_session, USER_SUGGEST)
    await real_db_session.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision) VALUES (:user_id, 0, 0, 0, 0, 0)"
        ),
        {"user_id": USER_SUGGEST},
    )
    await grant_consent(
        real_db_session,
        USER_SUGGEST,
        "profile_text_extract",
        AiConsentGrantRequest(
            consent_version=CONSENT_VERSION, policy_revision=POLICY_REVISION
        ),
        f"suggest-grant-{USER_SUGGEST}",
        0,
    )
    await _seed_projection(real_db_session, USER_SUGGEST)
    await real_db_session.commit()

    # 生成任务。
    result = await generate_search_suggestions(
        real_db_session, USER_SUGGEST, "client-suggest-key-1"
    )
    await real_db_session.commit()
    assert result.source == "ai"
    assert result.replayed is False
    assert result.task_id

    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as claim_db:
        claimed = await claim_tasks(claim_db, "worker-suggest-a", _now(), 10)
        await claim_db.commit()
    assert len(claimed) == 1
    async with factory() as start_db:
        started = await start_task(start_db, claimed[0].task_id, "worker-suggest-a")
        await start_db.commit()

    gateway = _SuggestGateway()
    monkeypatch.setattr(search_module, "AIGateway", lambda timeout_seconds=None: gateway)
    async with factory() as handler_db:
        handler_result = await search_suggest_handler(
            handler_db, started, "worker-suggest-a"
        )
        assert handler_result is not None
        assert handler_result[0] == "search-suggest:3"  # 去重 + 去空白后 3 条
        await handler_db.commit()
    async with factory() as finalize_db:
        completed = await complete_task(
            finalize_db, started.task_id, "worker-suggest-a",
            handler_result[0], handler_result[1],
        )
        assert completed.status.value == "succeeded"
        await finalize_db.commit()

    # GET：AI 缓存优先。
    async with factory() as read_db:
        read = await get_search_suggestions(read_db, USER_SUGGEST)
    assert read.source == "ai"
    assert read.items == ["喜欢旅行的女生", "热爱看展的", "户外爱好者"]
    # 上下文行含标签、理想型条件与条目摘要（faithfulness 输入）。
    lines = gateway.calls[0]
    assert any("兴趣/生活方式标签：旅行" in line for line in lines)
    assert any("理想型条件 occupation_group" in line for line in lines)
    assert any("条目：价值观" in line for line in lines)

    # 同日幂等回放：不再新建任务。
    replay = await generate_search_suggestions(
        real_db_session, USER_SUGGEST, "client-suggest-key-2"
    )
    await real_db_session.commit()
    assert replay.replayed is True
    assert replay.task_id == result.task_id

    await real_redis.delete(f"ai:search_suggest:{USER_SUGGEST}")


@pytest.mark.asyncio
async def test_real_search_suggest_degraded_and_rate_limit(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    real_redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _clean(real_db_session, USER_RATE)  # 清残留 suggest 任务
    # 降级：无投影用户不建任务。
    await _clean(real_db_session, USER_DEGRADED)
    result = await generate_search_suggestions(
        real_db_session, USER_DEGRADED, "client-degraded-key"
    )
    assert result.status == "degraded"
    assert result.task_id == ""
    assert result.source == "tags"
    # GET：无缓存回退标签（空标签也 source='tags'）。
    read = await get_search_suggestions(real_db_session, USER_DEGRADED)
    assert read.source == "tags"
    await real_db_session.commit()

    # 频控：24h 窗口内任务数达上限（monkeypatch 为 2）时 400。
    await _clean(real_db_session, USER_RATE)
    await _seed_projection(real_db_session, USER_RATE)  # 有投影才会走到频控
    await real_db_session.commit()
    monkeypatch.setattr(search_module.settings, "ai_search_suggest_daily_limit", 2)
    for idx in range(2):
        await enqueue_task(
            db=real_db_session,
            owner_user_id=USER_RATE,
            task_type=SEARCH_SUGGEST_TASK_TYPE,
            idempotency_key=f"rate-key-{idx}",
            request_hash=f"hash-{idx}",
        )
    await real_db_session.commit()
    with pytest.raises(AIInputError):
        await generate_search_suggestions(
            real_db_session, USER_RATE, "client-rate-key"
        )
    await real_db_session.commit()

    # 清场。
    cleanup_factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with cleanup_factory() as cleanup_db:
        for user_id in (USER_SUGGEST, USER_DEGRADED, USER_RATE):
            await _clean(cleanup_db, user_id)
        await cleanup_db.commit()
