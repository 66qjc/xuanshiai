"""WP-P4a update-intent 对话式追加会话单测（fake store）。

覆盖：update 会话创建（session_kind='update'、无题库、首轮 turn+task）、
唯一活动槽位纪律（已有活动会话时拒绝且不静默关闭）、澄清式 prompt 构建。
澄清分支的 handler 行为（assistant turn / patch 草稿）由真库集成测试覆盖。
"""

from __future__ import annotations

import pytest

from app.schemas.ai_profile import ProfileSubject
from app.services.ai.profile import (
    AIInputError,
    ProfileSessionStatus,
    create_profile_session,
    create_update_session,
)
from app.services.ai.prompts.profile_extract import (
    build_profile_update_clarify_prompt,
)
from tests.test_ai_profile_sessions import ProfileStore


@pytest.mark.asyncio
async def test_create_update_session_creates_update_kind_and_first_turn() -> None:
    store = ProfileStore()  # 构造时自动为 user 10 种子 consent
    session, submission = await create_update_session(
        store.db, 10, ProfileSubject.PERSONAL,
        "希望对方是艺术家，周末能一起看展", "profile-text-v1", "update-key-001",
    )
    assert session.session_kind == "update"
    assert session.current_question is None  # update 会话无题库推进
    assert session.status is ProfileSessionStatus.EXTRACTING  # 首轮已入队
    assert submission.replayed is False
    assert submission.task_id


@pytest.mark.asyncio
async def test_update_intent_rejects_when_active_session_exists() -> None:
    store = ProfileStore()
    # 已有活动 build 会话：update-intent 必须拒绝（先完成/放弃，不静默关闭）。
    await create_profile_session(
        store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "build-key-001"
    )
    with pytest.raises(AIInputError):
        await create_update_session(
            store.db, 10, ProfileSubject.PERSONAL,
            "希望对方是艺术家", "profile-text-v1", "update-key-002",
        )
    # 已有活动 update 会话：第二次 update-intent 同样拒绝。
    store2 = ProfileStore()
    await create_update_session(
        store2.db, 10, ProfileSubject.PERSONAL,
        "希望对方爱运动", "profile-text-v1", "update-key-003",
    )
    with pytest.raises(AIInputError):
        await create_update_session(
            store2.db, 10, ProfileSubject.PERSONAL,
            "希望对方会做饭", "profile-text-v1", "update-key-004",
        )


def test_update_clarify_prompt_carries_contract_and_digest() -> None:
    prompt = build_profile_update_clarify_prompt(
        "ideal_partner",
        ("希望对方是艺术家", "偏向看展和摄影"),
        entry_digest="entry_values_seed01｜价值观：欣赏踏实上进的人",
    )
    assert "clarifying_question" in prompt
    assert "patches" in prompt
    assert "replaces_field_key" in prompt
    assert "entry_values_seed01｜价值观" in prompt  # modify 目标可定位
    assert "希望对方是艺术家" in prompt
    assert "禁止编造" in prompt  # faithfulness 硬约束
    # 无条目 digest：modify 定位块不出现（首次发布用户也可能发起更新）。
    plain = build_profile_update_clarify_prompt("personal", ("最近开始健身",))
    assert "replaces_field_key" in plain
