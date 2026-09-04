"""批次3 #26：墨相师旅程 WS 全链路集成测试（mock LLM + 真实 MySQL）。

覆盖此前只有服务级用例（test_moxiang_journey_worker_real_db）验证过的链路，
但这次从 **WebSocket 协议层** 驱动：

    session_start → journey_ready + 开场白
    → text_message ×4 → ai_reply（抽取任务入队）
    → worker 处理候选任务 → journey_progress + build_invite 推送
    → build_invite_accept → build_invite_resolved + confirm_card
    → 服务级确认/发布 → worker 投影 → ai_feature_projection

DB 操作直接 await 在 pytest-asyncio 常驻循环上（与 autouse 的 sweep 夹具共享同一
循环），WS 由 TestClient 门户在独立线程驱动；两侧各用 NullPool 引擎，连接不跨
循环共享，仅以 MySQL 提交为媒介交换状态。provider 固定 mock，保证确定性。
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.routes import voice_moxiang
from app.core.config import settings
from app.core.security import create_token
from app.main import app
from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.consents import grant_consent
from app.services.ai.profile import confirm_profile_draft, publish_profile_draft
from app.workers import ai_worker

TEST_DATABASE_URL = os.getenv(
    "AI_TEST_DATABASE_URL",
    "mysql+aiomysql://root:@127.0.0.1:3307/xuanshiai_ai_test",
)

USER_ID = 9_876_543_460
CONSENT_VERSION = "profile-text-v1"
POLICY_REVISION = "ai-policy-2026-08-07-v1"

_TURNS = (
    "我住杭州，周末喜欢旅行和看展。",
    "我从事互联网技术工作，也喜欢户外活动。",
    "我想认真交往，以结婚为目标。",
    "我目前未婚，本科学历，身高一米七二。",
)


def _make_token() -> str:
    return create_token(
        user_id=USER_ID,
        session_id=1,
        token_type="access",
        expires_delta=timedelta(hours=1),
    )


def _factory():
    # NullPool：每次 checkout 现开现关，连接绝不跨事件循环复用。
    # 本测试有两个循环访问 MySQL——TestClient 门户循环（独立线程，跑 WS 处理与
    # 轮询）与 pytest-asyncio 主循环（跑 worker 与服务级校验）。aiomysql 连接绑定
    # 其创建循环，共享 QueuePool 会触发 "different loop" 错误，故用 NullPool。
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _grant_consent() -> None:
    engine, factory = _factory()
    try:
        async with factory() as db:
            await grant_consent(
                db,
                USER_ID,
                "profile_text_extract",
                AiConsentGrantRequest(
                    consent_version=CONSENT_VERSION,
                    policy_revision=POLICY_REVISION,
                ),
                "moxiang-ws-chain-grant-1",
                0,
            )
            await db.commit()
    finally:
        await engine.dispose()


def _recv_until(ws, predicate, *, limit: int = 400):
    """持续读取 WS 消息直到 predicate 命中；返回命中消息与前序全部消息。"""
    seen: list[dict] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if predicate(message):
            return message, seen
    raise AssertionError(f"未在 {limit} 条消息内等到目标；已收到类型={[m.get('type') for m in seen]}")


@pytest.mark.asyncio
async def test_ws_journey_full_chain_invite_confirm_publish_project(
    ai_test_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS 全链路：从握手到发布投影，邀请与确认卡都经协议推送。

    异步测试：所有 DB 操作直接 ``await`` 在 pytest-asyncio 的常驻循环上，与
    autouse 的 ``sweep_test_users`` 夹具共享同一循环——若写成同步用例，夹具的
    临时循环会在其结束后立即关闭，遗留的 aiomysql 连接延后 GC 时命中已关闭循环。
    WS 由 TestClient 门户在独立线程驱动，主线程用同步 send/receive 阻塞收发；
    两侧各用 NullPool 引擎，连接不跨循环共享，仅以 MySQL 提交为媒介交换状态。
    """
    monkeypatch.setattr(settings, "ai_provider", "mock")
    monkeypatch.setattr(settings, "ai_moxiang_journey_enabled", True)
    monkeypatch.setattr(settings, "ai_profile_enabled", True)
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    await _grant_consent()

    ws_engine, ws_factory = _factory()
    worker_engine, worker_factory = _factory()
    monkeypatch.setattr(voice_moxiang, "_db_session_factory", ws_factory)
    monkeypatch.setattr(ai_worker, "session_factory", worker_factory)

    client = TestClient(app)
    token = _make_token()
    try:
        with client.websocket_connect(
            f"/api/v1/voice/moxiang-master?token={token}"
        ) as ws:
            # 1) 会话建立：journey_ready + 时段化开场白（#1）。
            ws.send_json(
                {
                    "type": "session_start",
                    "mode": "moxiang_journey",
                    "subject": "personal",
                    "consentVersion": CONSENT_VERSION,
                }
            )
            ready, _ = _recv_until(ws, lambda m: m.get("type") == "journey_ready")
            assert ready["subject"] == "personal"
            opening, _ = _recv_until(
                ws, lambda m: m.get("type") == "ai_reply" and m.get("opening") is True
            )
            assert "我是知遇" in opening["text"]

            # 2) 四轮自然对话：每轮 ai_reply，抽取任务在后台入队。
            for idx, answer in enumerate(_TURNS):
                ws.send_json(
                    {
                        "type": "text_message",
                        "text": answer,
                        "clientTurnId": f"ws-chain-turn-{idx}",
                    }
                )
                _recv_until(
                    ws,
                    lambda m: m.get("type") == "ai_reply" and m.get("opening") is False,
                )

            # 3) 驱动 worker 处理候选任务；后台轮询任务随后推送进度与邀请。
            assert await _run_worker(4) == (4, 4, 0)
            invite, _ = _recv_until(ws, lambda m: m.get("type") == "build_invite")
            assert invite["subject"] == "personal"
            assert invite["dimension_count"] >= 3
            assert invite["summary_items"], "邀请摘要至少一条"
            # #19/#21：摘要项带 profile_dimension（前端映射中文小标题）。
            assert all(
                item.get("profile_dimension") for item in invite["summary_items"]
            )
            invite_id = str(invite["invite_id"])

            # 4) 接受邀请 → resolved + confirm_card。
            ws.send_json(
                {
                    "type": "build_invite_accept",
                    "subject": "personal",
                    "invite_id": invite_id,
                }
            )
            resolved, _ = _recv_until(
                ws, lambda m: m.get("type") == "build_invite_resolved"
            )
            assert resolved["resolution"] == "accepted"
            card, _ = _recv_until(ws, lambda m: m.get("type") == "confirm_card")
            assert card["draft_id"]
            assert card["items"], "确认卡至少一条待确认字段"
            draft_id = str(card["draft_id"])
            expected_revision = int(card["expected_revision"])

        # 5) 服务级确认 + 发布，再驱动投影 worker，验证六维 entry 进入投影链。
        field_keys = await _draft_structured_keys(draft_id)
        assert len(field_keys) >= 7
        published = await _confirm_and_publish(field_keys, draft_id, expected_revision)
        assert published["task_id"]
        claimed, completed, failed = await _run_worker_collect(6)
        assert claimed >= 1 and completed >= 1 and failed == 0

        kinds = await _projection_kinds()
        assert {"personal_searchable", "personal_compatibility"} <= kinds
    finally:
        await _dispose_engines(ws_engine, worker_engine)


