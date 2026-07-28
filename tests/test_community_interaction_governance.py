from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas.admin import ReportReviewRequest
from app.main import app
from app.schemas.community import CommunityCommentResponse
from app.schemas.social import NotificationItem, PrivacyResponse, PrivacyUpdateRequest
from app.services import community as community_service
from app.services import notifications as notification_service
from app.services import social as social_service
from database_setup_marriage import DatabaseManager, FAIL_CLOSED_COMMUNITY_COLUMNS


client = TestClient(app)


class _ScalarResult:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


class _Mappings:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def first(self) -> dict[str, Any] | None:
        return self.row

    def one(self) -> dict[str, Any]:
        assert self.row is not None
        return self.row


class _MappingResult(_ScalarResult):
    def __init__(self, row: dict[str, Any] | None) -> None:
        super().__init__()
        self.row = row

    def mappings(self) -> _Mappings:
        return _Mappings(self.row)


class ScriptedSession:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.execute = AsyncMock(side_effect=self.results)
        self.commit = AsyncMock()


class NotificationSession:
    def __init__(self, *, preference_enabled: bool = True, blocked: bool = False) -> None:
        self.preference_enabled = preference_enabled
        self.blocked = blocked
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> _ScalarResult:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        self.params.append(params or {})
        if "FROM user_block" in sql:
            return _ScalarResult(1 if self.blocked else None)
        if "FROM user_privacy" in sql:
            return _ScalarResult(1 if self.preference_enabled else 0)
        return _ScalarResult()


@pytest.mark.asyncio
async def test_notification_events_honor_self_preference_and_block_suppression() -> None:
    assert hasattr(social_service, "emit_notification")
    emit = social_service.emit_notification

    self_db = NotificationSession()
    assert await emit(
        self_db,
        recipient_user_id=7,
        actor_user_id=7,
        event_type="follow",
        title="关注通知",
        content="有人关注了你",
        target_type="user",
        target_id=7,
    ) is False
    assert self_db.statements == []

    preference_db = NotificationSession(preference_enabled=False)
    assert await emit(
        preference_db,
        recipient_user_id=8,
        actor_user_id=7,
        event_type="follow",
        title="关注通知",
        content="有人关注了你",
        target_type="user",
        target_id=7,
    ) is False
    assert not any("INSERT INTO user_notification" in sql for sql in preference_db.statements)

    blocked_db = NotificationSession(blocked=True)
    assert await emit(
        blocked_db,
        recipient_user_id=8,
        actor_user_id=7,
        event_type="follow",
        title="关注通知",
        content="有人关注了你",
        target_type="user",
        target_id=7,
    ) is False
    assert not any("INSERT INTO user_notification" in sql for sql in blocked_db.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["report_result", "appeal_result"])
async def test_governance_notification_bypasses_system_notification_preference(event_type: str) -> None:
    db = NotificationSession(preference_enabled=False)

    assert await notification_service.emit_notification(
        db,
        recipient_user_id=8,
        actor_user_id=None,
        event_type=event_type,
        title="举报处理结果",
        content="举报已处理",
        target_type="report",
        target_id=9,
    ) is True

    assert any("INSERT INTO user_notification" in sql for sql in db.statements)
    assert not any("FROM user_privacy" in sql for sql in db.statements)


@pytest.mark.asyncio
async def test_notification_writer_bounds_database_strings() -> None:
    db = NotificationSession()

    assert await notification_service.emit_notification(
        db,
        recipient_user_id=8,
        actor_user_id=7,
        event_type="comment",
        title="t" * 140,
        content="c" * 500,
        target_type="post",
        target_id=9,
    ) is True

    insert_params = db.params[-1]
    assert len(insert_params["title"]) == 128
    assert len(insert_params["content"]) == 255


