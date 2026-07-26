"""Community media upload, ownership and publish binding."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "database_setup_marriage.py").read_text(encoding="utf-8")


def test_community_media_tables_are_defined() -> None:
    assert "CREATE TABLE IF NOT EXISTS `community_media`" in SETUP
    assert "CREATE TABLE IF NOT EXISTS `community_media_attachment`" in SETUP
    assert "`purpose` varchar(32)" in SETUP
    assert "`status` varchar(16)" in SETUP
    assert "target_type" in SETUP
    assert "media_id" in SETUP


def test_community_media_routes_registered() -> None:
    from app.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/community/media/uploads" in paths
    assert "/api/v1/community/media/{media_id}" in paths


def test_community_media_response_shape() -> None:
    from app.schemas.community import CommunityMediaResponse

    item = CommunityMediaResponse(
        id=1,
        purpose="post",
        media_type="image",
        url="/storage/uploads/1/community/a.webp",
        thumbnail_url="/storage/uploads/1/community/a-thumb.webp",
        file_size=123,
        duration_seconds=None,
        status="ready",
    )
    assert item.purpose == "post"
    assert item.status == "ready"


def test_community_post_create_accepts_media_ids_and_media_only() -> None:
    from app.schemas.community import CommunityPostCreate

    payload = CommunityPostCreate(content="", image_media_ids=[1, 2])
    assert payload.image_media_ids == [1, 2]
    assert payload.content == ""


def test_paper_plane_create_accepts_image_media_ids() -> None:
    from app.schemas.community import PaperPlaneCreate

    payload = PaperPlaneCreate(content="", image_media_ids=[9])
    assert payload.image_media_ids == [9]
