from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from app.schemas.auth import NicknameUpdateRequest, PhotoOrderRequest, PreferenceUpdateRequest, ProfileUpdateRequest
from app.services.profile import COMPLETION_RULES, IMAGE_MAX_PIXELS, _image_outputs


def test_completion_weights_total_100() -> None:
    assert sum(weight for _, _, weight in COMPLETION_RULES) == 100
    assert {key for key, _, _ in COMPLETION_RULES} >= {"weight", "hometown", "album"}


def test_profile_validates_mbti_height_and_tags() -> None:
    request = ProfileUpdateRequest(
        height=175,
        is_married=1,
        mbti="INTJ",
        interest_tags=["健身", "旅行", "摄影"],
        personality_tags=["内向但真诚", "温柔细心", "独立自信"],
        tag_selections={"sports": ["健身", "跑步"], "city": ["上海"]},
    )
    assert request.mbti == "INTJ"
    assert request.tag_selections["sports"] == ["健身", "跑步"]

    with pytest.raises(ValidationError):
        ProfileUpdateRequest(height=139)
    assert ProfileUpdateRequest(height=140, weight=40, self_intro="x" * 500).height == 140
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(weight=39)
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(mbti="XXXX")
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(interest_tags=["只有一个"])
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(tag_selections={"sports": ["自定义标签"]})


def test_nickname_update_trims_and_rejects_blank_values() -> None:
    assert NicknameUpdateRequest(nickname="  小明  ").nickname == "小明"
    with pytest.raises(ValidationError):
        NicknameUpdateRequest(nickname="   ")
    with pytest.raises(ValidationError):
        NicknameUpdateRequest(nickname="x" * 65)


def test_preference_ranges_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        PreferenceUpdateRequest(age_min=35, age_max=25)
    with pytest.raises(ValidationError):
        PreferenceUpdateRequest(height_min=180, height_max=160)


def test_photo_order_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        PhotoOrderRequest(media_ids=[1, 1])


def test_image_outputs_are_webp_and_have_thumbnail() -> None:
    source = BytesIO()
    Image.new("RGB", (1200, 800), "white").save(source, format="PNG")

    image_data, thumbnail_data = _image_outputs(source.getvalue())

    with Image.open(BytesIO(image_data)) as image:
        assert image.format == "WEBP"
    with Image.open(BytesIO(thumbnail_data)) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert max(thumbnail.size) <= 480


def test_image_pixel_limit_is_explicit() -> None:
    assert IMAGE_MAX_PIXELS == 25_000_000