def test_notification_and_privacy_schemas_expose_follow_message_and_navigation_targets() -> None:
    request = PrivacyUpdateRequest(notify_follow=False, notify_message=False)
    assert request.notify_follow is False
    assert request.notify_message is False

    privacy = PrivacyResponse(
        user_id=1,
        hide_phone=False,
        hide_school=False,
        hide_company=False,
        hide_distance=False,
        hide_online_status=False,
        only_auth_can_contact=False,
        only_vip_can_see_detail=False,
        who_can_see_me=1,
        match_status=1,
        anonymous_browse_enabled=False,
        show_profile=True,
        show_likes=True,
        show_posts=True,
        notify_like=True,
        notify_comment=True,
        notify_follow=False,
        notify_message=False,
        notify_match=True,
        notify_apply=True,
        notify_system=True,
        notify_activity=True,
    )
    assert privacy.notify_follow is False
    assert privacy.notify_message is False

    item = NotificationItem(
        id=1,
        notification_type="comment",
        title="新评论",
        content="有人评论了你的动态",
        payload=None,
        related_user_id=2,
        related_id=9,
        target_type="post",
        target_id=9,
        is_read=False,
        created_at=datetime(2026, 7, 28),
    )
    assert (item.target_type, item.target_id) == ("post", 9)


@pytest.mark.asyncio
async def test_follow_event_uses_central_notification_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ensure_target(_db: object, _user_id: int, _target_id: int) -> None:
        return None

    emitted: list[dict[str, Any]] = []

    async def fake_emit(_db: object, **kwargs: Any) -> bool:
        emitted.append(kwargs)
        return True

    monkeypatch.setattr(social_service, "_ensure_target", fake_ensure_target)
    monkeypatch.setattr(social_service, "emit_notification", fake_emit)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult()),
        commit=AsyncMock(),
    )

    await social_service.set_follow(db, user_id=7, target_id=8, enabled=True)

    assert emitted == [
        {
            "recipient_user_id": 8,
            "actor_user_id": 7,
            "event_type": "follow",
            "title": "有人关注了你",
            "content": "你收到了一条新的关注",
            "target_type": "user",
            "target_id": 7,
        }
    ]


@pytest.mark.asyncio
async def test_single_notification_read_uses_notification_primary_key() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult()), commit=AsyncMock())

    await social_service.mark_notification_read(db, user_id=7, notification_id=19)

    statement = str(db.execute.await_args.args[0])
    assert "AND id = :notification_id" in statement
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocking_user_closes_both_match_parentheses() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult()),
        commit=AsyncMock(),
    )

    async def fake_ensure_target(_db: object, _user_id: int, _target_id: int) -> None:
        return None

    original = social_service._ensure_target
    social_service._ensure_target = fake_ensure_target
    try:
        await social_service.set_block(db, user_id=7, target_id=8, request=None, enabled=True)
    finally:
        social_service._ensure_target = original

    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "OR (user_id = :target_id AND target_user_id = :user_id)" in statements[1]


def test_message_notification_preference_column_fails_closed_during_migration() -> None:
    assert ("user_privacy", "notify_message") in FAIL_CLOSED_COMMUNITY_COLUMNS


def test_threaded_comment_routes_and_legacy_array_route_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/community/posts/{post_id}/comments" in paths
    assert "/api/v1/community/posts/{post_id}/comments/page" in paths
    assert "/api/v1/community/comments/{comment_id}/replies" in paths
    legacy = paths["/api/v1/community/posts/{post_id}/comments"]["get"]
    assert legacy["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"


def test_comment_schema_exposes_thread_and_tombstone_metadata() -> None:
    comment = CommunityCommentResponse(
        id=21,
        post_id=4,
        user_id=8,
        nickname="reply author",
        avatar=None,
        parent_id=20,
        root_id=10,
        target_comment_id=20,
        target_user_id=7,
        reply_to_user="root author",
        content="reply",
        like_count=0,
        is_liked=False,
        reply_count=0,
        is_deleted=False,
        can_delete=True,
        created_at=datetime(2026, 7, 28),
    )
    assert comment.root_id == 10
    assert comment.target_comment_id == 20
    assert comment.target_user_id == 7
    assert comment.reply_to_user == "root author"
    assert comment.can_delete is True
    assert comment.replies == []
    assert comment.is_deleted is False

    assert hasattr(community_service, "CommentCursorPage")
    page = community_service.CommentCursorPage(items=[comment], next_cursor="opaque", has_more=True)
    assert page.next_cursor == "opaque"


@pytest.mark.asyncio
async def test_comment_cursor_rejects_malformed_values() -> None:
    assert hasattr(community_service, "decode_comment_cursor")
    with pytest.raises(HTTPException) as exc:
        community_service.decode_comment_cursor("not-a-valid-cursor")
    assert exc.value.status_code == 422


def test_report_and_appeal_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/community/reports/mine",
        "/api/v1/community/reports/{report_id}/appeals",
        "/api/v1/community/report-appeals/mine",
        "/api/v1/admin/reports/{report_id}",
        "/api/v1/admin/report-appeals",
        "/api/v1/admin/report-appeals/{appeal_id}/review",
    ):
        assert path in paths


