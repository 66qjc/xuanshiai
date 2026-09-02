"""墨相师人设提示词：白名单段必须存在；构建上下文注入 system 消息。"""
from app.services.ai.prompts.moxiang_master import (
    _SYSTEM_HEADER,
    build_build_context,
    build_master_prompt,
    opening_message_for_subject,
    OPENING_MESSAGE,
    IDEAL_PARTNER_OPENING_MESSAGE,
)
from app.services.ai.prompts.profile_extract import (
    _MASTER_SYSTEM_HEADER,
    build_profile_master_extract_prompt,
)


def test_system_header_contains_topic_whitelist() -> None:
    for topic in ("自我认知", "三观", "生活方式", "物理位置", "基本情况"):
        assert topic in _SYSTEM_HEADER
    assert "拉回" in _SYSTEM_HEADER


def test_build_context_appended_as_system_message() -> None:
    ctx = build_build_context(["city_code", "age"], "用户已确认：从事设计工作", 40.0)
    messages = build_master_prompt("我在杭州做设计", [], narrative_context="", build_context=ctx)
    assert messages[0]["role"] == "system"
    assert "白名单" not in messages[1]["content"]  # build_context 是独立 system 段
    assert "城市" in messages[1]["content"]
    assert "40" in messages[1]["content"]


def test_build_context_identifies_the_current_portrait_subject() -> None:
    ctx = build_build_context(
        ["age"], "", 0.0, subject="ideal_partner"
    )

    assert "愿遇之相" in ctx
    assert "当前建构主体" in ctx


def test_master_extract_prompt_protects_specific_people_and_requires_preference() -> None:
    for phrase in ("具体对象", "明确确认", "愿遇之相", "偏好"):
        assert phrase in _MASTER_SYSTEM_HEADER


# ===== Phase 1 Contract v1.1 P1-B：subject boundary + opening message guards =====


def test_system_header_declares_two_subjects_no_cross_write() -> None:
    """Master system header must declare the personal/ideal_partner split."""
    for cue in ("我的墨相", "愿遇之相", "不"):
        assert cue in _SYSTEM_HEADER, (
            f"master system header missing subject-boundary cue: {cue!r}"
        )


def test_opening_messages_are_subject_specific() -> None:
    """``OPENING_MESSAGE`` and ``IDEAL_PARTNER_OPENING_MESSAGE`` must be distinct."""
    assert OPENING_MESSAGE != IDEAL_PARTNER_OPENING_MESSAGE
    assert opening_message_for_subject("personal") == OPENING_MESSAGE
    assert opening_message_for_subject("ideal_partner") == IDEAL_PARTNER_OPENING_MESSAGE


def test_opening_messages_do_not_leak_into_the_other_subject() -> None:
    """The two opening strings must not reference the other subject's vocabulary."""
    assert "理想" not in OPENING_MESSAGE
    assert "我自己" not in IDEAL_PARTNER_OPENING_MESSAGE


def test_build_context_personal_and_ideal_partner_are_distinct() -> None:
    """The two subject build contexts must be distinct and labelled."""
    personal = build_build_context(
        missing_hard=["age"], confirmed_summary="", percent=25.0, subject="personal"
    )
    partner = build_build_context(
        missing_hard=["age"], confirmed_summary="", percent=25.0, subject="ideal_partner"
    )
    assert "我的墨相" in personal
    assert "愿遇之相" in partner
    assert personal != partner


def test_master_extract_prompt_injects_per_subject_label() -> None:
    """Master extract prompt must label the per-subject so the model buckets correctly."""
    personal = build_profile_master_extract_prompt(
        subject="personal", turn_texts=("我性格偏内敛。",)
    )
    partner = build_profile_master_extract_prompt(
        subject="ideal_partner", turn_texts=("我希望对方开朗一些。",)
    )
    assert "个人画像" in personal
    assert "理想型画像" in partner
