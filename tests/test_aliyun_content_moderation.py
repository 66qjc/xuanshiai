from __future__ import annotations

import pytest

from app.services import aliyun_content_moderation as moderation


def test_classify_payload_supports_boolean_hit() -> None:
    assert moderation._classify_payload({"data": {"hit": True}}) is True
    assert moderation._classify_payload({"data": {"hit": False}}) is False


def test_classify_payload_rejects_unknown_schema() -> None:
    with pytest.raises(moderation.ProviderProtocolError):
        moderation._classify_payload({"code": 200, "data": {"value": "unknown"}})


def test_extract_matched_words_deduplicates_values() -> None:
    assert moderation._extract_matched_words(
        {"data": {"matchedWords": ["one", "one", " two "]}}
    ) == ("one", "two")


@pytest.mark.asyncio
async def test_disabled_provider_does_not_make_request() -> None:
    result = await moderation.moderate_with_aliyun("sample")
    assert result.action == "allow"
    assert result.provider == "aliyun_market"
