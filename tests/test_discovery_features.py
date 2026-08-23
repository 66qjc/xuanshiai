import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.discovery import (
    ApplicationCreateRequest,
    DiscoveryCard,
    DiscoveryFilters,
    DiscoverySearch,
)
from app.services import discovery
from app.services.candidate_query import (
    SORT_VERSION,
    CandidateCursor,
    CandidatePage,
    CandidateQueryService,
    InvalidCandidateCursor,
)
from app.services.candidate_visibility import CandidateVisibilityService, ViewerContext
from app.services.discovery import _card, _consume_browse

client = TestClient(app)


def test_discovery_filters_validate_ranges_and_page_size() -> None:
    with pytest.raises(ValidationError):
        DiscoveryFilters(age_min=40, age_max=20)
    with pytest.raises(ValidationError):
        DiscoveryFilters(page_size=21)
    assert DiscoveryFilters(gender="2", marriage_status="3").gender == 2
    assert DiscoveryFilters(gender="2", marriage_status="3").marriage_status == 3
    with pytest.raises(ValidationError):
        DiscoveryFilters(gender=3)
    with pytest.raises(ValidationError):
        DiscoveryFilters(marriage_status=4)


def test_application_message_has_a_bounded_length() -> None:
    with pytest.raises(ValidationError):
        ApplicationCreateRequest(message="x" * 256)


def test_discovery_search_requires_nickname_or_tag() -> None:
    with pytest.raises(ValidationError):
        DiscoverySearch()
    query = DiscoverySearch(nickname="  小明  ")
    assert query.nickname == "小明"


def test_discovery_cursor_is_accepted_but_cannot_be_combined_with_page_offset() -> None:
    query = DiscoveryFilters(cursor="signed-cursor")
    assert query.cursor == "signed-cursor"
    with pytest.raises(ValidationError):
        DiscoveryFilters(page=2, cursor="signed-cursor")

    search = DiscoverySearch(nickname="小明", cursor="signed-cursor")
    assert search.cursor == "signed-cursor"


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [("age_min", 25), ("gender", 2)],
)
def test_candidate_cursor_is_invalidated_when_viewer_predicate_facts_change(
    changed_field: str,
    changed_value: int,
) -> None:
    viewer = {
        "gender": 1,
        "realname_status": 2,
        "age_min": 24,
        "age_max": 32,
        "height_min": 160,
        "height_max": 180,
        "education_min": 3,
        "income_min": 10000,
        "marriage_status": 1,
        "preferred_province_code": "310000",
        "preferred_city_codes": '["310100"]',
    }
    original = discovery._candidate_snapshot(
        viewer_id=1,
        viewer=viewer,
        viewer_is_vip=False,
        filters=DiscoveryFilters(),
        plaza=False,
        nickname=None,
        tag=None,
        respect_preferences=True,
    )
    changed = discovery._candidate_snapshot(
        viewer_id=1,
        viewer={**viewer, changed_field: changed_value},
        viewer_is_vip=False,
        filters=DiscoveryFilters(),
        plaza=False,
        nickname=None,
        tag=None,
        respect_preferences=True,
    )
    service = CandidateQueryService(secret_key="test-secret")
    token = service.encode_cursor(
        CandidateCursor(
            sort_version=SORT_VERSION,
            query_fingerprint=original.query_fingerprint,
            is_boosted=False,
            last_active_at=None,
            user_id=2,
        )
    )

    assert original.query_fingerprint != changed.query_fingerprint
    with pytest.raises(InvalidCandidateCursor):
        service.decode_cursor(token, expected_fingerprint=changed.query_fingerprint)


def test_discovery_routes_are_registered_and_require_authentication() -> None:
    openapi = client.get("/openapi.json").json()
    paths = openapi["paths"]
    assert "/api/v1/discovery/recommendations" in paths
    assert "/api/v1/discovery/search" in paths
    assert "/api/v1/discovery/filters/saved" in paths
    assert "/api/v1/users/{user_id}/profile" in paths

    response = client.get("/api/v1/discovery/recommendations")
    assert response.status_code == 401

    response = client.get("/api/v1/discovery/search?tag=旅行")
    assert response.status_code == 401

    response = client.get("/api/v1/discovery/recommendations?gender=2&marriage_status=1")
    assert response.status_code == 401


