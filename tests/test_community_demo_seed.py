from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_community_demo_seed_is_development_only_and_contains_main_demo_data() -> None:
    from scripts.seed_community_demo import (
        DEMO_ACTIVITY_TITLES,
        DEMO_AVAILABLE_PLANE_CONTENTS,
        DEMO_POST_DECLARATIONS,
        DEMO_POST_CONTENTS,
        DEMO_PROFILE_PHONES,
        DEMO_TOPIC_NAMES,
        seed_community_demo,
    )

    assert len(DEMO_TOPIC_NAMES) >= 4
    assert len(DEMO_ACTIVITY_TITLES) >= 3
    assert len(DEMO_PROFILE_PHONES) >= 5
    assert len(DEMO_POST_CONTENTS) >= 8
    assert len(DEMO_AVAILABLE_PLANE_CONTENTS) >= 4
    assert set(DEMO_POST_DECLARATIONS) <= {
        "",
        "内容包含虚构演绎",
        "内容包含广告推广",
        "内容可能引起不适",
    }
    with pytest.raises(RuntimeError, match="development/testing"):
        seed_community_demo(connection=object(), environment="production")


def test_community_demo_seed_contains_profile_feed_comment_and_signup_writes() -> None:
    script = (ROOT / "scripts" / "seed_community_demo.py").read_text(encoding="utf-8")

    assert "INSERT INTO user_profile" in script
    assert "INSERT INTO community_post" in script
    assert "INSERT INTO community_comment" in script
    assert "INSERT INTO activity_signup" in script
    assert "ON DUPLICATE KEY UPDATE" in script


def test_paper_plane_message_response_exposes_viewer_message_ownership() -> None:
    from app.schemas.community import PaperPlaneMessageResponse

    assert "mine" in PaperPlaneMessageResponse.model_fields


def test_community_demo_seed_uses_idempotent_writes_and_keeps_paper_plane_conversation() -> None:
    script = (ROOT / "scripts" / "seed_community_demo.py").read_text(encoding="utf-8")

    assert "INSERT INTO community_topic" in script
    assert "ON DUPLICATE KEY UPDATE" in script
    assert "config_banner" in script
    assert "link_type" in script
    assert "paper_plane_conversation" in script
    assert "paper_plane_message" in script
    assert "expire_at" in script


def test_community_demo_seed_can_import_backend_modules_when_run_as_a_script() -> None:
    script = (ROOT / "scripts" / "seed_community_demo.py").read_text(encoding="utf-8")

    assert "sys.path.insert" in script
