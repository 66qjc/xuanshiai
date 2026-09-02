"""master 会话：创建/复用/助手回复落库（fake store）。

形态与 tests/test_ai_profile_sessions.py 一致（ProfileStore 内存假库，不依赖
真实数据库）。覆盖简报三断言：新会话 kind='master' 且 status=draft；旧 build
会话保留并标记 stale，重复进入只复用新 master；助手回复以 role='assistant'
turn 落库后可读回。
"""

from __future__ import annotations

import pytest

from app.schemas.ai_profile import ProfileSubject
from app.services.ai.profile import (
    ProfileSessionNotFound,
    create_master_session,
    persist_master_assistant_reply,
)
from tests.test_ai_profile_sessions import ProfileStore


@pytest.mark.asyncio
async def test_create_master_session_inserts_kind_master() -> None:
    store = ProfileStore()  # 构造时自动为 user 10 种子 consent
    session = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    assert session.session_kind == "master"
    assert session.status.value == "draft"
    # 落库行本身就是 kind='master'（不只返回对象上如此）。
    assert store.sessions[session.session_id]["session_kind"] == "master"


@pytest.mark.asyncio
async def test_create_master_session_replaces_legacy_then_reuses_master() -> None:
    store = ProfileStore()
    # 已有活动 build 会话（seed 行缺 session_kind，读取时默认 build）：
    # master 不得静默复用；旧行保留为 stale，后续重连复用新 master。
    await store.seed_session(
        owner_user_id=10, subject="personal", session_id="sess1"
    )
    first = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    second = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    assert first.session_id == second.session_id
    assert first.session_id != "sess1"
    assert first.session_kind == "master"
    assert store.sessions["sess1"]["status"] == "stale"
    assert store.sessions["sess1"]["active_status"] == 0
    assert len(store.sessions) == 2


@pytest.mark.asyncio
async def test_assistant_reply_persisted() -> None:
    store = ProfileStore()
    session = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    await persist_master_assistant_reply(
        store.db, session.session_id, 10, "聊得不错，继续～"
    )
    turns = [t for t in store.turns if t["session_id"] == session.session_id]
    assert any(
        t["role"] == "assistant" and t["answer_text"] == "聊得不错，继续～"
        for t in turns
    )


@pytest.mark.asyncio
async def test_assistant_reply_skips_blank() -> None:
    store = ProfileStore()
    session = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    await persist_master_assistant_reply(store.db, session.session_id, 10, "   ")
    assert store.turns == []


@pytest.mark.asyncio
async def test_assistant_reply_rejects_foreign_user() -> None:
    store = ProfileStore()
    session = await create_master_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1"
    )
    with pytest.raises(ProfileSessionNotFound):
        await persist_master_assistant_reply(
            store.db, session.session_id, 11, "你好"
        )
