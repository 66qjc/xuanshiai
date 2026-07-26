import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import database_setup_marriage
from app.api.dependencies import CurrentUser, get_current_user, get_realname_verified_user
from app.api.routes import community as community_routes
from app.core import redis as redis_module
from app.db.session import get_db
from app.main import app
from app.schemas.community import (
    ActivitySignupCreate,
    CommunityCityUpdateRequest,
    CommunityPostCreate,
    CommunityPostPage,
    CommunityTopicDetailResponse,
    CommunityTopicResponse,
    PaperPlaneCreate,
    PaperPlaneResponse,
)
from app.schemas.discovery import ApplicationCreateRequest
from app.services import community as community_service
from app.services import discovery as discovery_service


client = TestClient(app)


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        if len(self.rows) != 1:
            raise AssertionError(f"expected one row, got {len(self.rows)}")
        return self.rows[0]


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def scalar(self) -> object | None:
        if not self.rows:
            return None
        return next(iter(self.rows[0].values()))


class FakeSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def execute(self, _statement: object, _params: object = None) -> FakeResult:
        return FakeResult(self.rows)


class FakeRedis:
    def __init__(self, *, eval_result: int = 1, error: Exception | None = None) -> None:
        self.eval_result = eval_result
        self.error = error
        self.eval_calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> int:
        self.eval_calls.append(args)
        if self.error is not None:
            raise self.error
        return self.eval_result


