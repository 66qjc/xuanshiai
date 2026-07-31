"""Community media upload, ownership and publish binding."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "database_setup_marriage.py").read_text(encoding="utf-8")


def _tiny_png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _tiny_jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1, 1), (0, 255, 0)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_community_media_tables_are_defined() -> None:
    assert "CREATE TABLE IF NOT EXISTS `community_media`" in SETUP
    assert "CREATE TABLE IF NOT EXISTS `community_media_attachment`" in SETUP
    assert "`purpose` varchar(32)" in SETUP
    assert "`status` varchar(16)" in SETUP
    assert "target_type" in SETUP
    assert "media_id" in SETUP


def test_community_media_routes_registered() -> None:
    from app.api.routes import community

    paths = {route.path for route in community.router.routes}
    assert "/community/media/uploads" in paths
    assert "/community/media/{media_id}" in paths


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


def test_image_media_ids_reject_non_positive() -> None:
    from app.schemas.community import CommunityPostCreate, PaperPlaneCreate

    with pytest.raises(ValidationError):
        CommunityPostCreate(content="x", image_media_ids=[0])
    with pytest.raises(ValidationError):
        CommunityPostCreate(content="x", image_media_ids=[-1])
    with pytest.raises(ValidationError):
        PaperPlaneCreate(content="x", image_media_ids=[0])


def test_image_outputs_accepts_jpeg_and_png() -> None:
    from app.services.community_media import _image_outputs

    webp, thumb = _image_outputs(_tiny_png_bytes())
    assert webp[:4] == b"RIFF"
    assert thumb[:4] == b"RIFF"

    webp2, thumb2 = _image_outputs(_tiny_jpeg_bytes())
    assert webp2[:4] == b"RIFF"
    assert thumb2[:4] == b"RIFF"


def test_image_outputs_rejects_non_image() -> None:
    from app.services.community_media import _image_outputs

    with pytest.raises(HTTPException) as exc:
        _image_outputs(b"not-an-image")
    assert exc.value.status_code == 415


def _media_db_for_image_insert(*, media_id: int = 42, purpose: str = "post") -> AsyncMock:
    insert_result = SimpleNamespace(lastrowid=media_id)
    select_mappings = MagicMock()
    select_mappings.one.return_value = {
        "id": media_id,
        "purpose": purpose,
        "media_type": "image",
        "file_url": f"/storage/uploads/7/community/{media_id}.webp",
        "thumbnail_url": f"/storage/uploads/7/community/{media_id}-thumb.webp",
        "file_size": 100,
        "duration_seconds": None,
        "status": "ready",
    }
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[insert_result, select_result])
    db.commit = AsyncMock()
    return db


def _prepare_upload_root(monkeypatch, media_svc) -> Path:
    import shutil

    upload_root = ROOT / ".pytest_media_upload"
    if upload_root.exists():
        shutil.rmtree(upload_root, ignore_errors=True)
    upload_root.mkdir(exist_ok=True)
    monkeypatch.setattr(media_svc.settings, "upload_dir", str(upload_root))
    return upload_root


@pytest.mark.asyncio
async def test_upload_paper_plane_rejects_video_like_file(monkeypatch) -> None:
    from app.services import community_media as media_svc

    _prepare_upload_root(monkeypatch, media_svc)
    db = AsyncMock()
    upload = UploadFile(
        filename="clip.mp4",
        file=BytesIO(b"fake-video"),
        headers={"content-type": "video/mp4"},
    )
    with pytest.raises(HTTPException) as exc:
        await media_svc.upload_community_media(db, user_id=1, file=upload, purpose="paper_plane")
    assert exc.value.status_code == 422
    assert "纸飞机" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_upload_rejects_invalid_purpose(monkeypatch) -> None:
    from app.services import community_media as media_svc

    _prepare_upload_root(monkeypatch, media_svc)
    db = AsyncMock()
    upload = UploadFile(
        filename="a.png",
        file=BytesIO(_tiny_png_bytes()),
        headers={"content-type": "image/png"},
    )
    with pytest.raises(HTTPException) as exc:
        await media_svc.upload_community_media(db, user_id=1, file=upload, purpose="story")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_upload_small_png_succeeds(monkeypatch) -> None:
    import shutil

    from app.services import community_media as media_svc

    upload_root = _prepare_upload_root(monkeypatch, media_svc)
    db = _media_db_for_image_insert(media_id=42, purpose="post")

    upload = UploadFile(
        filename="tiny.png",
        file=BytesIO(_tiny_png_bytes()),
        headers={"content-type": "image/png"},
    )
    resp = await media_svc.upload_community_media(db, user_id=7, file=upload, purpose="post")
    assert resp.id == 42
    assert resp.status == "ready"
    assert resp.media_type == "image"
    assert db.commit.await_count == 1
    written = list((upload_root / "7" / "community").glob("*.webp"))
    assert len(written) == 2
    shutil.rmtree(upload_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_upload_valid_jpeg_succeeds(monkeypatch) -> None:
    """最小合法 JPEG 经 upload_community_media 落盘为 webp 并返回 ready。"""
    import shutil

    from app.services import community_media as media_svc

    upload_root = _prepare_upload_root(monkeypatch, media_svc)
    db = _media_db_for_image_insert(media_id=55, purpose="post")
    upload = UploadFile(
        filename="tiny.jpg",
        file=BytesIO(_tiny_jpeg_bytes()),
        headers={"content-type": "image/jpeg"},
    )
    resp = await media_svc.upload_community_media(db, user_id=7, file=upload, purpose="post")
    assert resp.id == 55
    assert resp.status == "ready"
    assert resp.media_type == "image"
    written = list((upload_root / "7" / "community").glob("*.webp"))
    assert len(written) == 2
    shutil.rmtree(upload_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_upload_forged_image_content_type_returns_415(monkeypatch) -> None:
    """content_type 声称 image/png，body 为伪造字节 → 415。"""
    from app.services import community_media as media_svc

    _prepare_upload_root(monkeypatch, media_svc)
    db = AsyncMock()
    upload = UploadFile(
        filename="fake.png",
        file=BytesIO(b"not-an-image"),
        headers={"content-type": "image/png"},
    )
    with pytest.raises(HTTPException) as exc:
        await media_svc.upload_community_media(db, user_id=1, file=upload, purpose="post")
    assert exc.value.status_code == 415
    assert db.execute.await_count == 0


@pytest.mark.asyncio
async def test_upload_image_oversize_returns_413(monkeypatch) -> None:
    """超过 IMAGE_MAX_BYTES 的图片 payload → 413（mock 小阈值）。"""
    from app.services import community_media as media_svc

    _prepare_upload_root(monkeypatch, media_svc)
    monkeypatch.setattr(media_svc, "IMAGE_MAX_BYTES", 64)
    db = AsyncMock()
    # 合法 JPEG 但大于 mock 上限 64 字节
    payload = _tiny_jpeg_bytes()
    assert len(payload) > 64
    upload = UploadFile(
        filename="big.jpg",
        file=BytesIO(payload),
        headers={"content-type": "image/jpeg"},
    )
    with pytest.raises(HTTPException) as exc:
        await media_svc.upload_community_media(db, user_id=1, file=upload, purpose="post")
    assert exc.value.status_code == 413
    assert "MB" in str(exc.value.detail) or "大小" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_probe_video_missing_ffprobe_returns_503(monkeypatch) -> None:
    from app.services import community_media as media_svc

    monkeypatch.setattr(media_svc.shutil, "which", lambda _name: None)
    with pytest.raises(HTTPException) as exc:
        await media_svc._probe_video(Path("missing.mp4"))
    assert exc.value.status_code == 503
    assert "ffprobe" in str(exc.value.detail).lower() or "视频" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_probe_video_rejects_non_mp4(monkeypatch) -> None:
    """ffprobe 返回非 mp4 容器名 → 415。"""
    from app.services import community_media as media_svc

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            payload = json.dumps(
                {"format": {"format_name": "matroska,webm", "duration": "5.0"}}
            ).encode("utf-8")
            return payload, b""

        def kill(self) -> None:
            return None

    async def fake_exec(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(media_svc.shutil, "which", lambda _name: "ffprobe")
    monkeypatch.setattr(media_svc.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(HTTPException) as exc:
        await media_svc._probe_video(Path("clip.webm"))
    assert exc.value.status_code == 415
    assert "MP4" in str(exc.value.detail) or "mp4" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_upload_video_oversize_returns_413(monkeypatch) -> None:
    """视频边读边写超过 VIDEO_MAX_BYTES → 413。"""
    from app.services import community_media as media_svc

    upload_root = _prepare_upload_root(monkeypatch, media_svc)
    monkeypatch.setattr(media_svc, "VIDEO_MAX_BYTES", 32)
    db = AsyncMock()
    upload = UploadFile(
        filename="clip.mp4",
        file=BytesIO(b"x" * 64),
        headers={"content-type": "video/mp4"},
    )
    with pytest.raises(HTTPException) as exc:
        await media_svc.upload_community_media(db, user_id=3, file=upload, purpose="post")
    assert exc.value.status_code == 413
    assert "50MB" in str(exc.value.detail) or "视频" in str(exc.value.detail)
    # temp file cleaned in finally
    leftovers = list((upload_root / "3" / "community").glob("video-*.upload"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_upload_post_video_with_monkeypatched_probe(monkeypatch) -> None:
    """post 用途视频：monkeypatch _probe_video 返回 10 秒，跳过真实 ffprobe。"""
    import shutil

    from app.services import community_media as media_svc

    upload_root = _prepare_upload_root(monkeypatch, media_svc)

    async def fake_probe(_path: Path) -> int:
        return 10

    monkeypatch.setattr(media_svc, "_probe_video", fake_probe)

    insert_result = SimpleNamespace(lastrowid=77)
    select_mappings = MagicMock()
    select_mappings.one.return_value = {
        "id": 77,
        "purpose": "post",
        "media_type": "video",
        "file_url": "/storage/uploads/3/community/v.mp4",
        "thumbnail_url": None,
        "file_size": 12,
        "duration_seconds": 10,
        "status": "ready",
    }
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[insert_result, select_result])
    db.commit = AsyncMock()

    upload = UploadFile(
        filename="clip.mp4",
        file=BytesIO(b"fake-mp4-bytes"),
        headers={"content-type": "video/mp4"},
    )
    resp = await media_svc.upload_community_media(db, user_id=3, file=upload, purpose="post")
    assert resp.id == 77
    assert resp.media_type == "video"
    assert resp.duration_seconds == 10
    assert resp.status == "ready"
    written = list((upload_root / "3" / "community").glob("*.mp4"))
    assert len(written) == 1
    shutil.rmtree(upload_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_delete_missing_returns_404() -> None:
    from app.services import community_media as media_svc

    select_mappings = MagicMock()
    select_mappings.first.return_value = None
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings

    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    with pytest.raises(HTTPException) as exc:
        await media_svc.delete_community_media(db, user_id=1, media_id=99)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_bound_returns_409() -> None:
    from app.services import community_media as media_svc

    select_mappings = MagicMock()
    select_mappings.first.return_value = {
        "id": 5,
        "status": "bound",
        "storage_key": None,
        "thumbnail_url": None,
    }
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings

    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    with pytest.raises(HTTPException) as exc:
        await media_svc.delete_community_media(db, user_id=1, media_id=5)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_bind_media_rejects_non_ready() -> None:
    from app.services import community_media as media_svc

    select_mappings = MagicMock()
    select_mappings.first.return_value = {"id": 3, "status": "bound"}
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings

    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    with pytest.raises(HTTPException) as exc:
        await media_svc.bind_media(db, media_ids=[3], target_type="post", target_id=10)
    assert exc.value.status_code == 409
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_bind_media_rejects_missing() -> None:
    from app.services import community_media as media_svc

    select_mappings = MagicMock()
    select_mappings.first.return_value = None
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings

    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    with pytest.raises(HTTPException) as exc:
        await media_svc.bind_media(db, media_ids=[9], target_type="post", target_id=1)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_bind_media_ready_to_bound_ok() -> None:
    from app.services import community_media as media_svc

    lock_mappings = MagicMock()
    lock_mappings.first.return_value = {"id": 8, "status": "ready"}
    lock_result = MagicMock()
    lock_result.mappings.return_value = lock_mappings

    insert_result = MagicMock()
    update_result = SimpleNamespace(rowcount=1)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[lock_result, insert_result, update_result])

    await media_svc.bind_media(db, media_ids=[8], target_type="post", target_id=100)
    assert db.execute.await_count == 3


@pytest.mark.asyncio
async def test_bind_media_fails_when_update_rowcount_zero() -> None:
    from app.services import community_media as media_svc

    lock_mappings = MagicMock()
    lock_mappings.first.return_value = {"id": 8, "status": "ready"}
    lock_result = MagicMock()
    lock_result.mappings.return_value = lock_mappings

    insert_result = MagicMock()
    update_result = SimpleNamespace(rowcount=0)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[lock_result, insert_result, update_result])

    with pytest.raises(HTTPException) as exc:
        await media_svc.bind_media(db, media_ids=[8], target_type="post", target_id=100)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_owned_ready_media_dedupes_preserving_order() -> None:
    from app.services import community_media as media_svc

    rows = [
        {"id": 2, "status": "ready", "purpose": "post"},
        {"id": 1, "status": "ready", "purpose": "post"},
    ]
    mappings = MagicMock()
    mappings.all.return_value = rows
    result = MagicMock()
    result.mappings.return_value = mappings

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    ordered = await media_svc.resolve_owned_ready_media(
        db, user_id=1, media_ids=[2, 1, 2, 1], purpose="post"
    )
    assert [r["id"] for r in ordered] == [2, 1]
    call_params = db.execute.await_args.args[1]
    assert set(k for k in call_params if k.startswith("id")) == {"id0", "id1"}


def test_post_schema_rejects_image_and_video_together() -> None:
    from app.schemas.community import CommunityPostCreate

    with pytest.raises(ValidationError):
        CommunityPostCreate(content="x", image_media_ids=[1], video_media_id=2)


def test_post_schema_allows_media_only() -> None:
    from app.schemas.community import CommunityPostCreate

    item = CommunityPostCreate(content="", image_media_ids=[1, 2])
    assert item.content == ""
    assert item.image_media_ids == [1, 2]


def test_paper_plane_schema_limits_images() -> None:
    from app.schemas.community import PaperPlaneCreate

    with pytest.raises(ValidationError):
        PaperPlaneCreate(content="hi", image_media_ids=[1, 2, 3, 4, 5, 6, 7])


@pytest.mark.asyncio
async def test_assert_owned_media_urls_rejects_external_and_temp_schemes() -> None:
    from app.services import community_media as media_svc

    db = AsyncMock()
    forbidden = [
        "wxfile://tmp/a.jpg",
        "file:///C:/Users/a.jpg",
        "temp://cache/a.jpg",
        "http://evil.example/a.jpg",
        "https://cdn.example/a.jpg",
        "C:\\Users\\ASUS\\a.jpg",
        "/etc/passwd",
        "/tmp/upload.webp",
    ]
    for url in forbidden:
        with pytest.raises(HTTPException) as exc:
            await media_svc.assert_owned_media_urls(
                db, user_id=1, urls=[url], purpose="post", media_type="image"
            )
        assert exc.value.status_code == 422
        assert "已上传" in str(exc.value.detail)
    assert db.execute.await_count == 0


@pytest.mark.asyncio
async def test_assert_owned_media_urls_accepts_owned_storage_url() -> None:
    from app.services import community_media as media_svc

    url = "/storage/uploads/3/community/a.webp"
    select_mappings = MagicMock()
    select_mappings.first.return_value = {
        "id": 11,
        "file_url": url,
        "status": "ready",
        "purpose": "post",
        "media_type": "image",
        "user_id": 3,
    }
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings
    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    rows = await media_svc.assert_owned_media_urls(
        db, user_id=3, urls=[url], purpose="post", media_type="image"
    )
    assert len(rows) == 1
    assert rows[0]["id"] == 11
    assert rows[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_assert_owned_media_urls_rejects_unowned_url() -> None:
    from app.services import community_media as media_svc

    select_mappings = MagicMock()
    select_mappings.first.return_value = None
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings
    db = AsyncMock()
    db.execute = AsyncMock(return_value=select_result)

    with pytest.raises(HTTPException) as exc:
        await media_svc.assert_owned_media_urls(
            db,
            user_id=1,
            urls=["/storage/uploads/9/community/other.webp"],
            purpose="post",
            media_type="image",
        )
    assert exc.value.status_code == 422


def _patch_content_filter(monkeypatch) -> AsyncMock:
    """Ensure content_filter imports and stub assert_text_allowed without swallowing ImportError."""
    from app.services import content_filter as content_filter_mod

    stub = AsyncMock()
    monkeypatch.setattr(content_filter_mod, "assert_text_allowed", stub)
    return stub


@pytest.mark.asyncio
async def test_create_post_with_content_imports_content_filter(monkeypatch) -> None:
    """Non-empty content must import content_filter without ModuleNotFoundError."""
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc
    from app.services import content_filter as content_filter_mod

    assert hasattr(content_filter_mod, "assert_text_allowed")
    filter_stub = _patch_content_filter(monkeypatch)

    insert_result = SimpleNamespace(lastrowid=12)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=insert_result)
    db.commit = AsyncMock()

    async def fake_get_post(db, user_id, post_id):
        return SimpleNamespace(id=post_id, user_id=user_id, images=[], video=None, content="hello")

    monkeypatch.setattr(community_svc, "get_post", fake_get_post)

    post = await community_svc.create_post(
        db,
        user_id=1,
        request=CommunityPostCreate(content="hello"),
        commit=True,
    )
    assert post.id == 12
    filter_stub.assert_awaited()
    assert filter_stub.await_args.args[1] == "hello"


@pytest.mark.asyncio
async def test_create_post_binds_image_media_ids(monkeypatch) -> None:
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc
    from app.services import community_media as media_svc

    resolve_calls: list[dict] = []
    bind_calls: list[dict] = []

    async def fake_resolve(db, user_id, media_ids, *, purpose, media_type=None):
        resolve_calls.append(
            {
                "user_id": user_id,
                "media_ids": list(media_ids),
                "purpose": purpose,
                "media_type": media_type,
            }
        )
        return [
            {"id": 5, "file_url": "/storage/uploads/7/community/a.webp", "status": "ready"},
            {"id": 6, "file_url": "/storage/uploads/7/community/b.webp", "status": "ready"},
        ]

    async def fake_bind(db, *, media_ids, target_type, target_id):
        bind_calls.append(
            {
                "media_ids": list(media_ids),
                "target_type": target_type,
                "target_id": target_id,
            }
        )

    async def fake_get_post(db, user_id, post_id):
        return SimpleNamespace(
            id=post_id,
            user_id=user_id,
            images=["/storage/uploads/7/community/a.webp", "/storage/uploads/7/community/b.webp"],
            video=None,
            content="",
        )

    insert_result = SimpleNamespace(lastrowid=100)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=insert_result)
    db.commit = AsyncMock()

    monkeypatch.setattr(media_svc, "resolve_owned_ready_media", fake_resolve)
    monkeypatch.setattr(media_svc, "bind_media", fake_bind)
    monkeypatch.setattr(community_svc, "resolve_owned_ready_media", fake_resolve)
    monkeypatch.setattr(community_svc, "bind_media", fake_bind)
    monkeypatch.setattr(community_svc, "get_post", fake_get_post)
    _patch_content_filter(monkeypatch)

    request = CommunityPostCreate(content="", image_media_ids=[5, 6])
    post = await community_svc.create_post(db, user_id=7, request=request, commit=True)

    assert post.id == 100
    assert resolve_calls and resolve_calls[0]["purpose"] == "post"
    assert resolve_calls[0]["media_type"] == "image"
    assert bind_calls == [{"media_ids": [5, 6], "target_type": "post", "target_id": 100}]
    insert_params = db.execute.await_args_list[0].args[1]
    assert json.loads(insert_params["images"]) == [
        "/storage/uploads/7/community/a.webp",
        "/storage/uploads/7/community/b.webp",
    ]
    assert insert_params["video"] is None
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_create_post_binds_video_media_id(monkeypatch) -> None:
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc
    from app.services import community_media as media_svc

    resolve_mock = AsyncMock(
        return_value=[
            {
                "id": 88,
                "file_url": "/storage/uploads/3/community/v.mp4",
                "status": "ready",
                "media_type": "video",
            }
        ]
    )
    bind_mock = AsyncMock()

    async def fake_get_post(db, user_id, post_id):
        return SimpleNamespace(
            id=post_id,
            user_id=user_id,
            images=[],
            video="/storage/uploads/3/community/v.mp4",
            content="clip",
        )

    insert_result = SimpleNamespace(lastrowid=200)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=insert_result)
    db.commit = AsyncMock()

    monkeypatch.setattr(media_svc, "resolve_owned_ready_media", resolve_mock)
    monkeypatch.setattr(media_svc, "bind_media", bind_mock)
    monkeypatch.setattr(community_svc, "resolve_owned_ready_media", resolve_mock)
    monkeypatch.setattr(community_svc, "bind_media", bind_mock)
    monkeypatch.setattr(community_svc, "get_post", fake_get_post)
    _patch_content_filter(monkeypatch)

    post = await community_svc.create_post(
        db,
        user_id=3,
        request=CommunityPostCreate(content="clip", video_media_id=88),
        commit=True,
    )
    assert post.id == 200
    resolve_mock.assert_awaited()
    resolve_kwargs = resolve_mock.await_args
    assert resolve_kwargs.kwargs["purpose"] == "post"
    assert resolve_kwargs.kwargs["media_type"] == "video"
    assert list(resolve_kwargs.args[2]) == [88]
    bind_mock.assert_awaited_once()
    bind_kwargs = bind_mock.await_args.kwargs
    assert bind_kwargs["media_ids"] == [88]
    assert bind_kwargs["target_type"] == "post"
    assert bind_kwargs["target_id"] == 200
    insert_params = db.execute.await_args_list[0].args[1]
    assert insert_params["video"] == "/storage/uploads/3/community/v.mp4"
    assert json.loads(insert_params["images"]) == []


@pytest.mark.asyncio
async def test_create_post_rejects_foreign_media_id(monkeypatch) -> None:
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc

    async def fake_resolve(*_a, **_k):
        raise HTTPException(422, detail="媒体不可用: 99")

    monkeypatch.setattr(community_svc, "resolve_owned_ready_media", fake_resolve)
    _patch_content_filter(monkeypatch)

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await community_svc.create_post(
            db,
            user_id=1,
            request=CommunityPostCreate(content="x", image_media_ids=[99]),
        )
    assert exc.value.status_code == 422
    assert db.execute.await_count == 0


@pytest.mark.asyncio
async def test_create_post_rejects_legacy_external_image_url(monkeypatch) -> None:
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc

    async def boom(*_a, **_k):
        raise AssertionError("should not resolve media ids")

    monkeypatch.setattr(community_svc, "resolve_owned_ready_media", boom)
    _patch_content_filter(monkeypatch)

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await community_svc.create_post(
            db,
            user_id=1,
            request=CommunityPostCreate(content="hi", images=["https://evil.example/a.jpg"]),
        )
    assert exc.value.status_code == 422
    assert "已上传" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_post_legacy_ready_url_binds(monkeypatch) -> None:
    from app.schemas.community import CommunityPostCreate
    from app.services import community as community_svc
    from app.services import community_media as media_svc

    url = "/storage/uploads/2/community/x.webp"
    bind_calls: list[dict] = []

    async def fake_assert(db, user_id, urls, *, purpose, media_type):
        return [{"id": 44, "file_url": url, "status": "ready"}]

    async def fake_bind(db, *, media_ids, target_type, target_id):
        bind_calls.append(
            {"media_ids": list(media_ids), "target_type": target_type, "target_id": target_id}
        )

    async def fake_get_post(db, user_id, post_id):
        return SimpleNamespace(id=post_id, images=[url], video=None, content="hi")

    insert_result = SimpleNamespace(lastrowid=55)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=insert_result)
    db.commit = AsyncMock()

    monkeypatch.setattr(media_svc, "assert_owned_media_urls", fake_assert)
    monkeypatch.setattr(community_svc, "assert_owned_media_urls", fake_assert)
    monkeypatch.setattr(media_svc, "bind_media", fake_bind)
    monkeypatch.setattr(community_svc, "bind_media", fake_bind)
    monkeypatch.setattr(community_svc, "get_post", fake_get_post)
    _patch_content_filter(monkeypatch)

    await community_svc.create_post(
        db,
        user_id=2,
        request=CommunityPostCreate(content="hi", images=[url]),
        commit=False,
    )
    assert bind_calls == [{"media_ids": [44], "target_type": "post", "target_id": 55}]
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_create_paper_plane_binds_image_media_ids(monkeypatch) -> None:
    from app.schemas.community import PaperPlaneCreate
    from app.services import community as community_svc

    bind_calls: list[dict] = []

    async def fake_resolve(db, user_id, media_ids, *, purpose, media_type=None):
        assert purpose == "paper_plane"
        assert media_type == "image"
        return [{"id": 3, "file_url": "/storage/uploads/1/community/p.webp", "status": "ready"}]

    async def fake_bind(db, *, media_ids, target_type, target_id):
        bind_calls.append(
            {"media_ids": list(media_ids), "target_type": target_type, "target_id": target_id}
        )

    async def consume(_key: str, _limit: int) -> bool:
        return True

    insert_result = SimpleNamespace(lastrowid=77)
    created_mappings = MagicMock()
    created_mappings.one.return_value = {
        "id": 77,
        "content": "plane",
        "images": '["/storage/uploads/1/community/p.webp"]',
        "city": None,
        "tags": "[]",
        "is_anonymous": 1,
        "reply_count": 0,
        "voice_url": None,
        "voice_duration_sec": None,
        "created_at": "2026-01-01T00:00:00",
    }
    created_result = MagicMock()
    created_result.mappings.return_value = created_mappings

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[insert_result, created_result])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    monkeypatch.setattr(community_svc, "resolve_owned_ready_media", fake_resolve)
    monkeypatch.setattr(community_svc, "bind_media", fake_bind)
    monkeypatch.setattr(community_svc, "consume_daily", consume)
    _patch_content_filter(monkeypatch)

    resp = await community_svc.create_paper_plane(
        db,
        user_id=1,
        request=PaperPlaneCreate(content="plane", image_media_ids=[3]),
        commit=True,
    )
    assert resp.id == 77
    assert bind_calls == [{"media_ids": [3], "target_type": "paper_plane", "target_id": 77}]


@pytest.mark.asyncio
async def test_delete_post_marks_attached_media_deleted(monkeypatch) -> None:
    from app.services import community as community_svc

    update_post = SimpleNamespace(rowcount=1)
    select_mappings = MagicMock()
    select_mappings.all.return_value = [{"media_id": 8}, {"media_id": 9}]
    select_result = MagicMock()
    select_result.mappings.return_value = select_mappings
    media_update = MagicMock()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[update_post, select_result, media_update])
    db.commit = AsyncMock()

    await community_svc.delete_post(db, user_id=4, post_id=12)
    assert db.execute.await_count >= 2
    sqls = [" ".join(str(call.args[0]).split()).lower() for call in db.execute.await_args_list]
    assert any("community_media" in s and "status" in s for s in sqls)
    assert db.commit.await_count == 1
