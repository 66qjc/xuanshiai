import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.matchmaker import (
    MatchmakerContactExchangeCreate,
    MatchmakerContactExchangeUpdate,
    MatchmakerServiceOrderCreate,
    MatchmakerServiceProductCreate,
    MatchmakerServiceProductUpdate,
    MatchmakerServiceRequestCreate,
    MatchmakerServiceRequestResponse,
    MatchmakerServiceRequestUpdate,
)


client = TestClient(app)


def test_matchmaker_service_request_schema_validates_business_rules() -> None:
    request = MatchmakerServiceRequestCreate(order_no="XM202607240001", requirement="希望寻找认真稳定的婚恋关系")
    assert request.order_no == "XM202607240001"
    with pytest.raises(ValidationError):
        MatchmakerServiceRequestCreate(matchmaker_id=1, requirement="太短")
    with pytest.raises(ValidationError):
        MatchmakerServiceRequestUpdate(status=2)
    assert MatchmakerServiceRequestUpdate(status=1).status == 1


def test_matchmaker_paid_service_contracts() -> None:
    product = MatchmakerServiceProductCreate(
        code="paid_matchmaking", name="付费牵线", service_type=1,
        price="99.00", description="支付后获得红娘微信服务",
    )
    assert product.service_type == 1
    order = MatchmakerServiceOrderCreate(
        product_id=1, matchmaker_id=2, requirement="希望寻找认真稳定的婚恋关系"
    )
    assert order.product_id == 1
    assert MatchmakerServiceProductUpdate(active=False).active is False
    with pytest.raises(ValidationError):
        MatchmakerServiceProductCreate(
            code="free_matchmaking", name="免费牵线", service_type=2,
            price="99.00", description="不应开放免费红娘服务",
        )


def test_matchmaker_routes_are_registered_and_require_authentication() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/matchmakers" in paths
    assert "/api/v1/matchmakers/{matchmaker_id}" in paths
    assert "/api/v1/matchmaker/service-requests" in paths
    assert "/api/v1/matchmaker/service-requests/mine" in paths
    assert "/api/v1/matchmaker/service-requests/assigned" in paths
    assert "/api/v1/matchmaker/service-products" in paths
    assert "/api/v1/matchmaker/service-products/{product_id}" in paths
    assert "get" in paths["/api/v1/matchmaker/service-products/{product_id}"]
    assert "/api/v1/matchmaker/service-requests/orders" in paths
    assert "get" in paths["/api/v1/matchmaker/service-requests/orders"]
    assert "/api/v1/matchmaker/service-requests/{service_id}" in paths
    assert "/api/v1/matchmaker/service-requests/{service_id}/contact" in paths
    assert "/api/v1/matchmaker/service-requests/{service_id}/contact-exchanges" in paths
    assert "/api/v1/matchmaker/service-requests/contact-exchanges/{exchange_id}" in paths
    assert "/api/v1/matchmaker/service-requests/contact-exchanges/{exchange_id}/contacts" in paths
    assert "/api/v1/admin/matchmaker/service-requests" in paths
    assert client.post(
        "/api/v1/matchmaker/service-requests",
        json={"matchmaker_id": 1, "requirement": "希望寻找认真稳定的婚恋关系"},
    ).status_code == 401


def test_matchmaker_public_list_does_not_require_authentication() -> None:
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/matchmakers"]["get"]
    security = operation.get("security", [])
    assert security == []


def test_matchmaker_fixed_routes_precede_dynamic_service_route() -> None:
    route_paths = list(client.get("/openapi.json").json()["paths"])
    orders_index = route_paths.index("/api/v1/matchmaker/service-requests/orders")
    assigned_index = route_paths.index("/api/v1/matchmaker/service-requests/assigned")
    dynamic_index = route_paths.index("/api/v1/matchmaker/service-requests/{service_id}")
    assert orders_index < dynamic_index
    assert assigned_index < dynamic_index


def test_matchmaker_service_response_has_no_unapproved_workflow_or_contact_fields() -> None:
    fields = set(MatchmakerServiceRequestResponse.model_fields)
    assert fields == {
        "id", "user_id", "matchmaker_id", "service_type", "status", "order_id",
        "product_id", "requirement", "feedback", "created_at", "updated_at",
        "start_at", "end_at",
    }
    assert not fields.intersection({"wechat_contact", "phone", "chat_id", "meeting_id", "match_result"})


def test_matchmaker_openapi_does_not_add_in_app_delivery_workflow_routes() -> None:
    paths = [path for path in client.get("/openapi.json").json()["paths"] if "/matchmaker" in path]
    # Meeting association routes already exist for compatibility; this guards
    # against adding new in-app chat or matchmaking-result workflows.
    forbidden_fragments = ("/chat", "/conversation", "/match-result", "/workflow")
    assert not any(any(fragment in path for fragment in forbidden_fragments) for path in paths)


def test_parent_client_can_use_public_catalog_but_not_private_order_queries() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/v1/matchmaker/service-products/{product_id}"]["get"].get("security", []) == []
    assert paths["/api/v1/matchmaker/service-requests/orders"]["get"].get("security")
    assert paths["/api/v1/matchmaker/service-requests/{service_id}"]["get"].get("security")


def test_contact_exchange_schema_requires_explicit_consent_action() -> None:
    assert MatchmakerContactExchangeCreate(target_user_id=2).target_user_id == 2
    assert MatchmakerContactExchangeUpdate(action="CONSENT").action == "CONSENT"
    with pytest.raises(ValidationError):
        MatchmakerContactExchangeUpdate(action="DELIVER")