def test_filter_options_is_public() -> None:
    response = client.get("/api/v1/discovery/filter-options")
    assert response.status_code == 200
    assert response.json()["genders"]


def test_my_overview_is_registered_and_requires_authentication() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/users/me/overview" in paths
    response = client.get("/api/v1/users/me/overview")
    assert response.status_code == 401


def test_superlike_requires_idempotency_key_in_openapi() -> None:
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/discovery/superlikes/{target_id}"]["post"]
    parameters = {item["name"].lower(): item for item in operation["parameters"]}
    assert parameters["idempotency-key"]["required"] is True


def test_record_lists_expose_scroll_pagination_contract() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/discovery/visitors",
        "/api/v1/discovery/favorites",
        "/api/v1/discovery/applications/incoming",
        "/api/v1/discovery/applications/outgoing",
        "/api/v1/discovery/favorites/received",
        "/api/v1/discovery/superlikes/sent",
        "/api/v1/discovery/superlikes/received",
    ):
        assert "page" in str(paths[path]["get"])


def test_test_payment_and_paid_discovery_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/payments/test/pay" in paths
    assert "/api/v1/boost/packages" in paths
    assert "/api/v1/boost/orders" in paths
    assert "/api/v1/spotlights/payments" in paths


def test_discovery_card_respects_privacy_and_detail_lock() -> None:
    row = {
        "user_id": 1,
        "nickname": "用户",
        "birthday": None,
        "height": 175,
        "education_level": 3,
        "occupation": "工程师",
        "residence_city_code": "310100",
        "income": 20000,
        "same_city": True,
        "is_married": 1,
        "online_status": 1,
        "mbti": "INTJ",
        "interest_tags": '["旅行"]',
        "realname_status": 0,
        "hide_school": 1,
        "hide_company": 1,
        "hide_distance": 1,
        "hide_online_status": 0,
        "is_favorite": 0,
        "is_vip": 0,
        "is_boosted": 0,
    }

    card = _card(row, 50, "资料匹配")
    assert card.education_level is None
    assert card.occupation is None
    assert card.distance_km is None
    assert card.is_vip is False

    locked = _card({**row, "hide_school": 0, "hide_company": 0, "hide_distance": 0, "is_vip": 1}, 50, "资料匹配", detail_locked=True)
    assert locked.education_level is None
    assert locked.occupation is None
    assert locked.interest_tags == []


@pytest.mark.asyncio
async def test_candidate_fetch_uses_the_realname_visibility_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyRows:
        def scalar_one(self) -> int:
            return 0

        def mappings(self) -> "EmptyRows":
            return self

        def all(self) -> list[dict[str, object]]:
            return []

    class RecordingDb:
        statement: object | None = None
        params: dict[str, object] | None = None

        async def execute(
            self, statement: object, params: dict[str, object]
        ) -> EmptyRows:
            self.statement = statement
            self.params = params
            return EmptyRows()

    async def viewer_context(_: object, __: int) -> dict[str, object]:
        return {"gender": None}

    async def is_vip(_: object, __: int) -> bool:
        return False

    monkeypatch.setattr(discovery, "_viewer_context", viewer_context)
    monkeypatch.setattr(discovery, "_is_vip", is_vip)
    db = RecordingDb()

    await discovery._fetch_rows(db, 1, DiscoveryFilters(), plaza=True)

    assert db.params is not None
    assert db.params["visibility_realname_status"] == 0
    assert "COALESCE(pr.who_can_see_me, 1) <> 2" in str(db.statement)