class FakeMutationResult:
    def __init__(self, *, rowcount: int = 1, lastrowid: int = 1) -> None:
        self.rowcount = rowcount
        self.lastrowid = lastrowid


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class FakeIdempotencySession:
    def __init__(
        self,
        record: dict[str, object] | None = None,
        *,
        allow_takeover: bool = False,
    ) -> None:
        self.record = record
        self.allow_takeover = allow_takeover
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []

    @classmethod
    def completed(
        cls,
        *,
        user_id: int,
        operation: str,
        key: str,
        payload_hash: str,
        response_json: dict[str, object],
    ) -> "FakeIdempotencySession":
        return cls(
            {
                "id": 91,
                "user_id": user_id,
                "operation": operation,
                "idempotency_key": key,
                "payload_hash": payload_hash,
                "state": "completed",
                "response_json": response_json,
                "owner_token": "finished-owner",
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )

    @classmethod
    def reserved(
        cls,
        *,
        user_id: int,
        operation: str,
        key: str,
        payload_hash: str,
    ) -> "FakeIdempotencySession":
        return cls(
            {
                "id": 92,
                "user_id": user_id,
                "operation": operation,
                "idempotency_key": key,
                "payload_hash": payload_hash,
                "state": "reserved",
                "response_json": None,
                "owner_token": "active-owner",
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )

    async def execute(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeResult | FakeMutationResult:
        sql = " ".join(str(statement).split()).lower()
        self.statements.append(sql)
        values = params or {}
        if sql.startswith("insert into api_idempotency_record"):
            if self.record is not None:
                raise IntegrityError("duplicate", values, RuntimeError("duplicate"))
            self.record = {
                "id": 1,
                "user_id": values["user_id"],
                "operation": values["operation"],
                "idempotency_key": values["idempotency_key"],
                "payload_hash": values["payload_hash"],
                "state": "reserved",
                "response_json": None,
                "owner_token": values["owner_token"],
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
            return FakeMutationResult()
        if sql.startswith("select") and "api_idempotency_record" in sql:
            return FakeResult([self.record] if self.record is not None else [])
        if sql.startswith("update api_idempotency_record") and "completed" in sql:
            if self.record is None or self.record["owner_token"] != values["owner_token"]:
                return FakeMutationResult(rowcount=0)
            self.record["state"] = "completed"
            self.record["response_json"] = values["response_json"]
            return FakeMutationResult()
        if sql.startswith("update api_idempotency_record") and "date_sub" in sql:
            if not self.allow_takeover or self.record is None:
                return FakeMutationResult(rowcount=0)
            self.record["owner_token"] = values["owner_token"]
            return FakeMutationResult()
        if sql.startswith("delete from api_idempotency_record"):
            if self.record is not None and self.record["owner_token"] == values["owner_token"]:
                self.record = None
                return FakeMutationResult()
            return FakeMutationResult(rowcount=0)
        raise AssertionError(f"unexpected idempotency SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class ScriptedSession:
    def __init__(self, *results: dict[str, object] | None | Exception) -> None:
        self.results = list(results)
        self.rollbacks = 0

    async def execute(self, _statement: object, _params: object = None) -> FakeResult:
        if not self.results:
            raise AssertionError("unexpected database execute")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResult([result] if result is not None else [])

    async def rollback(self) -> None:
        self.rollbacks += 1


class RollbackOnlySession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class RollbackFailingSession(ScriptedSession):
    def __init__(self, *results: dict[str, object] | None | Exception) -> None:
        super().__init__(*results)
        self.refund_attempted = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        raise RuntimeError("rollback failed")


class IdempotencyMigrationCursor:
    def __init__(self, columns: list[dict[str, object]]) -> None:
        self.columns = columns
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()).lower())

    def fetchall(self) -> list[dict[str, object]]:
        return self.columns


class CommunityDatabaseHarness:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.user_ids: list[int] = []
        self.activity_ids: list[int] = []
        self.city = f"test-city-{uuid4().hex}"

    async def create_user(
        self,
        *,
        show_posts: int = 1,
        real_name: str | None = None,
        phone: str | None = None,
    ) -> int:
        token = uuid4().hex
        account_phone = phone or f"139{uuid4().int % 100_000_000:08d}"
        async with self.sessions() as db:
            created = await db.execute(
                text(
                    """INSERT INTO users (phone, nickname, status)
                    VALUES (:phone, :nickname, 1)"""
                ),
                {"phone": account_phone, "nickname": f"community-test-{token[:12]}"},
            )
            user_id = int(created.lastrowid)
            await db.execute(
                text("INSERT INTO user_privacy (user_id, show_posts) VALUES (:user_id, :show_posts)"),
                {"user_id": user_id, "show_posts": show_posts},
            )
            if real_name is not None:
                await db.execute(
                    text(
                        """INSERT INTO user_auth (user_id, real_name, realname_status)
                        VALUES (:user_id, :real_name, 2)"""
                    ),
                    {"user_id": user_id, "real_name": real_name},
                )
            await db.commit()
        self.user_ids.append(user_id)
        return user_id

    async def create_post(self, user_id: int, visibility: int) -> int:
        async with self.sessions() as db:
            created = await db.execute(
                text(
                    """INSERT INTO community_post
                    (user_id, content, images, location, visibility, declaration, status)
                    VALUES (:user_id, :content, JSON_ARRAY(), :location, :visibility, '', 1)"""
                ),
                {
                    "user_id": user_id,
                    "content": f"community-test-post-{uuid4().hex}",
                    "location": self.city,
                    "visibility": visibility,
                },
            )
            await db.commit()
            return int(created.lastrowid)

    async def create_match(self, user_id: int, target_user_id: int) -> None:
        async with self.sessions() as db:
            await db.execute(
                text(
                    """INSERT INTO user_match (user_id, target_user_id, status)
                    VALUES (:user_id, :target_user_id, 1)"""
                ),
                {"user_id": user_id, "target_user_id": target_user_id},
            )
            await db.commit()

    async def create_activity(self, max_people: int) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self.sessions() as db:
            created = await db.execute(
                text(
                    """INSERT INTO offline_activity
                    (title, start_time, end_time, signup_deadline, max_people, current_people, status)
                    VALUES (:title, :start_time, :end_time, :signup_deadline, :max_people, 0, 1)"""
                ),
                {
                    "title": f"community-test-activity-{uuid4().hex}",
                    "start_time": now + timedelta(days=2),
                    "end_time": now + timedelta(days=2, hours=2),
                    "signup_deadline": now + timedelta(days=1),
                    "max_people": max_people,
                },
            )
            await db.commit()
            activity_id = int(created.lastrowid)
        self.activity_ids.append(activity_id)
        return activity_id

    async def cleanup(self) -> None:
        async with self.sessions() as db:
            for activity_id in self.activity_ids:
                await db.execute(
                    text("DELETE FROM activity_signup WHERE activity_id = :activity_id"),
                    {"activity_id": activity_id},
                )
                await db.execute(
                    text("DELETE FROM offline_activity WHERE id = :activity_id"),
                    {"activity_id": activity_id},
                )
            for user_id in self.user_ids:
                await db.execute(text("DELETE FROM community_post WHERE user_id = :user_id"), {"user_id": user_id})
                await db.execute(
                    text("DELETE FROM user_match WHERE user_id = :user_id OR target_user_id = :user_id"),
                    {"user_id": user_id},
                )
                await db.execute(text("DELETE FROM user_auth WHERE user_id = :user_id"), {"user_id": user_id})
                await db.execute(text("DELETE FROM user_privacy WHERE user_id = :user_id"), {"user_id": user_id})
                await db.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
            await db.commit()


@pytest_asyncio.fixture
async def community_database() -> CommunityDatabaseHarness:
    test_database_url = os.getenv("COMMUNITY_TEST_DATABASE_URL")
    if os.getenv("ENVIRONMENT") != "testing" or not test_database_url:
        pytest.skip(
            "database-backed community tests require ENVIRONMENT=testing and COMMUNITY_TEST_DATABASE_URL"
        )
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    harness = CommunityDatabaseHarness(async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield harness
    finally:
        await harness.cleanup()
        await engine.dispose()


def route_dependencies(path: str, method: str) -> set[object]:
    method_u = method.upper()
    matches = []
    for route in app.routes:
        route_path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if route_path == path and method_u in {m.upper() for m in methods}:
            matches.append(route)
        for child in getattr(route, "routes", []) or []:
            child_path = getattr(child, "path", None)
            child_methods = getattr(child, "methods", None) or set()
            if child_path == path and method_u in {m.upper() for m in child_methods}:
                matches.append(child)
    if not matches:
        raise AssertionError(f"route not found: {method_u} {path}")
    route = matches[0]
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        raise AssertionError(f"route has no dependant: {method_u} {path}")
    return {dependency.call for dependency in dependant.dependencies}


def test_community_content_limits() -> None:
    with pytest.raises(ValidationError):
        CommunityPostCreate(content="x" * 2001)
    with pytest.raises(ValidationError):
        PaperPlaneCreate(content="")
    assert ActivitySignupCreate().remark is None


def test_city_code_requires_four_or_six_ascii_digits_at_schema_and_feed_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for code in ("3301", "330100"):
        assert CommunityCityUpdateRequest(name="Hangzhou", code=code).code == code

    for code in ("", "123", "12345", "1234567", "12ab", "１２３４"):
        with pytest.raises(ValidationError):
            CommunityCityUpdateRequest(name="Hangzhou", code=code)

    async def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=7,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    async def fake_db():
        yield object()

    async def fake_list_posts(*_args: object, **_kwargs: object) -> CommunityPostPage:
        return CommunityPostPage(items=[], page=1, page_size=20, total=0)

    monkeypatch.setattr(community_routes, "list_posts", fake_list_posts)
    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db
    try:
        for code in ("123", "12345", "12ab", "１２３４"):
            response = client.get("/api/v1/community/posts", params={"city_code": code})
            assert response.status_code == 422
        for code in ("3301", "330100"):
            response = client.get("/api/v1/community/posts", params={"city_code": code})
            assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_primary_community_and_social_docs_match_current_contracts() -> None:
    community_docs = Path("docs/api/community.md").read_text(encoding="utf-8")
    community_primary = community_docs.split("## 10.", 1)[0]
    primary_feed = community_primary.split("### 2.3", 1)[0]
    primary_topic_detail = community_primary.split(
        "#### `GET /api/v1/community/topics/{topic_id}/detail`", 1
    )[1].split("### 7.4", 1)[0]
    primary_quotas = community_primary.split("### 9.2", 1)[1].split("### 9.3", 1)[0]
    primary_media = community_primary.split("## 11.", 1)[1]

    assert "当前未提供话题查询接口" not in community_primary
    assert "`latest` / `following` / `city` / `liked_users` / `following_and_liked`" in primary_feed
    assert '"posts":{"items":' in primary_topic_detail
    assert '"posts":[]' not in primary_topic_detail
    assert '"points_available":false' in primary_quotas
    assert '"points_available":true' not in primary_quotas

    # Community media upload contracts (Task 4)
    assert "POST /api/v1/community/media/uploads" in primary_media
    assert "DELETE /api/v1/community/media/{media_id}" in primary_media
    assert "image_media_ids" in community_primary
    assert "video_media_id" in community_primary
    assert "cleanup_expired_unbound_media" in primary_media
    assert "`ready`" in primary_media and "`bound`" in primary_media
    assert "multipart/form-data" in primary_media

    social_docs = Path("docs/api/social.md").read_text(encoding="utf-8")
    likes = social_docs.split("### 2.1", 1)[1].split("### 2.2", 1)[0]
    follows = social_docs.split("### 2.2", 1)[1].split("### 2.3", 1)[0]
    assert "实名认证通过" in likes
    assert "实名认证通过" in follows


def test_post_visibility_and_declaration_contract() -> None:
    post = CommunityPostCreate(content="hello", visibility=2, declaration="内容包含虚构演绎")
    assert post.visibility == 2
    assert post.declaration == "内容包含虚构演绎"
    with pytest.raises(ValidationError):
        CommunityPostCreate(content="hello", visibility=3)


def test_post_visibility_clause_covers_public_friends_and_self_only() -> None:
    clause = community_service._post_visibility_clause()
    assert "p.user_id = :user_id" in clause
    assert "p.visibility = 0" in clause
    assert "p.visibility = 1" in clause
    assert "user_match" in clause
    assert "p.visibility = 2" in clause


def test_city_anchor_does_not_fall_back_to_residence() -> None:
    assert community_service._resolve_city_anchor(
        city=None,
        city_code=None,
        me={"residence": "杭州", "residence_city_code": "330100"},
    ) == (None, None)


def test_city_code_takes_precedence_over_conflicting_city_name() -> None:
    assert community_service._resolve_city_anchor(
        city="杭州",
        city_code="3201",
        me=None,
    ) == ("南京", "320100")


def test_unknown_city_code_keeps_explicit_city_name() -> None:
    assert community_service._resolve_city_anchor(
        city="\u798f\u5dde",
        city_code="3501",
        me=None,
    ) == ("\u798f\u5dde", "350100")


def test_topic_detail_retains_post_page_metadata() -> None:
    topic = CommunityTopicResponse(
        id=1,
        name="树洞",
        icon=None,
        sort=0,
        post_count=0,
        participant_count=0,
        heat=0,
        joined=False,
    )
    posts = CommunityPostPage(items=[], page=2, page_size=10, total=23)
    detail = CommunityTopicDetailResponse(topic=topic, posts=posts, sort="latest")
    assert detail.posts.items == []
    assert detail.posts.page == 2
    assert detail.posts.page_size == 10
    assert detail.posts.total == 23


@pytest.mark.asyncio
async def test_post_visibility_blocks_outsiders_and_keeps_self_only_for_author(
    community_database: CommunityDatabaseHarness,
) -> None:
    owner = await community_database.create_user()
    stranger = await community_database.create_user()
    friends_only = await community_database.create_post(owner, visibility=1)
    self_only = await community_database.create_post(owner, visibility=2)

    async with community_database.sessions() as db:
        for post_id in (friends_only, self_only):
            with pytest.raises(HTTPException) as exc:
                await community_service.get_post(db, stranger, post_id)
            assert exc.value.status_code == 404

        await community_database.create_match(stranger, owner)
        with pytest.raises(HTTPException) as exc:
            await community_service.get_post(db, stranger, friends_only)
        assert exc.value.status_code == 404

        await community_database.create_match(owner, stranger)
        visible_to_friend = await community_service.get_post(db, stranger, friends_only)
        assert visible_to_friend.id == friends_only

        visible_to_author = await community_service.get_post(db, owner, self_only)
        assert visible_to_author.id == self_only


@pytest.mark.asyncio
async def test_post_feed_count_excludes_hidden_public_and_private_posts(
    community_database: CommunityDatabaseHarness,
) -> None:
    public_owner = await community_database.create_user(show_posts=1)
    hidden_owner = await community_database.create_user(show_posts=0)
    private_owner = await community_database.create_user(show_posts=1)
    stranger = await community_database.create_user()
    public_post = await community_database.create_post(public_owner, visibility=0)
    await community_database.create_post(hidden_owner, visibility=0)
    await community_database.create_post(private_owner, visibility=1)
    await community_database.create_post(private_owner, visibility=2)

    async with community_database.sessions() as db:
        page = await community_service.list_posts(
            db,
            stranger,
            mode="city",
            city=community_database.city,
            page=1,
            page_size=20,
        )

    assert page.total == 1
    assert [post.id for post in page.items] == [public_post]


@pytest.mark.asyncio
async def test_activity_signup_uses_canonical_contact_not_spoofed_request(
    community_database: CommunityDatabaseHarness,
) -> None:
    canonical_phone = "13800000001"
    user_id = await community_database.create_user(
        real_name="Canonical Name",
        phone=canonical_phone,
    )
    activity_id = await community_database.create_activity(max_people=2)

    async with community_database.sessions() as db:
        await community_service.signup_activity(
            db,
            user_id,
            activity_id,
            ActivitySignupCreate(real_name="Spoofed Name", phone="19900000000", remark="note"),
        )
        signup = (
            await db.execute(
                text(
                    """SELECT real_name, phone, remark FROM activity_signup
                    WHERE activity_id = :activity_id AND user_id = :user_id"""
                ),
                {"activity_id": activity_id, "user_id": user_id},
            )
        ).mappings().one()

    assert signup["real_name"] == "Canonical Name"
    assert signup["phone"] == canonical_phone
    assert signup["remark"] == "note"


@pytest.mark.asyncio
async def test_activity_signup_capacity_is_safe_across_independent_sessions(
    community_database: CommunityDatabaseHarness,
) -> None:
    first_user = await community_database.create_user(real_name="First User")
    second_user = await community_database.create_user(real_name="Second User")
    activity_id = await community_database.create_activity(max_people=1)

    async def sign_up(user_id: int) -> int:
        async with community_database.sessions() as db:
            try:
                await community_service.signup_activity(db, user_id, activity_id)
            except HTTPException as exc:
                return exc.status_code
            return 201

    outcomes = await asyncio.gather(sign_up(first_user), sign_up(second_user))
    assert sorted(outcomes) == [201, 422]

    async with community_database.sessions() as db:
        active_count = int(
            (
                await db.execute(
                    text(
                        """SELECT COUNT(*) FROM activity_signup
                        WHERE activity_id = :activity_id AND status IN (0, 1)"""
                    ),
                    {"activity_id": activity_id},
                )
            ).scalar()
            or 0
        )
        current_people = int(
            (
                await db.execute(
                    text("SELECT current_people FROM offline_activity WHERE id = :activity_id"),
                    {"activity_id": activity_id},
                )
            ).scalar()
            or 0
        )

    assert active_count == 1
    assert current_people == 1


def test_community_routes_are_registered_and_require_authentication() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/community/posts",
        "/api/v1/community/posts/{post_id}",
        "/api/v1/community/posts/{post_id}/comments",
        "/api/v1/community/posts/{post_id}/collect",
        "/api/v1/community/topics",
        "/api/v1/community/topics/page",
        "/api/v1/community/topics/{topic_id}",
        "/api/v1/community/topics/{topic_id}/detail",
        "/api/v1/community/topics/{topic_id}/join",
        "/api/v1/community/activities",
        "/api/v1/community/activities/mine",
        "/api/v1/community/activities/{activity_id}",
        "/api/v1/community/activities/{activity_id}/signup",
        "/api/v1/community/banners",
        "/api/v1/community/quotas",
        "/api/v1/community/city",
        "/api/v1/community/report-reasons",
        "/api/v1/paper-planes",
        "/api/v1/paper-planes/{plane_id}/replies",
    ):
        assert path in paths, path

    for path in (
        "/api/v1/community/posts",
        "/api/v1/community/topics",
        "/api/v1/community/activities",
        "/api/v1/community/banners",
        "/api/v1/community/quotas",
        "/api/v1/community/city",
        "/api/v1/community/report-reasons",
        "/api/v1/paper-planes",
    ):
        response = client.get(path)
        assert response.status_code == 401, path


@pytest.mark.asyncio
async def test_realname_guard_rejects_nonpassed_user() -> None:
    current = CurrentUser(id=7, session_id=9, phone="13800000000", status=1, realname_status=1)
    with pytest.raises(HTTPException) as exc:
        await get_realname_verified_user(current)
    assert exc.value.status_code == 403


def test_community_interactions_require_realname() -> None:
    guarded_routes = (
        ("/api/v1/community/posts", "POST"),
        ("/api/v1/community/posts/{post_id}", "DELETE"),
        ("/api/v1/community/posts/{post_id}/like", "PUT"),
        ("/api/v1/community/posts/{post_id}/like", "DELETE"),
        ("/api/v1/community/posts/{post_id}/collect", "PUT"),
        ("/api/v1/community/posts/{post_id}/collect", "DELETE"),
        ("/api/v1/community/posts/{post_id}/comments", "POST"),
        ("/api/v1/community/comments/{comment_id}", "DELETE"),
        ("/api/v1/community/topics/{topic_id}/join", "POST"),
        ("/api/v1/community/activities/{activity_id}/signup", "POST"),
        ("/api/v1/paper-planes", "POST"),
        ("/api/v1/paper-planes/{plane_id}/replies", "POST"),
    )
    for path, method in guarded_routes:
        assert get_realname_verified_user in route_dependencies(path, method), (method, path)


def test_community_browsing_does_not_require_realname() -> None:
    browsable_routes = (
        ("/api/v1/community/posts", "GET"),
        ("/api/v1/community/posts/{post_id}", "GET"),
        ("/api/v1/community/posts/{post_id}/comments", "GET"),
        ("/api/v1/community/topics", "GET"),
        ("/api/v1/community/activities", "GET"),
        ("/api/v1/community/report-reasons", "GET"),
        ("/api/v1/paper-planes", "GET"),
    )
    for path, method in browsable_routes:
        assert get_realname_verified_user not in route_dependencies(path, method), (method, path)


@pytest.mark.asyncio
async def test_list_comments_checks_post_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[tuple[int, int]] = []

    async def fake_post(_db: object, user_id: int, post_id: int) -> object:
        checked.append((user_id, post_id))
        return object()

    monkeypatch.setattr(community_service, "get_post", fake_post)
    await community_service.list_comments(FakeSession([]), 11, 22, 1, 20)
    assert checked == [(11, 22)]


@pytest.mark.asyncio
async def test_consume_daily_uses_one_atomic_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(eval_result=1)
    monkeypatch.setattr(redis_module, "redis_client", fake)

    assert await redis_module.consume_daily("paper-plane:7:2026-07-25", 3) is True
    assert len(fake.eval_calls) == 1
    script, key_count, key, limit, ttl = fake.eval_calls[0]
    assert script == redis_module.CONSUME_DAILY_LUA
    assert (key_count, key, limit) == (1, "paper-plane:7:2026-07-25", 3)
    assert isinstance(ttl, int)
    assert 60 <= ttl <= 86_400


@pytest.mark.asyncio
async def test_consume_daily_returns_false_when_limit_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis(eval_result=0)
    monkeypatch.setattr(redis_module, "redis_client", fake)

    assert await redis_module.consume_daily("paper-plane:7:2026-07-25", 3) is False
    assert len(fake.eval_calls) == 1


@pytest.mark.asyncio
async def test_completed_idempotency_key_replays_the_saved_response() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    payload = {"content": "hello"}
    db = FakeIdempotencySession.completed(
        user_id=7,
        operation="community.post.create",
        key="post-key-0001",
        payload_hash=_payload_hash(payload),
        response_json={"id": 81, "content": "hello"},
    )

    replay = await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "post-key-0001",
        payload,
    )

    assert replay.response == {"id": 81, "content": "hello"}


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_payload() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    db = FakeIdempotencySession.completed(
        user_id=7,
        operation="community.post.create",
        key="post-key-0001",
        payload_hash=_payload_hash({"content": "first"}),
        response_json={"id": 81, "content": "first"},
    )

    with pytest.raises(HTTPException) as exc:
        await idempotency.reserve_or_replay(
            db,
            7,
            "community.post.create",
            "post-key-0001",
            {"content": "changed"},
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_in_flight_idempotency_key_rejects_a_concurrent_create() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    payload = {"content": "hello"}
    db = FakeIdempotencySession.reserved(
        user_id=7,
        operation="community.post.create",
        key="post-key-0001",
        payload_hash=_payload_hash(payload),
    )

    with pytest.raises(HTTPException) as exc:
        await idempotency.reserve_or_replay(
            db,
            7,
            "community.post.create",
            "post-key-0001",
            payload,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_insert_uses_explicit_utc_timestamps() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    db = FakeIdempotencySession()

    await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "Case-Key-0001",
        {"content": "hello"},
    )

    insert_sql = db.statements[0]
    assert "created_at, updated_at" in insert_sql
    assert "utc_timestamp(6), utc_timestamp(6)" in insert_sql


@pytest.mark.asyncio
async def test_idempotency_lookup_uses_binary_key_equality() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    payload = {"content": "hello"}
    db = FakeIdempotencySession.completed(
        user_id=7,
        operation="community.post.create",
        key="Case-Key-0001",
        payload_hash=_payload_hash(payload),
        response_json={"id": 81, "content": "hello"},
    )

    await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "Case-Key-0001",
        payload,
    )

    select_sql = next(sql for sql in db.statements if sql.startswith("select"))
    assert "binary idempotency_key = binary :idempotency_key" in select_sql


@pytest.mark.asyncio
async def test_stale_idempotency_reservation_is_taken_over_by_database_clock() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    payload = {"content": "hello"}
    db = FakeIdempotencySession(
        {
            "id": 92,
            "user_id": 7,
            "operation": "community.post.create",
            "idempotency_key": "post-key-0001",
            "payload_hash": _payload_hash(payload),
            "state": "reserved",
            "response_json": None,
            "owner_token": "stale-owner",
            # The service must not interpret a server-local naive timestamp in Python.
            "updated_at": object(),
        },
        allow_takeover=True,
    )

    reservation = await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "post-key-0001",
        payload,
    )

    assert reservation.record_id == 92
    takeover_sql = next(sql for sql in db.statements if "date_sub" in sql)
    assert "interval 5 minute" in takeover_sql


@pytest.mark.asyncio
async def test_idempotency_reservation_is_short_and_completion_is_transactional() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    db = FakeIdempotencySession()
    reservation = await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "post-key-0001",
        {"content": "hello"},
    )

    assert db.commits == 1
    assert reservation.response is None

    await idempotency.complete(db, reservation, {"id": 81, "content": "hello"})

    assert db.commits == 2
    assert db.record is not None
    assert db.record["state"] == "completed"
    saved = db.record["response_json"]
    decoded = json.loads(saved) if isinstance(saved, str) else saved
    assert decoded == {"id": 81, "content": "hello"}


