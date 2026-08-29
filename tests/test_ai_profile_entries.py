"""WP-P1b entry 条目链路单测（fake store）。

覆盖：content 校验边界、ExtractedEntry 出参契约（9 分类枚举/200 字/确认纪律）、
entry 确认/编辑/删除动作路由、发布门槛不计入 entry（Global Constraint 防回归）。
structured 链路的行为由既有 test_ai_profile_publish 全量保证，此处不重复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.ai_profile import (
    PROFILE_ENTRY_CATEGORIES,
    PROFILE_ENTRY_CONTENT_MAX_LENGTH,
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.base import ExtractedEntry
from app.services.ai.profile import (
    AIInputError,
    confirm_profile_draft,
    load_owned_draft,
    publish_profile_draft,
    validate_entry_content,
)
from tests.test_ai_profile_publish import ProfileStore
from tests.test_ai_profile_sessions import _now


@pytest.fixture
def profile_store() -> ProfileStore:
    """与 test_ai_profile_publish 同构的 store fixture（fixture 不跨模块）。"""
    return ProfileStore()


# ----------------------------------------------------------------------
# 纯校验函数
# ----------------------------------------------------------------------


def test_validate_entry_content_boundaries() -> None:
    assert validate_entry_content("  欣赏阳光开朗的人  ") == "欣赏阳光开朗的人"
    assert validate_entry_content("x" * PROFILE_ENTRY_CONTENT_MAX_LENGTH) == "x" * 200
    with pytest.raises(AIInputError):
        validate_entry_content("x" * (PROFILE_ENTRY_CONTENT_MAX_LENGTH + 1))
    with pytest.raises(AIInputError):
        validate_entry_content("")
    with pytest.raises(AIInputError):
        validate_entry_content("   ")
    with pytest.raises(AIInputError):
        validate_entry_content(123)


def test_extracted_entry_schema_contract() -> None:
    entry = ExtractedEntry(
        category="values",
        content="欣赏阳光开朗、品行端正的人",
        subject=ProfileSubject.PERSONAL,
        source_quote="我喜欢阳光开朗品行端正的",
        confidence=0.88,
    )
    assert entry.category in PROFILE_ENTRY_CATEGORIES
    # source_span 缺省回填为 source_quote（与 ExtractedField 同纪律）。
    assert entry.source_span == "我喜欢阳光开朗品行端正的"

    with pytest.raises(ValidationError):
        ExtractedEntry(
            category="not_a_category",
            content="任意内容",
            subject=ProfileSubject.PERSONAL,
        )
    with pytest.raises(ValidationError):
        ExtractedEntry(
            category="values",
            content="x" * (PROFILE_ENTRY_CONTENT_MAX_LENGTH + 1),
            subject=ProfileSubject.PERSONAL,
        )
    with pytest.raises(ValidationError):
        ExtractedEntry(
            category="values",
            content="   ",
            subject=ProfileSubject.PERSONAL,
        )
    # provider 产物必须等待用户确认。
    with pytest.raises(ValidationError):
        ExtractedEntry(
            category="values",
            content="任意内容",
            subject=ProfileSubject.PERSONAL,
            needs_confirmation=False,
        )


# ----------------------------------------------------------------------
# fake store：确认/编辑/删除与发布门槛
# ----------------------------------------------------------------------


def _entry_row(
    draft_id: str,
    field_key: str,
    *,
    subject: str = "personal",
    category: str = "values",
    content: str = "欣赏阳光开朗、品行端正的人",
    status: str = "suggested",
) -> dict[str, Any]:
    now: datetime = _now()
    return {
        "draft_id": draft_id,
        "field_key": field_key,
        "subject": subject,
        "field_kind": "entry",
        "category": category,
        "content": content,
        "replaces_field_key": None,
        "value": None,
        "value_json": "null",
        "display_value": content,
        "source_type": "user_answer",
        "source_turn_ids": '["turn-001"]',
        "source_span": "我喜欢阳光开朗品行端正的",
        "confidence": 0.88,
        "visibility": "self",
        "consent_scope": "profile_text_extract",
        "schema_version": "profile-extract-v1",
        "prompt_version": "profile-extract-prompt-v1",
        "content_hash": f"hash-{field_key}",
        "confirmation_status": status,
        "created_at": now,
        "updated_at": now,
    }


def _field_row_by_key(store: ProfileStore, draft_id: str, field_key: str) -> dict[str, Any]:
    for row in store.draft_fields:
        if row["draft_id"] == draft_id and row["field_key"] == field_key:
            return row
    raise AssertionError(f"field {field_key} not found in draft {draft_id}")


@pytest.mark.asyncio
async def test_entry_confirm_edit_delete_via_patch_actions(profile_store) -> None:
    store: ProfileStore = profile_store
    draft = await store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[
            {"field_key": "city_code", "value": "330100", "status": "suggested"},
        ],
        revision=1,
    )
    draft_id = draft["draft_id"]
    entry_key = "entry_values_ab12cd34"
    store.draft_fields.append(_entry_row(draft_id, entry_key))

    # 确认条目：entry key 不在 structured allowlist，但存在于草稿即可确认。
    await confirm_profile_draft(
        store.session,
        draft_id,
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key=entry_key,
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            )
        ],
        expected_revision=1,
    )
    row = _field_row_by_key(store, draft_id, entry_key)
    assert row["confirmation_status"] == "confirmed"

    # 编辑条目：REPLACE 携带新 content（≤200 字），status 转 confirmed。
    old_hash = row["content_hash"]
    new_content = "偏好有共同话题、能一起旅行的人"
    await confirm_profile_draft(
        store.session,
        draft_id,
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key=entry_key,
                action=ProfileFieldPatchAction.REPLACE,
                value=new_content,
                expected_revision=2,
            )
        ],
        expected_revision=2,
    )
    row = _field_row_by_key(store, draft_id, entry_key)
    assert row["content"] == new_content
    assert row["display_value"] == new_content
    assert row["confirmation_status"] == "confirmed"
    assert row["content_hash"] != old_hash

    # 201 字编辑被拒绝（AI_INPUT_INVALID）。
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            store.session,
            draft_id,
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key=entry_key,
                    action=ProfileFieldPatchAction.REPLACE,
                    value="x" * (PROFILE_ENTRY_CONTENT_MAX_LENGTH + 1),
                    expected_revision=3,
                )
            ],
            expected_revision=3,
        )

    # 删除条目：复用 confirmation_status 状态机，标记 deleted。
    await confirm_profile_draft(
        store.session,
        draft_id,
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key=entry_key,
                action=ProfileFieldPatchAction.DELETE,
                expected_revision=3,
            )
        ],
        expected_revision=3,
    )
    assert _field_row_by_key(store, draft_id, entry_key)["confirmation_status"] == "deleted"

    # 草稿里的未知 entry key（不存在）依旧拒绝。
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            store.session,
            draft_id,
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="entry_values_doesnot1",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=4,
                )
            ],
            expected_revision=4,
        )
    updated = await load_owned_draft(store.session, draft_id, 10)
    assert updated.revision == 4


@pytest.mark.asyncio
async def test_publish_gate_ignores_confirmed_entries(profile_store) -> None:
    store: ProfileStore = profile_store
    six_structured_plus_one = [
        {"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"},
        {"field_key": "city_code", "value": "330100", "status": "confirmed"},
        {"field_key": "occupation_group", "value": "technology", "status": "confirmed"},
        {"field_key": "education_level", "value": 4, "status": "confirmed"},
        {"field_key": "height_cm", "value": 175, "status": "confirmed"},
        {"field_key": "income_band", "value": "high", "status": "confirmed"},
        {"field_key": "marriage_status", "value": "single", "status": "suggested"},
    ]
    draft = await store.seed_draft(
        owner_user_id=20, subject="personal", fields=six_structured_plus_one, revision=0
    )
    draft_id = draft["draft_id"]
    for idx, category in enumerate(("values", "interests", "life_plan")):
        store.draft_fields.append(
            _entry_row(
                draft_id,
                f"entry_{category}_0000000{idx}",
                category=category,
                content=f"条目内容 {idx}",
                status="confirmed",
            )
        )
    # 6 个 structured 确认 + 3 个 entry 确认：门槛(默认 7)仍不满足——entry 不计入。
    with pytest.raises(AIInputError):
        await publish_profile_draft(
            store.session, draft_id, 20, expected_revision=0, idempotency_key="pub-entry-gate"
        )
    # 确认第 7 个 structured 字段后发布成立（entry 依旧计入发布内容但不计门槛）。
    await confirm_profile_draft(
        store.session,
        draft_id,
        20,
        [
            ProfileDraftFieldPatchRequest(
                field_key="marriage_status",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=0,
            )
        ],
        expected_revision=0,
    )
    await publish_profile_draft(
        store.session, draft_id, 20, expected_revision=1, idempotency_key="pub-entry-ok"
    )


# ----------------------------------------------------------------------
# T4：entry_digest 摘要与叙事 serialize
# ----------------------------------------------------------------------


def test_serialize_fields_for_prompt_includes_entries() -> None:
    from app.services.ai.prompts.profile_narrative import serialize_fields_for_prompt

    rows = [
        {"field_key": "height_cm", "value_json": "175", "display_value": "175"},
        {
            "field_key": "entry_values_x",
            "value_json": None,
            "display_value": "欣赏阳光开朗、品行端正的人",
            "field_kind": "entry",
            "category": "values",
            "content": "欣赏阳光开朗、品行端正的人",
        },
        {"field_key": "entry_blank", "field_kind": "entry", "category": "diet", "content": ""},
    ]
    result = serialize_fields_for_prompt(rows)
    assert result[0]["field_key"] == "height_cm"
    entry_line = result[1]
    assert entry_line["field_key"] == "entry_values"
    assert "条目·价值观" in entry_line["display_value"]
    assert "欣赏阳光开朗" in entry_line["display_value"]
    # 空正文条目不进 prompt。
    assert all(item["field_key"] != "entry_diet" for item in result)


def test_build_entry_digest_lines_and_empty() -> None:
    from app.services.ai.features import build_entry_digest

    rows = [
        {"field_kind": "structured", "field_key": "height_cm", "content": None},
        {
            "field_kind": "entry",
            "category": "values",
            "content": "欣赏阳光开朗、品行端正的人",
        },
        {"field_kind": "entry", "category": "interests", "content": "周末旅行与看展"},
        {"field_kind": "entry", "category": "diet", "content": "   "},
    ]
    digest = build_entry_digest(rows)
    assert digest is not None
    assert "价值观：欣赏阳光开朗、品行端正的人" in digest
    assert "兴趣爱好：周末旅行与看展" in digest
    assert "饮食习惯" not in digest
    assert "height_cm" not in digest
    # 纯 structured 用户：摘要为 NULL（回归保护）。
    assert build_entry_digest([{"field_kind": "structured", "content": "x"}]) is None