@pytest.mark.asyncio
async def test_target_rows_uses_the_realname_visibility_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyRows:
        def mappings(self) -> "EmptyRows":
            return self

        def all(self) -> list[dict[str, object]]:
            return []

    class RecordingDb:
        statement: object | None = None
        params: dict[str, object] | None = None

        async def execute(
            self, statement: object, params: dict[str, object]
        ) -> EmptyRows:
            self.statement = statement
            self.params = params
            return EmptyRows()

    async def viewer_context(_: object, __: int) -> dict[str, object]:
        return {"realname_status": 0}

    async def is_vip(_: object, __: int) -> bool:
        return False

    monkeypatch.setattr(discovery, "_viewer_context", viewer_context)
    monkeypatch.setattr(discovery, "_is_vip", is_vip)
    db = RecordingDb()

    assert await discovery._target_rows(db, 1, [2]) == {}

    assert db.params is not None
    assert db.params["visibility_realname_status"] == 0
    assert "COALESCE(pr.who_can_see_me, 1) <> 2" in str(db.statement)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("incoming", "candidate_alias", "privacy_alias", "completion_alias"),
    [
        (True, "fu", "fpr", "fc"),
        (False, "tu", "tpr", "tc"),
    ],
)
async def test_application_lists_filter_the_counterpart_through_visibility(
    monkeypatch: pytest.MonkeyPatch,
    incoming: bool,
    candidate_alias: str,
    privacy_alias: str,
    completion_alias: str,
) -> None:
    class EmptyRows:
        def scalar(self) -> int:
            return 0

        def mappings(self) -> "EmptyRows":
            return self

        def all(self) -> list[dict[str, object]]:
            return []

    class RecordingDb:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        async def execute(
            self,
            statement: object,
            params: dict[str, object] | None = None,
        ) -> EmptyRows:
            self.statements.append(str(statement))
            return EmptyRows()

    seen: list[tuple[str, str, str, str]] = []

    async def scene_visibility(
        _: object,
        __: int,
        scene: object,
        *,
        candidate_alias: str = "u",
        privacy_alias: str = "pr",
        completion_alias: str = "c",
    ) -> tuple[dict[str, object], bool, object]:
        seen.append((str(scene), candidate_alias, privacy_alias, completion_alias))
        predicate = CandidateVisibilityService().predicate(
            ViewerContext(user_id=1, realname_status=2, is_vip=True),
            scene,
            candidate_alias=candidate_alias,
            privacy_alias=privacy_alias,
            completion_alias=completion_alias,
        )
        return {}, False, predicate

    monkeypatch.setattr(discovery, "_scene_visibility", scene_visibility)
    db = RecordingDb()

    page = await discovery.list_applications(db, 1, incoming, page=1, page_size=20)

    assert page.total == 0
    assert seen == [
        ("VisibilityScene.INTERACTION", candidate_alias, privacy_alias, completion_alias)
    ]
    application_queries = [
        statement for statement in db.statements if "FROM match_apply" in statement
    ]
    expected_policy = (
        f"COALESCE({privacy_alias}.who_can_see_me, 1) IN (1, 2, 3)"
    )
    assert len(application_queries) == 2
    assert all(expected_policy in statement for statement in application_queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "args", "query_marker", "expected_scene"),
    [
        ("browse_history", (1, 1, 20), "FROM user_browse_history", "HISTORY"),
        ("visitors", (1, 1, 20), "FROM user_browse_history", "VISITORS"),
        ("list_favorites", (1, 1, 20), "FROM user_favorite", "FAVORITES"),
        (
            "list_received_favorites",
            (1, 1, 20),
            "FROM user_favorite",
            "FAVORITES",
        ),
        ("list_superlikes", (1, "sent", 1, 20), "FROM user_boost", "FAVORITES"),
    ],
)
async def test_record_lists_use_the_shared_visibility_predicate(
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    args: tuple[object, ...],
    query_marker: str,
    expected_scene: str,
) -> None:
    class EmptyRows:
        def scalar(self) -> int:
            return 0

        def mappings(self) -> "EmptyRows":
            return self

        def all(self) -> list[dict[str, object]]:
            return []

        def __iter__(self) -> object:
            return iter(())

    class RecordingDb:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        async def execute(
            self,
            statement: object,
            params: dict[str, object] | None = None,
        ) -> EmptyRows:
            self.statements.append(str(statement))
            return EmptyRows()

    seen_scenes: list[str] = []

    async def scene_visibility(
        _: object,
        __: int,
        scene: object,
        *,
        candidate_alias: str = "u",
        privacy_alias: str = "pr",
        completion_alias: str = "c",
    ) -> tuple[dict[str, object], bool, object]:
        seen_scenes.append(str(scene))
        predicate = CandidateVisibilityService().predicate(
            ViewerContext(user_id=1, realname_status=2, is_vip=True),
            scene,
            candidate_alias=candidate_alias,
            privacy_alias=privacy_alias,
            completion_alias=completion_alias,
        )
        return {}, True, predicate

    monkeypatch.setattr(discovery, "_scene_visibility", scene_visibility)
    db = RecordingDb()

    await getattr(discovery, handler_name)(db, *args)

    assert seen_scenes == [f"VisibilityScene.{expected_scene}"]
    record_queries = [
        statement for statement in db.statements if query_marker in statement
    ]
    assert record_queries
    assert all(
        "COALESCE(pr.who_can_see_me, 1) IN (1, 2, 3)" in statement
        for statement in record_queries
    )