@pytest.mark.asyncio
async def test_abort_removes_only_the_owned_reservation() -> None:
    idempotency = importlib.import_module("app.services.idempotency")
    db = FakeIdempotencySession()
    reservation = await idempotency.reserve_or_replay(
        db,
        7,
        "community.post.create",
        "post-key-0001",
        {"content": "hello"},
    )

    await idempotency.abort(db, reservation)

    assert db.record is None
    assert db.commits == 2


def test_create_routes_expose_optional_bounded_idempotency_key_headers() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    create_operations = (
        ("/api/v1/community/posts", "post"),
        ("/api/v1/community/posts/{post_id}/comments", "post"),
        ("/api/v1/paper-planes", "post"),
        ("/api/v1/paper-planes/{plane_id}/replies", "post"),
    )

    for path, method in create_operations:
        parameters = paths[path][method]["parameters"]
        header = next(item for item in parameters if item["name"] == "Idempotency-Key")
        assert header["in"] == "header"
        assert header["required"] is False
        schema = next(
            item
            for item in header["schema"].get("anyOf", [header["schema"]])
            if item.get("type") == "string"
        )
        assert schema["minLength"] == 8
        assert schema["maxLength"] == 128


def test_four_create_services_support_transaction_composition() -> None:
    for creator in (
        community_service.create_post,
        community_service.create_comment,
        community_service.create_paper_plane,
        community_service.reply_paper_plane,
    ):
        commit = inspect.signature(creator).parameters["commit"]
        assert commit.default is True
        assert commit.kind is inspect.Parameter.KEYWORD_ONLY


