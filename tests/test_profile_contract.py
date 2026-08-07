import pytest
from pydantic import ValidationError

from app.schemas.auth import ProfileUpdateRequest


def test_profile_update_rejects_avatar_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ProfileUpdateRequest(avatar="/storage/uploads/1/avatar.webp")

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
