import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.discovery import ApplicationCreateRequest, DiscoveryFilters, DiscoverySearch
from app.services import discovery
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
    assert locked.is_vip is True


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