@pytest.mark.asyncio
async def test_report_review_is_terminal() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_MappingResult(
                {"id": 4, "target_type": "post", "target_id": 11, "status": 1}
            )
        ),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException) as exc:
        await social_service.review_report(
            db,
            4,
            ReportReviewRequest(status=1, result="重复审核", action="none"),
            actor_id=9,
        )
    assert exc.value.status_code == 409
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


def test_report_review_rejects_contradictory_status_and_action() -> None:
    with pytest.raises(ValidationError):
        ReportReviewRequest(
            status=2,
            result="举报驳回",
            action="hide_content",
        )
    with pytest.raises(ValidationError):
        ReportReviewRequest(
            status=1,
            result="举报成立",
            action="dismiss",
        )


@pytest.mark.asyncio
async def test_report_review_service_rejects_bypassed_invalid_model() -> None:
    request = ReportReviewRequest.model_construct(
        status=2,
        result="举报驳回",
        action="hide_content",
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    with pytest.raises(HTTPException) as exc:
        await social_service.review_report(db, 4, request, actor_id=9)

    assert exc.value.status_code == 422
    db.execute.assert_not_awaited()


def test_subject_report_projection_redacts_safety_identities() -> None:
    row = {
        "id": 5,
        "reporter_user_id": 7,
        "target_user_id": 8,
        "target_type": "post",
        "target_id": 12,
        "type": "harassment",
        "description": "reporter-authored details",
        "status": 1,
        "result": "举报成立",
        "action": "hide_content",
        "reviewed_by": 9,
        "reviewed_at": datetime(2026, 7, 28),
        "created_at": datetime(2026, 7, 27),
        "updated_at": datetime(2026, 7, 28),
        "has_appeal": 0,
    }

    detail = social_service._user_report_detail(row, viewer_id=8)
    dumped = detail.model_dump()

    assert detail.viewer_role == "subject"
    assert detail.description is None
    assert "reporter_user_id" not in dumped
    assert "reviewed_by" not in dumped


@pytest.mark.asyncio
async def test_content_moderation_does_not_reuse_user_deletion_state() -> None:
    update = SimpleNamespace(rowcount=1)
    db = SimpleNamespace(execute=AsyncMock(return_value=update))

    await social_service.moderate_content(
        db, target_type="post", target_id=11, hide=True, actor_id=None
    )
    await social_service.moderate_content(
        db, target_type="comment", target_id=12, hide=True, actor_id=None
    )

    statements = [" ".join(str(call.args[0]).split()) for call in db.execute.await_args_list]
    assert all("moderation_status" in sql for sql in statements)
    assert all(" SET status" not in sql for sql in statements)


@pytest.mark.asyncio
async def test_appeal_requires_subject_and_different_terminal_reviewer() -> None:
    assert hasattr(social_service, "create_report_appeal")
    assert hasattr(social_service, "review_report_appeal")

    schemas = pytest.importorskip("app.schemas.social")
    create_request = schemas.ReportAppealCreate(reason="内容没有违规，请复核")
    unauthorized = SimpleNamespace(
        execute=AsyncMock(
            return_value=_MappingResult(
                {
                    "id": 5,
                    "target_user_id": 99,
                    "target_type": "post",
                    "target_id": 12,
                    "status": 1,
                    "action": "hide_content",
                    "reviewed_by": 7,
                }
            )
        )
    )
    with pytest.raises(HTTPException) as subject_exc:
        await social_service.create_report_appeal(
            unauthorized, user_id=8, report_id=5, request=create_request
        )
    assert subject_exc.value.status_code == 403

    admin_schemas = pytest.importorskip("app.schemas.admin")
    review_request = admin_schemas.ReportAppealReviewRequest(
        status=1, result="申诉通过"
    )
    same_reviewer = SimpleNamespace(
        execute=AsyncMock(
            return_value=_MappingResult(
                {
                    "id": 6,
                    "report_id": 5,
                    "status": 0,
                    "target_type": "post",
                    "target_id": 12,
                    "original_reviewer_id": 7,
                }
            )
        ),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException) as reviewer_exc:
        await social_service.review_report_appeal(
            same_reviewer,
            appeal_id=6,
            request=review_request,
            actor_id=7,
        )
    assert reviewer_exc.value.status_code == 409
    assert same_reviewer.execute.await_count == 1
    same_reviewer.commit.assert_not_awaited()


def test_notification_table_has_one_central_writer() -> None:
    service_dir = Path("app/services")
    direct_writers = []
    for path in service_dir.glob("*.py"):
        if path.name == "notifications.py":
            continue
        if "INSERT INTO user_notification" in path.read_text(encoding="utf-8"):
            direct_writers.append(path.name)
    assert direct_writers == []


def test_notification_response_exposes_standard_actor_and_action_fields() -> None:
    item = pytest.importorskip("app.schemas.social").NotificationItem(
        id=1,
        notification_type="comment",
        title="新评论",
        content="你好",
        payload={},
        related_user_id=7,
        related_id=11,
        target_type="post",
        target_id=11,
        actor_user_id=7,
        action="comment",
        is_read=False,
        created_at=datetime(2026, 7, 28),
    )
    assert item.actor_user_id == 7
    assert item.action == "comment"


@pytest.mark.asyncio
async def test_post_like_emits_navigation_aware_event_once(monkeypatch: pytest.MonkeyPatch) -> None:
    post = SimpleNamespace(user_id=8, content="post content")

    async def fake_get_post(_db: object, _user_id: int, _post_id: int) -> object:
        return post

    emitted: list[dict[str, Any]] = []

    async def fake_emit(_db: object, **kwargs: Any) -> bool:
        emitted.append(kwargs)
        return True

    monkeypatch.setattr(community_service, "get_post", fake_get_post)
    monkeypatch.setattr(community_service, "emit_notification", fake_emit)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        commit=AsyncMock(),
    )

    await community_service.like_post(db, user_id=7, post_id=11, enabled=True)

    assert emitted == [
        {
            "recipient_user_id": 8,
            "actor_user_id": 7,
            "event_type": "like",
            "title": "有人点赞了你的动态",
            "content": "post content",
            "target_type": "post",
            "target_id": 11,
        }
    ]