# ----------------------------------------------------------------------
# 各异步辅助直接 await 在测试的常驻循环上；NullPool 保证连接不跨循环共享。
# ----------------------------------------------------------------------


async def _dispose_engines(*engines) -> None:
    for engine in engines:
        await engine.dispose()


async def _run_worker(batch: int):
    return await ai_worker._run_round("it-ws-chain-candidates", batch)


async def _run_worker_collect(batch: int):
    return await ai_worker._run_round("it-ws-chain-projection", batch)


async def _draft_structured_keys(draft_id: str) -> list[str]:
    engine, factory = _factory()
    try:
        async with factory() as db:
            rows = (
                await db.execute(
                    sql_text(
                        "SELECT field_key FROM ai_profile_draft_field "
                        "WHERE draft_id = :draft_id AND field_kind = 'structured' "
                        "ORDER BY field_key"
                    ),
                    {"draft_id": draft_id},
                )
            ).scalars().all()
        return [str(k) for k in rows]
    finally:
        await engine.dispose()


async def _confirm_and_publish(field_keys, draft_id: str, expected_revision: int):
    engine, factory = _factory()
    try:
        async with factory() as db:
            confirmed = await confirm_profile_draft(
                db,
                draft_id,
                USER_ID,
                [
                    ProfileDraftFieldPatchRequest(
                        field_key=key,
                        action=ProfileFieldPatchAction.CONFIRM,
                        expected_revision=expected_revision,
                    )
                    for key in field_keys
                ],
                expected_revision=expected_revision,
                idempotency_key="ws-chain-confirm-1",
            )
            published = await publish_profile_draft(
                db,
                draft_id,
                USER_ID,
                expected_revision=confirmed.revision,
                idempotency_key="ws-chain-publish-1",
            )
            await db.commit()
            return {"task_id": published.task_id}
    finally:
        await engine.dispose()


async def _projection_kinds():
    engine, factory = _factory()
    try:
        async with factory() as db:
            rows = (
                await db.execute(
                    sql_text(
                        "SELECT DISTINCT projection_kind FROM ai_feature_projection "
                        "WHERE subject_user_id = :uid AND status = 'active'"
                    ),
                    {"uid": USER_ID},
                )
            ).scalars().all()
        return {str(k) for k in rows}
    finally:
        await engine.dispose()