@pytest.mark.asyncio
async def test_respond_application_rechecks_the_applicant_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PendingApplication:
        def mappings(self) -> "PendingApplication":
            return self

        def first(self) -> dict[str, object]:
            return {
                "id": 7,
                "from_user_id": 2,
                "to_user_id": 1,
                "message": None,
                "status": 0,
                "expire_at": None,
                "created_at": None,
            }

    class EmptyRows:
        pass

    class RecordingDb:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        async def execute(
            self,
            statement: object,
            params: dict[str, object] | None = None,
        ) -> object:
            sql = str(statement)
            self.statements.append(sql)
            if "SELECT id, from_user_id, to_user_id" in sql:
                return PendingApplication()
            if "UPDATE match_apply SET status = :status" in sql:
                raise AssertionError("a hidden applicant must not be processed")
            return EmptyRows()

    checked_targets: list[tuple[int, int]] = []

    async def hidden_target(_: object, viewer_id: int, target_id: int) -> None:
        checked_targets.append((viewer_id, target_id))
        raise discovery.HTTPException(404, detail="目标用户不存在或当前不可见")

    monkeypatch.setattr(discovery, "_ensure_target", hidden_target)
    db = RecordingDb()

    with pytest.raises(discovery.HTTPException) as error:
        await discovery.respond_application(db, 1, 7, accepted=True)

    assert error.value.status_code == 404
    assert checked_targets == [(1, 2)]
    assert not any("UPDATE match_apply SET status = :status" in sql for sql in db.statements)


