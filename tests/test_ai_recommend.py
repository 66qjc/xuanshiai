"""三类推荐打分核心（WP-P6b）单测：纯函数，不触 DB/LLM/任务系统。

量纲锁定：score 0..100（与 compatibility 引擎/快照一致），coverage 0..1。
"""

from __future__ import annotations

from app.services.ai.compatibility import (
    COVERAGE_THRESHOLD,
    REASON_AGE,
    REASON_CITY,
    REASON_INTEREST,
)
from app.services.ai.recommend import (
    RecommendationScore,
    positive_dimension_codes,
    score_i_like,
    score_likes_me,
    similarity_score,
)


def _rich_preference() -> dict:
    return {
        "age": {"min": 25, "max": 35},
        "city_code": ["3100000"],
        "interest_tags": ["hiking", "摄影"],
    }


def _matching_profile() -> dict:
    return {
        "age": 30,
        "city_code": "3100000",
        "interest_tags": ["hiking", "travel"],
    }


def test_score_i_like_known_dimensions_weighted() -> None:
    card = score_i_like(_rich_preference(), _matching_profile())
    assert isinstance(card, RecommendationScore)
    # age(20)*100 + city(15)*100 + interest(15)*50，其余维度未知
    assert card.score == 85.0
    assert card.coverage == 0.5  # 50/100 可用权重
    assert REASON_AGE in card.reason_codes
    assert REASON_CITY in card.reason_codes
    assert REASON_INTEREST in card.reason_codes


def test_score_i_like_below_coverage_threshold_is_none() -> None:
    card = score_i_like({"age": {"min": 25, "max": 35}}, {"age": 30})
    assert COVERAGE_THRESHOLD == 0.5
    assert card.score is None
    assert card.coverage == 0.2


def test_score_i_like_no_dimensions_at_all() -> None:
    card = score_i_like({}, {})
    assert card.score is None
    assert card.coverage == 0.0


def test_score_likes_me_is_directional_reverse() -> None:
    """likes_me = 对方的理想型投影 × 我的个人画像，与 i_like 同一套规则。"""
    card = score_likes_me(_rich_preference(), _matching_profile())
    assert card.score == 85.0
    assert card.coverage == 0.5
    assert REASON_AGE in card.reason_codes


def test_similarity_score_same_city_close_age_ranks_high() -> None:
    a = {
        "interest_tags": ["a", "b", "c"],
        "city_code": "3100000",
        "age": 30,
        "height_cm": 175,
    }
    b = {
        "interest_tags": ["a", "b"],
        "city_code": "3100000",
        "age": 32,
        "height_cm": 175,
    }
    card = similarity_score(a, b)
    # Jaccard 按维度先舍入(2/3→66.67, w0.30) + city(0.10) + age(w0.10) + height(w0.10)
    assert card.score == round((0.30 * 66.67 + 0.10 * 100 + 0.10 * 100 + 0.10 * 100) / 0.60, 2)
    assert card.coverage == 0.6
    assert "SIM_INTEREST_TAGS" in card.reason_codes
    assert "SIM_CITY_CODE" in card.reason_codes


def test_similarity_score_symmetric() -> None:
    a = {"interest_tags": ["a", "b"], "age": 30}
    b = {"interest_tags": ["b", "a"], "age": 31}
    assert similarity_score(a, b).score == similarity_score(b, a).score


def test_similarity_score_both_empty_is_none() -> None:
    card = similarity_score({}, {})
    assert card.score is None
    assert card.coverage == 0.0


def test_similarity_score_below_coverage_is_none() -> None:
    card = similarity_score({"height_cm": 175}, {"height_cm": 176})
    assert card.score is None  # 仅 0.10 权重可用 < 0.5


def test_positive_dimension_codes_only_positive_hits() -> None:
    codes = positive_dimension_codes(
        {"age": {"min": 25, "max": 35}, "city_code": ["3100000"]},
        {"age": 30, "city_code": "4400000"},
    )
    assert REASON_AGE in codes
    assert REASON_CITY not in codes  # 已知但不满足 → 不产正向码
    assert "DIMENSION_UNKNOWN" in codes  # 缺失维度显式标注


def test_scores_round_to_two_decimals() -> None:
    card = similarity_score(
        {"interest_tags": ["a", "b", "c", "d", "e", "f", "g"], "age": 30, "city_code": "3100000"},
        {"interest_tags": ["a"], "age": 30, "city_code": "3100000"},
    )
    assert card.score is not None
    assert card.score == round(card.score, 2)


# ----------------------------------------------------------------------
# WP-P6e 读取端点（API 层，monkeypatch 服务函数）
# ----------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.api.dependencies import CurrentUser, get_current_user, get_db  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