@pytest.mark.asyncio
async def test_block_relation_rejects_direct_interaction() -> None:
    assert hasattr(notification_service, "ensure_interaction_allowed")
    with pytest.raises(HTTPException) as exc:
        await notification_service.ensure_interaction_allowed(
            NotificationSession(blocked=True), actor_user_id=7, target_user_id=8
        )
    assert exc.value.status_code == 403


def test_legacy_reply_without_root_id_falls_back_to_parent() -> None:
    response = community_service._comment_response(
        {
            "id": 21,
            "post_id": 4,
            "user_id": 8,
            "nickname": "legacy",
            "avatar": None,
            "parent_id": 10,
            "root_id": None,
            "target_comment_id": 10,
            "target_user_id": 7,
            "content": "legacy reply",
            "like_count": 0,
            "is_liked": 0,
            "reply_count": 0,
            "deleted_at": None,
            "created_at": datetime(2026, 7, 28),
        }
    )
    assert response.root_id == 10


@pytest.mark.asyncio
async def test_subject_can_create_one_appeal() -> None:
    created_at = datetime(2026, 7, 28)
    inserted = SimpleNamespace(lastrowid=6)
    db = ScriptedSession(
        [
            _MappingResult(
                {
                    "id": 5,
                    "target_user_id": 8,
                    "target_type": "post",
                    "target_id": 12,
                    "status": 1,
                    "action": "hide_content",
                    "reviewed_by": 7,
                }
            ),
            _ScalarResult(None),
            inserted,
            _MappingResult(
                {
                    "id": 6,
                    "report_id": 5,
                    "appellant_user_id": 8,
                    "reason": "请复核",
                    "status": 0,
                    "result": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            ),
        ]
    )
    request = pytest.importorskip("app.schemas.social").ReportAppealCreate(reason="请复核")

    response = await social_service.create_report_appeal(
        db, user_id=8, report_id=5, request=request
    )

    assert response.id == 6
    assert response.status == 0
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_reviewer_can_approve_appeal(monkeypatch: pytest.MonkeyPatch) -> None:
    restored: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []

    async def fake_moderate(_db: object, **kwargs: Any) -> bool:
        restored.append(kwargs)
        return True

    async def fake_emit(_db: object, **kwargs: Any) -> bool:
        emitted.append(kwargs)
        return True

    monkeypatch.setattr(social_service, "moderate_content", fake_moderate)
    monkeypatch.setattr(social_service, "emit_notification", fake_emit)
    db = ScriptedSession(
        [
            _MappingResult(
                {
                    "id": 6,
                    "report_id": 5,
                    "appellant_user_id": 8,
                    "status": 0,
                    "target_type": "post",
                    "target_id": 12,
                    "original_action": "hide_content",
                    "original_reviewer_id": 7,
                }
            ),
            _ScalarResult(),
        ]
    )
    request = pytest.importorskip("app.schemas.admin").ReportAppealReviewRequest(
        status=1, result="申诉通过"
    )

    response = await social_service.review_report_appeal(
        db, appeal_id=6, request=request, actor_id=9
    )

    assert response.content_restored is True
    assert restored == [
        {
            "target_type": "post",
            "target_id": 12,
            "hide": False,
            "reason": "申诉通过",
            "actor_id": 9,
            "expected_report_id": 5,
        }
    ]
    assert emitted[0]["recipient_user_id"] == 8
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_appeal_does_not_restore_content_unrelated_to_original_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored: list[dict[str, Any]] = []

    async def fake_moderate(_db: object, **kwargs: Any) -> bool:
        restored.append(kwargs)
        return True

    async def fake_emit(_db: object, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(social_service, "moderate_content", fake_moderate)
    monkeypatch.setattr(social_service, "emit_notification", fake_emit)
    db = ScriptedSession(
        [
            _MappingResult(
                {
                    "id": 6,
                    "report_id": 5,
                    "appellant_user_id": 8,
                    "status": 0,
                    "target_type": "post",
                    "target_id": 12,
                    "original_action": "none",
                    "original_reviewer_id": 7,
                }
            ),
            _ScalarResult(),
        ]
    )
    request = pytest.importorskip("app.schemas.admin").ReportAppealReviewRequest(
        status=1, result="申诉通过"
    )

    response = await social_service.review_report_appeal(
        db, appeal_id=6, request=request, actor_id=9
    )

    assert response.content_restored is False
    assert restored == []


def test_database_bootstrap_backfills_nested_comment_roots() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.rowcounts = iter((2, 1, 0))
            self.rowcount = 0
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(" ".join(statement.split()))
            self.rowcount = next(self.rowcounts)

    cursor = Cursor()
    manager = object.__new__(DatabaseManager)

    manager._backfill_comment_roots(cursor)

    assert len(cursor.statements) == 3
    assert "parent.root_id" in cursor.statements[0]
    assert "comment.root_id IS NULL" in cursor.statements[0]


def test_database_bootstrap_upgrades_community_indexes() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(" ".join(statement.split()))

        def fetchone(self) -> None:
            return None

    cursor = Cursor()
    manager = object.__new__(DatabaseManager)

    manager._ensure_community_post_feed_indexes(cursor)

    alters = [sql for sql in cursor.statements if sql.startswith("ALTER TABLE")]
    assert any("idx_post_visibility_state" in sql for sql in alters)
    assert any("`community_comment`" in sql and "idx_root_created" in sql for sql in alters)


def test_database_bootstrap_replaces_reviewer_cascade_with_set_null() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(" ".join(statement.split()))

        def fetchone(self) -> dict[str, str]:
            return {"CONSTRAINT_NAME": "fk_user_report_reviewed_by", "DELETE_RULE": "CASCADE"}

    cursor = Cursor()
    manager = object.__new__(DatabaseManager)

    manager._add_foreign_key(
        cursor,
        "user_report",
        "reviewed_by",
        on_delete="SET NULL",
    )

    assert any("DROP FOREIGN KEY `fk_user_report_reviewed_by`" in sql for sql in cursor.statements)
    assert any("ON DELETE SET NULL" in sql for sql in cursor.statements)
