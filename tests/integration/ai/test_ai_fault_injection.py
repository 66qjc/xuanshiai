"""Task 11 fault taxonomy checks.

The gateway/provider classification is evaluation-only by design: it exercises
the real boundary code with deterministic mock failures but does not pretend to
be a production outage drill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.ai.base import AITaskContext, SearchParseRequest, StructuredExtractRequest
from app.services.ai.gateway import AIGateway
from app.services.ai.providers import MockAIProvider
from app.services.ai.search import _consume_parse_quota
from app.services.ai.tasks import claim_tasks, enqueue_task, reap_expired_leases
from app.workers.ai_worker import _process

USER_ID = 9_876_544_210


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "method", "case_request", "expected_code", "retryable"),
    [
        (
            "timeout",
            "structured_extract",
            StructuredExtractRequest(
                subject="personal",
                turn_texts=("周末喜欢旅行",),
                consent_version="profile-text-v1",
                policy_revision="ai-policy-2026-08-07-v1",
            ),
            "AI_TEMPORARILY_UNAVAILABLE",
            True,
        ),
        (
            "http_429",
            "parse_search_query",
            SearchParseRequest(query_text="26到32岁 杭州"),
            "AI_QUOTA_EXCEEDED",
            True,
        ),
        (
            "http_500",
            "parse_search_query",
            SearchParseRequest(query_text="26到32岁 杭州"),
            "AI_TEMPORARILY_UNAVAILABLE",
            True,
        ),
        (
            "schema_invalid",
            "structured_extract",
            StructuredExtractRequest(
                subject="personal",
                turn_texts=("周末喜欢旅行",),
                consent_version="profile-text-v1",
                policy_revision="ai-policy-2026-08-07-v1",
            ),
            "AI_INPUT_INVALID",
            False,
        ),
        (
            "policy_blocked",
            "parse_search_query",
            SearchParseRequest(query_text="加微信 26到32岁"),
            "AI_POLICY_DENIED",
            False,
        ),
    ],
)
async def test_gateway_fault_taxonomy_evaluation_only(
    failure: str,
    method: str,
    case_request: object,
    expected_code: str,
    retryable: bool,
) -> None:
    gateway = AIGateway(provider=MockAIProvider(failures=[failure]))
    context = AITaskContext(
        task_id=f"fault-{failure}",
        request_id=f"fault-{failure}",
        scene=method,
        provider="mock",
        model="mock-model-v1",
        prompt_version="prompt-v1",
        schema_version="schema-v1",
        input_revision={"profile": 1},
        policy_revision="ai-policy-2026-08-07-v1",
    )
    if method == "structured_extract":
        outcome = await gateway.structured_extract(context, case_request)
    else:
        outcome = await gateway.parse_search_query(context, case_request)
    assert outcome.result is None
    assert outcome.error_code == expected_code
    assert outcome.retryable is retryable


@pytest.mark.asyncio
async def test_lease_expiry_and_worker_kill_recovery_real_db(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    await real_db_session.execute(
        text("DELETE FROM ai_task WHERE owner_user_id = :user_id"),
        {"user_id": USER_ID},
    )
    await real_db_session.commit()

    task = await enqueue_task(
        real_db_session,
        owner_user_id=USER_ID,
        task_type="integration_no_handler",
        idempotency_key="fault-lease-expiry",
        request_hash="1" * 64,
        revisions={"profile": 0, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0},
        consent=None,
    )
    await real_db_session.commit()

    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as db:
        claimed = await claim_tasks(db, "fault-worker", _now(), 1)
        await db.commit()
    assert [item.task_id for item in claimed] == [task.task_id]

    # Simulate a worker death after lease acquisition but before completion by
    # expiring the lease directly, then letting the real reaper recover it.
    await real_db_session.execute(
        text(
            "UPDATE ai_task SET lease_until = :expired WHERE task_id = :task_id"
        ),
        {"expired": _now() - timedelta(seconds=1), "task_id": task.task_id},
    )
    await real_db_session.commit()

    recovered = await reap_expired_leases(
        real_db_session,
        _now(),
        limit=10,
    )
    await real_db_session.commit()
    assert task.task_id in recovered

    await _process(None, claimed[0], "fault-worker", session_provider=factory)
    status = await real_db_session.scalar(
        text("SELECT status FROM ai_task WHERE task_id = :task_id"),
        {"task_id": task.task_id},
    )
    assert status in {"retry_wait", "failed"}


@pytest.mark.asyncio
async def test_search_parse_tolerates_redis_blip_evaluation_only(
    monkeypatch: pytest.MonkeyPatch,
    real_db_session: AsyncSession,
) -> None:
    class BrokenRedis:
        async def eval(self, *_args: object, **_kwargs: object) -> int:
            raise RedisError("transient redis outage")

        async def incr(self, *_args: object, **_kwargs: object) -> int:
            raise RedisError("transient redis outage")

        async def expire(self, *_args: object, **_kwargs: object) -> bool:
            raise RedisError("transient redis outage")

    monkeypatch.setattr("app.services.ai.search.redis_client", BrokenRedis())
    await _consume_parse_quota(real_db_session, USER_ID)


@pytest.mark.asyncio
async def test_engine_reopen_after_dispose_evaluation_only(
    real_db_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as db:
        assert await db.scalar(text("SELECT 1")) == 1
    await real_db_engine.dispose()
    async with factory() as db:
        assert await db.scalar(text("SELECT 1")) == 1