def test_idempotency_table_has_one_unique_key_per_user_operation_and_key() -> None:
    setup = Path("database_setup_marriage.py").read_text(encoding="utf-8")
    assert "'api_idempotency_record':" in setup
    assert (
        "UNIQUE KEY `uk_api_idempotency_scope` "
        "(`user_id`,`operation`,`idempotency_key`)"
    ) in setup


def test_idempotency_table_uses_case_sensitive_keys_and_explicit_utc_timestamps() -> None:
    setup = Path("database_setup_marriage.py").read_text(encoding="utf-8")
    table_sql = setup.split("'api_idempotency_record':", 1)[1].split('""",', 1)[0].lower()

    assert (
        "`idempotency_key` varchar(128) character set utf8mb4 "
        "collate utf8mb4_bin not null"
    ) in table_sql
    assert "`created_at` datetime(6) not null" in table_sql
    assert "`updated_at` datetime(6) not null" in table_sql
    assert "current_timestamp" not in table_sql


def test_idempotency_contract_migration_upgrades_legacy_table_idempotently() -> None:
    legacy = IdempotencyMigrationCursor(
        [
            {
                "Field": "idempotency_key",
                "Type": "varchar(128)",
                "Collation": "utf8mb4_unicode_ci",
                "Default": None,
                "Extra": "",
            },
            {
                "Field": "created_at",
                "Type": "datetime(6)",
                "Collation": None,
                "Default": "CURRENT_TIMESTAMP(6)",
                "Extra": "DEFAULT_GENERATED",
            },
            {
                "Field": "updated_at",
                "Type": "datetime(6)",
                "Collation": None,
                "Default": "CURRENT_TIMESTAMP(6)",
                "Extra": "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)",
            },
        ]
    )
    manager = database_setup_marriage.DatabaseManager.__new__(
        database_setup_marriage.DatabaseManager
    )

    manager._ensure_idempotency_contract(legacy)

    assert any("collate utf8mb4_bin" in sql for sql in legacy.statements)
    assert any("modify column `created_at` datetime(6) not null" in sql for sql in legacy.statements)
    assert any("modify column `updated_at` datetime(6) not null" in sql for sql in legacy.statements)
    assert any("where `state` = 'reserved'" in sql for sql in legacy.statements)

    compliant = IdempotencyMigrationCursor(
        [
            {
                "Field": "idempotency_key",
                "Type": "varchar(128)",
                "Collation": "utf8mb4_bin",
                "Default": None,
                "Extra": "",
            },
            {
                "Field": "created_at",
                "Type": "datetime(6)",
                "Collation": None,
                "Default": None,
                "Extra": "",
            },
            {
                "Field": "updated_at",
                "Type": "datetime(6)",
                "Collation": None,
                "Default": None,
                "Extra": "",
            },
        ]
    )

    manager._ensure_idempotency_contract(compliant)

    assert len(compliant.statements) == 1
    assert compliant.statements[0].startswith("show full columns")
    setup = Path("database_setup_marriage.py").read_text(encoding="utf-8")
    assert "self._ensure_idempotency_contract(cursor)" in setup


