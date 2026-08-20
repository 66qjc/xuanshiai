from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.main import app
from app.schemas.finance import CommissionRuleCreate, ProductCommissionConfigCreate
from app.schemas.organization import StoreCreate


client = TestClient(app)


def test_business_routes_are_registered_and_protected() -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/organizations/stores" in paths
    assert "/api/v1/promotions/attributions" in paths
    assert "/api/v1/matchmaker/meetings/requests" in paths
    assert "/api/v1/matchmaker/meetings/requests/from-service" in paths
    assert "/api/v1/finance/orders" in paths
    assert "/api/v1/finance/commission-entries" in paths
    assert "/api/v1/admin/finance/commission-rules" in paths
    assert "/api/v1/admin/finance/product-commission-rules/{product_id}" in paths
    assert "/api/v1/admin/finance/report" in paths
    assert "/api/v1/admin/finance/orders/{order_id}/refund" in paths
    tags = {item["name"]: item["description"] for item in schema["tags"]}
    expected_tags = {
        "账号与认证": "登录、账号身份、实名认证和账号安全。",
        "首页与资料": "推荐、搜索、公开资料和用户资料管理。",
        "红娘": "红娘申请、服务牵线、约见申请和约会记录。",
        "社区": "帖子、评论、互动、话题和纸飞机。",
        "消息": "申请认识、匹配、聊天、通知和关系安全。",
        "管理后台": "内容、消息、红娘、财务和运营治理。",
        "组织与归属": "门店、组织成员、资源分派、推广和合伙团队。",
        "财务与结算": "订单、分成、账本、余额和提现。",
    }
    assert {name: tags[name] for name in expected_tags} == expected_tags
    assert paths["/api/v1/matchmakers"]["get"]["tags"] == ["红娘"]
    assert paths["/api/v1/admin/finance/report"]["get"]["tags"] == ["管理后台"]
    assert paths["/api/v1/finance/balance"]["get"]["tags"] == ["财务与结算"]
    assert client.post("/api/v1/finance/orders", json={"product_type": 1, "product_name": "会员", "amount": "99.00"}).status_code == 401


def test_business_schemas_validate_contracts() -> None:
    assert StoreCreate(code="store-01", name="上海门店").auto_redirect is False
    rule = CommissionRuleCreate(
        beneficiary_type="promoter", name="推广分成", mode="rate", rate_percent="10.0000"
    )
    assert rule.rate_percent == 10
    with pytest.raises(ValidationError):
        CommissionRuleCreate(beneficiary_type="store", name="门店", mode="rate")
    with pytest.raises(ValidationError):
        ProductCommissionConfigCreate(beneficiary_type="store", mode="rate")
    assert ProductCommissionConfigCreate(
        beneficiary_type="service_matchmaker", mode="rate", rate_percent="10.0000"
    ).rate_percent == 10
