from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BACKEND_ROOT.parent


def test_ai_product_and_security_decisions_are_frozen() -> None:
    product = (WORKSPACE_ROOT / "PRODUCT.md").read_text(encoding="utf-8")
    decisions = (BACKEND_ROOT / "docs/ai/AI_PRODUCT_SECURITY_DECISIONS.md").read_text(
        encoding="utf-8"
    )
    product_required = (
        "AI 画像、搜索与资料合拍参考",
        "ai-policy-2026-08-07-v1",
    )
    decision_required = (
        "who_can_see_me=2 表示查看者 realname_status=2",
        "查看者认证状态缺失或无法判定时拒绝访问（fail-closed）",
        "一期只支持文字输入和 mock provider",
        "AI 输出经用户确认后才能发布",
        "match_score 保持 legacy-rule-v1",
        "新兼容度仅内部 shadow",
        "ai-policy-2026-08-07-v1",
        "age",
        "city_code",
        "marriage_status",
        "education_level",
        "height_cm",
        "income_band",
        "occupation_group",
        "interest_tags",
        "lifestyle_tags",
        "relationship_goal",
        "profile_text_extract",
        "search_parse",
        "compatibility_shadow",
        "ai_policy_approved",
        "ai_provider_approved",
        "ai_retention_policy_version",
        "HTTP 503 AI_FEATURE_DISABLED",
        "retryable=false",
        "compatibility-rule-v1",
        "普通日志不得写入手机号、身份证、精确位置、原始 IP、原始 prompt、原始 Provider 响应、隐藏资料或凭据",
    )
    for phrase in product_required:
        assert phrase in product
    for source in (product, decisions):
        for phrase in decision_required:
            assert phrase in source
