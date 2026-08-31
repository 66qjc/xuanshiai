"""墨相师人设提示词：白名单段必须存在；构建上下文注入 system 消息。"""
from app.services.ai.prompts.moxiang_master import (
    _SYSTEM_HEADER,
    build_build_context,
    build_master_prompt,
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