def test_idempotency_contract_migration_failure_stops_initializer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingMigrationCursor(IdempotencyMigrationCursor):
        def execute(self, statement: str) -> None:
            normalized = " ".join(statement.split()).lower()
            self.statements.append(normalized)
            if "modify column `idempotency_key`" in normalized:
                raise database_setup_marriage.pymysql.MySQLError("collation alter failed")

        def __enter__(self) -> "FailingMigrationCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeConnection:
        def __init__(self, cursor: FailingMigrationCursor) -> None:
            self._cursor = cursor
            self.commits = 0
            self.closed = False

        def cursor(self) -> FailingMigrationCursor:
            return self._cursor

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            self.closed = True

    cursor = FailingMigrationCursor(
        [
            {
                "Field": "idempotency_key",
                "Type": "varchar(128)",
                "Collation": "utf8mb4_unicode_ci",
                "Default": None,
                "Extra": "",
            }
        ]
    )
    connection = FakeConnection(cursor)

    def init_tables(manager: object, migration_cursor: FailingMigrationCursor) -> None:
        database_setup_marriage.DatabaseManager._ensure_idempotency_contract(
            manager,
            migration_cursor,
        )

    monkeypatch.setattr(database_setup_marriage, "create_database", lambda: None)
    monkeypatch.setattr(database_setup_marriage, "get_db_config", lambda: {})
    monkeypatch.setattr(
        database_setup_marriage.pymysql,
        "connect",
        lambda **_kwargs: connection,
    )
    monkeypatch.setattr(database_setup_marriage.DatabaseManager, "__init__", lambda _self: None)
    monkeypatch.setattr(database_setup_marriage.DatabaseManager, "init_all_tables", init_tables)
    caplog.set_level(logging.INFO, logger=database_setup_marriage.__name__)

    with pytest.raises(database_setup_marriage.pymysql.MySQLError, match="collation alter failed"):
        database_setup_marriage.initialize_database()

    assert connection.commits == 0
    assert connection.closed is True
    assert not any(
        record.getMessage() == "✅ 数据库表结构初始化完成。" for record in caplog.records
    )