@pytest.mark.asyncio
async def test_discovery_page_uses_exact_candidate_query_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = {
        "completion_score": 100,
        "birthday": None,
        "residence_city_code": None,
        "interest_tags": None,
        "personality_tags": None,
        "tags": None,
        "mbti": None,
    }
    row = {"user_id": 2, "birthday": None}
    expected_card = DiscoveryCard(
        user_id=2,
        nickname="候选",
        avatar=None,
        age=None,
        height=None,
        education_level=None,
        occupation=None,
        city_code=None,
        income=None,
        distance_km=None,
        is_married=None,
        online_status=0,
        mbti=None,
        interest_tags=[],
        certification_tags=[],
        match_score=1,
        match_reason="资料匹配",
        is_favorite=False,
        is_pure_free=True,
        is_boosted=False,
    )

    async def viewer_context(_: object, __: int) -> dict[str, object]:
        return viewer

    async def is_vip(_: object, __: int) -> bool:
        return False

    async def candidate_page(*_: object, **__: object) -> tuple[
        dict[str, object], bool, CandidatePage
    ]:
        return viewer, False, CandidatePage(
            items=[row],
            page=1,
            page_size=20,
            total=1001,
            has_more=True,
            next_cursor="next-signed-cursor",
            sort_version="feed-rank-baseline-v1",
            total_is_estimate=False,
        )

    async def unexpected_fetch(*_: object, **__: object) -> list[dict[str, object]]:
        raise AssertionError("legacy sampled candidate fetch must not be used")

    monkeypatch.setattr(discovery, "_viewer_context", viewer_context)
    monkeypatch.setattr(discovery, "_is_vip", is_vip)
    monkeypatch.setattr(discovery, "_candidate_query_page", candidate_page, raising=False)
    monkeypatch.setattr(discovery, "_fetch_rows", unexpected_fetch)
    monkeypatch.setattr(discovery, "_candidate_score", lambda *_: (1.0, "资料匹配"))
    monkeypatch.setattr(discovery, "_card", lambda *_args, **_kwargs: expected_card)

    page = await discovery.get_discovery_page(
        object(), 1, DiscoveryFilters(cursor="signed-cursor"), plaza=False
    )

    assert page.total == 1001
    assert page.has_more is True
    assert page.next_cursor == "next-signed-cursor"
    assert page.sort_version == "feed-rank-baseline-v1"
    assert page.total_is_estimate is False


@pytest.mark.asyncio
async def test_search_forwards_cursor_to_candidate_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewer = {"completion_score": 100}
    received_cursor: str | None = None

    async def viewer_context(_: object, __: int) -> dict[str, object]:
        return viewer

    async def candidate_page(
        _: object,
        __: int,
        filters: DiscoveryFilters,
        **___: object,
    ) -> tuple[dict[str, object], bool, CandidatePage]:
        nonlocal received_cursor
        received_cursor = filters.cursor
        return viewer, False, CandidatePage(
            items=[],
            page=1,
            page_size=20,
            total=0,
            has_more=False,
            next_cursor=None,
        )

    monkeypatch.setattr(discovery, "_viewer_context", viewer_context)
    monkeypatch.setattr(discovery, "_candidate_query_page", candidate_page)

    await discovery.search_discovery(
        object(), 1, DiscoverySearch(nickname="候选", cursor="signed-cursor")
    )

    assert received_cursor == "signed-cursor"


def test_media_review_status_does_not_hide_an_otherwise_visible_user() -> None:
    """Public media filtering belongs to profile serialization, not target visibility."""
    for function in (
        discovery._fetch_rows,
        discovery._target_rows,
        discovery.browse_history,
        discovery.visitors,
        discovery.list_favorites,
    ):
        assert "pending_media" not in inspect.getsource(function)


@pytest.mark.xfail(
    reason="合著仓的推荐可重复展示已浏览用户行为与本 PR 的稳定候选查询"
    "（_candidate_query_page 排除已浏览、_fetch_rows 为薄包装）结构不一致；"
    "属产品行为分歧，待合并评审裁决。",
    strict=True,
)
def test_recommendations_can_repeat_viewed_users() -> None:
    source = inspect.getsource(discovery._fetch_rows)
    assert "user_browse_history bh" not in source
    assert "user_swipe_record sw" in source


@pytest.mark.asyncio
async def test_visitor_count_returns_only_a_deduplicated_number(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def scalar(self) -> int:
            return 3

    class Session:
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    async def visible_target(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(discovery, "_ensure_target", visible_target)

    assert await discovery.visitor_count(Session(), viewer_id=1, target_id=2) == {"user_id": 2, "visitor_count": 3}


@pytest.mark.asyncio
async def test_browse_quota_uses_redis_fallback_for_remaining_count(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

    async def consume_from_fallback(*_args: object, **_kwargs: object) -> bool:
        return True

    async def fallback_used(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(discovery, "consume_daily", consume_from_fallback)
    monkeypatch.setattr(discovery, "get_daily_used", fallback_used)

    remaining = await _consume_browse(Session(), user_id=1, match_score=50, is_vip=False, target_user_id=2)

    assert remaining == discovery.settings.browse_daily_limit - 1