class _FakeDb:
    """API 单测的 DB 桩：路由的读路径不触库（服务函数被 monkeypatch）。"""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _override_auth(user_id: int = 9_876_549_001) -> None:
    def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=user_id, session_id=1, phone="13800000000", status=1, realname_status=2
        )

    def fake_db():
        yield _FakeDb()

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _enable_recommend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_recommend_enabled", True)


def test_api_recommendations_returns_ranked_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routes.ai_recommend as route_mod

    async def fake_read(db, viewer_id, view_kind, limit):
        assert view_kind == "i_like"
        return [
            {
                "target_user_id": 2,
                "score": 85.0,
                "coverage": 0.5,
                "rank_no": 1,
                "engine": "llm-v1",
                "reason_codes": ["AGE_MUTUAL_WITHIN_RANGE"],
                "reason_texts": ["年龄正处你期待区间"],
            },
            {
                "target_user_id": 3,
                "score": 70.0,
                "coverage": 0.6,
                "rank_no": 2,
                "engine": "rule-v1",
                "reason_codes": [],
                "reason_texts": [],
            },
        ]

    called = {"enqueue": False}

    async def fail_enqueue(db, viewer_id):
        called["enqueue"] = True
        return None

    monkeypatch.setattr(route_mod, "read_recommendations", fake_read)
    monkeypatch.setattr(route_mod, "enqueue_recommendation_rebuild", fail_enqueue)
    _enable_recommend(monkeypatch)
    _override_auth()
    try:
        response = client.get("/api/v1/ai/recommendations", params={"view": "i_like"})
    finally:
        _clear_overrides()
    assert response.status_code == 200
    body = response.json()
    assert body["regenerating"] is False
    assert [item["rank_no"] for item in body["items"]] == [1, 2]
    assert body["items"][0]["engine"] == "llm-v1"
    assert body["items"][0]["reason_texts"] == ["年龄正处你期待区间"]
    assert called["enqueue"] is False


def test_api_recommendations_miss_triggers_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routes.ai_recommend as route_mod

    async def fake_read(db, viewer_id, view_kind, limit):
        return []

    enqueued = {"count": 0}

    async def fake_enqueue(db, viewer_id):
        enqueued["count"] += 1
        return SimpleNamespace(status="queued")

    monkeypatch.setattr(route_mod, "read_recommendations", fake_read)
    monkeypatch.setattr(route_mod, "enqueue_recommendation_rebuild", fake_enqueue)
    _enable_recommend(monkeypatch)
    _override_auth()
    try:
        response = client.get("/api/v1/ai/recommendations", params={"view": "similar"})
    finally:
        _clear_overrides()
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["regenerating"] is True
    assert enqueued["count"] == 1


def test_api_recommendations_terminal_task_is_not_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同日任务已终态（如空池 succeeded）→ 如实 regenerating=false，禁止无限轮询。"""
    import app.api.routes.ai_recommend as route_mod
    from types import SimpleNamespace

    async def fake_read(db, viewer_id, view_kind, limit):
        return []

    async def fake_enqueue(db, viewer_id):
        return SimpleNamespace(status="succeeded")

    monkeypatch.setattr(route_mod, "read_recommendations", fake_read)
    monkeypatch.setattr(route_mod, "enqueue_recommendation_rebuild", fake_enqueue)
    _enable_recommend(monkeypatch)
    _override_auth()
    try:
        response = client.get("/api/v1/ai/recommendations", params={"view": "i_like"})
    finally:
        _clear_overrides()
    assert response.status_code == 200
    assert response.json()["regenerating"] is False


def test_api_recommendations_no_task_is_not_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无授权/无投影（未入队）→ regenerating=false（诚实空列表）。"""
    import app.api.routes.ai_recommend as route_mod

    async def fake_read(db, viewer_id, view_kind, limit):
        return []

    async def fake_enqueue(db, viewer_id):
        return None

    monkeypatch.setattr(route_mod, "read_recommendations", fake_read)
    monkeypatch.setattr(route_mod, "enqueue_recommendation_rebuild", fake_enqueue)
    _enable_recommend(monkeypatch)
    _override_auth()
    try:
        response = client.get("/api/v1/ai/recommendations", params={"view": "i_like"})
    finally:
        _clear_overrides()
    assert response.status_code == 200
    assert response.json()["regenerating"] is False


def test_api_recommendations_gate_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_master_enabled", False)
    _override_auth()
    try:
        response = client.get("/api/v1/ai/recommendations", params={"view": "i_like"})
    finally:
        _clear_overrides()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_FEATURE_DISABLED"


def test_api_recommendations_rejects_unknown_view() -> None:
    # view 参数校验发生在路由进入前（FastAPI），与门禁状态无关 → 422。
    _override_auth()
    try:
        response = client.get(
            "/api/v1/ai/recommendations", params={"view": "everyone"}
        )
    finally:
        _clear_overrides()
    assert response.status_code == 422