@pytest.mark.parametrize(
    ("table_name", "column_name", "column_definition"),
    (
        ("user_profile", "community_city_name", "`community_city_name` varchar(64) DEFAULT NULL"),
        ("user_profile", "community_city_code", "`community_city_code` varchar(32) DEFAULT NULL"),
        (
            "user_profile",
            "community_city_updated_at",
            "`community_city_updated_at` datetime DEFAULT NULL",
        ),
        ("community_post", "visibility", "`visibility` tinyint NOT NULL DEFAULT '0'"),
        ("community_post", "declaration", "`declaration` varchar(32) NOT NULL DEFAULT ''"),
    ),
)
def test_required_community_column_migration_failure_stops_initializer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    class FailingCommunityColumnCursor:
        def execute(self, statement: str) -> None:
            if "ADD COLUMN" in statement and column_name in statement:
                raise database_setup_marriage.pymysql.MySQLError("community column alter failed")

        def fetchall(self) -> list[dict[str, object]]:
            return []

        def __enter__(self) -> "FailingCommunityColumnCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeConnection:
        def __init__(self, cursor: FailingCommunityColumnCursor) -> None:
            self._cursor = cursor
            self.commits = 0
            self.closed = False

        def cursor(self) -> FailingCommunityColumnCursor:
            return self._cursor

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            self.closed = True

    cursor = FailingCommunityColumnCursor()
    connection = FakeConnection(cursor)

    def init_tables(manager: object, migration_cursor: FailingCommunityColumnCursor) -> None:
        database_setup_marriage.DatabaseManager._ensure_table_columns(
            manager,
            migration_cursor,
            f"`{table_name}`",
            {column_name: column_definition},
        )

    monkeypatch.setattr(database_setup_marriage, "create_database", lambda: None)
    monkeypatch.setattr(database_setup_marriage, "get_db_config", lambda: {})
    monkeypatch.setattr(
        database_setup_marriage.pymysql,
        "connect",
        lambda **_kwargs: connection,
    )
    monkeypatch.setattr(database_setup_marriage.DatabaseManager, "__init__", lambda _self: None)
    monkeypatch.setattr(database_setup_marriage.DatabaseManager, "init_all_tables", init_tables)
    caplog.set_level(logging.INFO, logger=database_setup_marriage.__name__)

    with pytest.raises(database_setup_marriage.pymysql.MySQLError, match="community column alter failed"):
        database_setup_marriage.initialize_database()

    assert connection.commits == 0
    assert connection.closed is True
    assert not any(
        record.getMessage() == "✅ 数据库表结构初始化完成。" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_paper_plane_refund_failure_preserves_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("paper-plane insert failed")

    async def consume(_key: str, _limit: int) -> bool:
        return True

    async def refund(_key: str) -> None:
        raise HTTPException(503, detail="refund unavailable")

    monkeypatch.setattr(community_service, "consume_daily", consume)
    monkeypatch.setattr(community_service, "refund_daily", refund)

    with pytest.raises(RuntimeError, match="paper-plane insert failed"):
        await community_service.create_paper_plane(
            ScriptedSession(database_error),
            7,
            PaperPlaneCreate(content="hello"),
        )


@pytest.mark.asyncio
async def test_discovery_refund_failure_preserves_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("application insert failed")

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def viewer(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"completion_score": 100, "phone": "13800000000", "realname_status": 2}

    async def not_vip(*_args: object, **_kwargs: object) -> bool:
        return False

    async def refund(_key: str) -> None:
        raise HTTPException(503, detail="refund unavailable")

    monkeypatch.setattr(discovery_service, "_lock_user_pair", no_op)
    monkeypatch.setattr(discovery_service, "_ensure_target", no_op)
    monkeypatch.setattr(discovery_service, "_expire_pending_applications", no_op)
    monkeypatch.setattr(discovery_service, "_viewer_context", viewer)
    monkeypatch.setattr(discovery_service, "_consume_apply_quota", no_op)
    monkeypatch.setattr(discovery_service, "_is_vip", not_vip)
    monkeypatch.setattr(discovery_service, "refund_daily", refund)

    with pytest.raises(RuntimeError, match="application insert failed"):
        await discovery_service.create_application(
            ScriptedSession(None, database_error),
            7,
            8,
            ApplicationCreateRequest(message="hello"),
        )


@pytest.mark.asyncio
async def test_idempotent_paper_plane_complete_failure_refunds_quota_without_masking_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("idempotency completion failed")
    refund_calls: list[str] = []
    quota_key = "paper-plane:7:2026-07-25"

    async def reserve(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(response=None)

    async def complete(*_args: object, **_kwargs: object) -> None:
        raise database_error

    async def abort(*_args: object, **_kwargs: object) -> None:
        return None

    async def create(*_args: object, **kwargs: object) -> PaperPlaneResponse:
        assert kwargs["commit"] is False
        assert kwargs["quota_key"] == quota_key
        return PaperPlaneResponse(
            id=81,
            content="hello",
            images=[],
            city=None,
            tags=[],
            is_anonymous=True,
            reply_count=0,
            created_at=datetime.now(UTC),
        )

    async def refund(key: str) -> None:
        refund_calls.append(key)
        raise HTTPException(503, detail="refund unavailable")

    monkeypatch.setattr(community_routes, "reserve_or_replay", reserve)
    monkeypatch.setattr(community_routes, "complete", complete)
    monkeypatch.setattr(community_routes, "abort", abort)
    monkeypatch.setattr(community_routes, "create_paper_plane", create)
    monkeypatch.setattr(community_routes, "daily_quota_key", lambda *_args: quota_key, raising=False)
    monkeypatch.setattr(community_routes, "refund_daily", refund, raising=False)

    db = RollbackOnlySession()
    with pytest.raises(RuntimeError, match="idempotency completion failed"):
        await community_routes.send_plane(
            body=PaperPlaneCreate(content="hello"),
            idempotency_key="plane-key-0001",
            current=CurrentUser(
                id=7,
                session_id=9,
                phone="13800000000",
                status=1,
                realname_status=2,
            ),
            db=db,
        )

    assert refund_calls == [quota_key]
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_idempotent_cleanup_failures_preserve_completion_error_and_attempt_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_error = RuntimeError("idempotency completion failed")
    cleanup_calls: list[str] = []

    async def reserve(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(response=None)

    async def complete(*_args: object, **_kwargs: object) -> None:
        raise completion_error

    async def abort(*_args: object, **_kwargs: object) -> None:
        cleanup_calls.append("abort")
        raise RuntimeError("abort failed")

    async def creator(_commit: bool) -> PaperPlaneResponse:
        return PaperPlaneResponse(
            id=81,
            content="hello",
            images=[],
            city=None,
            tags=[],
            is_anonymous=True,
            reply_count=0,
            created_at=datetime.now(UTC),
        )

    async def compensate() -> None:
        cleanup_calls.append("refund")

    monkeypatch.setattr(community_routes, "reserve_or_replay", reserve)
    monkeypatch.setattr(community_routes, "complete", complete)
    monkeypatch.setattr(community_routes, "abort", abort)

    with pytest.raises(RuntimeError, match="idempotency completion failed"):
        await community_routes._create_idempotently(
            RollbackFailingSession(),
            7,
            "paper-plane.create",
            "plane-key-0001",
            {"content": "hello"},
            PaperPlaneResponse,
            creator,
            compensate,
        )

    assert cleanup_calls == ["refund", "abort"]


@pytest.mark.asyncio
async def test_paper_plane_rollback_failure_preserves_database_error_and_attempts_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("paper-plane insert failed")
    refund_calls: list[str] = []

    async def consume(_key: str, _limit: int) -> bool:
        return True

    async def refund(key: str) -> None:
        refund_calls.append(key)

    async def allow_text(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(community_service, "consume_daily", consume)
    monkeypatch.setattr(community_service, "refund_daily", refund)
    monkeypatch.setattr(
        "app.services.content_filter.assert_text_allowed",
        allow_text,
    )

    with pytest.raises(RuntimeError, match="paper-plane insert failed"):
        await community_service.create_paper_plane(
            RollbackFailingSession(database_error),
            7,
            PaperPlaneCreate(content="hello"),
            quota_key="paper-plane:7:2026-07-25",
        )

    assert refund_calls == ["paper-plane:7:2026-07-25"]


@pytest.mark.asyncio
async def test_application_rollback_failure_preserves_database_error_and_attempts_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("application insert failed")
    refund_calls: list[str] = []

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def viewer(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"completion_score": 100, "phone": "13800000000", "realname_status": 2}

    async def not_vip(*_args: object, **_kwargs: object) -> bool:
        return False

    async def refund(key: str) -> None:
        refund_calls.append(key)

    monkeypatch.setattr(discovery_service, "_lock_user_pair", no_op)
    monkeypatch.setattr(discovery_service, "_ensure_target", no_op)
    monkeypatch.setattr(discovery_service, "_expire_pending_applications", no_op)
    monkeypatch.setattr(discovery_service, "_viewer_context", viewer)
    monkeypatch.setattr(discovery_service, "_consume_apply_quota", no_op)
    monkeypatch.setattr(discovery_service, "_is_vip", not_vip)
    monkeypatch.setattr(discovery_service, "refund_daily", refund)

    with pytest.raises(RuntimeError, match="application insert failed"):
        await discovery_service.create_application(
            RollbackFailingSession(None, database_error),
            7,
            8,
            ApplicationCreateRequest(message="hello"),
        )

    assert len(refund_calls) == 1
    assert refund_calls[0].startswith("discovery:apply:7:")


@pytest.mark.asyncio
async def test_superlike_rollback_failure_preserves_database_error_and_attempts_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = RuntimeError("superlike insert failed")
    refund_calls: list[str] = []

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def not_vip(*_args: object, **_kwargs: object) -> bool:
        return False

    async def consume(_key: str, _limit: int) -> bool:
        return True

    async def refund(key: str) -> None:
        refund_calls.append(key)

    monkeypatch.setattr(discovery_service, "_lock_user_pair", no_op)
    monkeypatch.setattr(discovery_service, "_ensure_target", no_op)
    monkeypatch.setattr(discovery_service, "_is_vip", not_vip)
    monkeypatch.setattr(discovery_service, "consume_daily", consume)
    monkeypatch.setattr(discovery_service, "refund_daily", refund)

    db = RollbackFailingSession(
        {"phone": "13800000000", "completion_score": 100, "realname_status": 2},
        None,
        database_error,
    )
    with pytest.raises(RuntimeError, match="superlike insert failed"):
        await discovery_service.create_superlike(db, 7, 8, "superlike-key-0001")

    assert len(refund_calls) == 1
    assert refund_calls[0].startswith("discovery:superlike:7:")
