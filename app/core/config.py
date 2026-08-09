"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from ``.env`` and process environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Xuanshi AI API"
    app_version: str = "0.1.0"
    environment: Literal["development", "testing", "staging", "production"] = "development"
    debug: bool = True
    docs_enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    api_prefix: str = "/api/v1"
    auto_init_db: bool = True

    database_url: str = "mysql+aiomysql://root:YOUR_MYSQL_PASSWORD@127.0.0.1:3306/xuanshiai"
    redis_url: str = "redis://127.0.0.1:6379/0"

    secret_key: str = "change-me-in-local-env"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    max_sessions_per_user: int = 5
    sms_code_expire_seconds: int = 300
    sms_send_interval_seconds: int = 60
    sms_daily_limit: int = 10
    sms_mock_code: str = "123456"
    wechat_app_id: str | None = None
    wechat_app_secret: str | None = None
    wechat_provider: str = "wechat"
    wechat_payment_mode: Literal["mock", "real"] = "mock"
    wechat_mock_openid_prefix: str = "mock-openid-"
    sms_provider: str = "disabled"
    agreement_versions_raw: str = (
        "user_service:v1,privacy_policy:v1,safety_pledge:v1,community_rules:v1"
    )

    cors_origins_raw: str = "http://localhost:3000,http://localhost:5173"
    upload_dir: str = "storage/uploads"
    public_base_url: str = "http://127.0.0.1:8000"
    wechat_mini_program_page: str = "pages/profile/profile"
    recommendation_page_size: int = 20
    browse_daily_limit: int = 8
    browse_high_match_bonus: int = 5
    apply_daily_free_limit: int = 3
    apply_daily_vip_limit: int = 10
    # 社区同城浏览偏好变更冷却（天）；对齐 PRD 居住城市一周更新一次精神
    community_city_cooldown_days: int = Field(default=7, ge=0)
    # 红娘服务只能通过现金订单获得，不为新用户自动发放免费次数。
    matchmaker_service_default_quota: int = 0
    superlike_daily_free_limit: int = 1
    superlike_daily_vip_limit: int = 3
    paper_plane_daily_limit: int = 3

    # Optional second-layer text moderation. The provider is disabled until
    # the purchased marketplace API path and AppCode are configured.
    aliyun_content_moderation_enabled: bool = False
    aliyun_content_moderation_base_url: str = (
        "https://lxmingan.market.alicloudapi.com"
    )
    aliyun_content_moderation_path: str = "/YOUR_API_PATH"
    aliyun_content_moderation_app_code: SecretStr | None = None
    aliyun_content_moderation_request_mode: Literal["json", "form"] = "json"
    aliyun_content_moderation_text_field: str = "text"
    aliyun_content_moderation_timeout_seconds: float = Field(
        default=2.5, gt=0, le=10
    )
    aliyun_content_moderation_fail_mode: Literal["review", "reject"] = "review"
    aliyun_content_moderation_default_action: Literal[
        "manual_review", "reject", "replace"
    ] = "manual_review"

    # Optional environment overrides for commercial configuration. When unset,
    # the corresponding database configuration remains the fallback.
    membership_monthly_price: float | None = Field(default=None, ge=0)
    membership_quarterly_price: float | None = Field(default=None, ge=0)
    membership_yearly_price: float | None = Field(default=None, ge=0)
    membership_monthly_original_price: float | None = Field(default=None, ge=0)
    membership_quarterly_original_price: float | None = Field(default=None, ge=0)
    membership_yearly_original_price: float | None = Field(default=None, ge=0)
    membership_monthly_daily_price: float | None = Field(default=None, ge=0)
    membership_quarterly_daily_price: float | None = Field(default=None, ge=0)
    membership_yearly_daily_price: float | None = Field(default=None, ge=0)

    # Rewards are also configurable so all point values have one source.
    point_checkin_reward: int = Field(default=5, gt=0)
    point_profile_complete_reward: int = Field(default=50, gt=0)
    point_realname_verified_reward: int = Field(default=100, gt=0)

    # Per-use costs for point products. Unset values fall back to the product
    # row, allowing existing database-configured products to keep working.
    point_cost_extra_apply: int | None = Field(default=None, gt=0)
    point_cost_extra_superlike: int | None = Field(default=None, gt=0)
    point_cost_browse_unlock: int | None = Field(default=None, gt=0)
    point_cost_exposure_card: int | None = Field(default=None, gt=0)
    point_cost_paper_plane_unlock: int | None = Field(default=None, gt=0)
    point_cost_profile_detail_unlock: int | None = Field(default=None, gt=0)
    point_cost_membership_exchange: int | None = Field(default=None, gt=0)
    point_cost_service_coupon: int | None = Field(default=None, gt=0)

    # ==================== AI 功能开关与门禁 ====================
    # 一期全部默认关闭。生产环境只有在 ai_policy_approved、
    # ai_provider_approved、ai_retention_policy_version 同时满足且
    # Provider 不是 mock 时才允许打开（见 validate_ai_feature_gates）。
    ai_master_enabled: bool = False
    ai_profile_enabled: bool = False
    ai_search_enabled: bool = False
    ai_compatibility_shadow_enabled: bool = False
    # 一期唯一 provider；生产环境启用 AI 时禁止使用 mock。
    ai_provider: Literal["mock"] = "mock"
    # 生产启用门禁（Task 1 冻结）：缺任一批准项则校验失败。
    ai_policy_approved: bool = False
    ai_provider_approved: bool = False
    ai_retention_policy_version: str | None = None

    # AI 任务/租约/重试/限流配置。
    ai_lease_seconds: int = Field(default=300, gt=0, le=3600)
    ai_max_attempts: int = Field(default=3, gt=0, le=10)
    ai_search_parse_rate_per_minute: int = Field(default=5, gt=0, le=60)
    ai_profile_session_expire_days: int = Field(default=7, gt=0)
    ai_search_draft_expire_hours: int = Field(default=24, gt=0)
    ai_compatibility_snapshot_ttl_minutes: int = Field(default=10, gt=0)
    ai_gateway_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # Task 12 审计/指标开关（非敏感，不影响 production fail-closed）。
    ai_audit_enabled: bool = True
    # outbox/purge 积压指标触发本地告警的阈值。
    ai_metrics_backlog_warn_threshold: int = Field(default=1000, ge=0)

    log_level: str = "INFO"

    @property
    def cors_origins(self) -> list[str]:
        """Convert the comma-separated environment value into CORS origins."""
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]

    @property
    def agreement_versions(self) -> dict[str, str]:
        """Return the currently published agreement version for each type."""
        versions: dict[str, str] = {}
        for item in self.agreement_versions_raw.split(","):
            if ":" in item:
                agreement_type, version = item.split(":", 1)
                versions[agreement_type.strip()] = version.strip()
        return versions

    @property
    def is_test_mode(self) -> bool:
        """Return whether development-only providers are allowed."""
        return self.environment in {"development", "testing"}

    def membership_price_override(self, code: str, field: str, fallback: float | None) -> float | None:
        """Return an environment override for a membership package field."""
        value = getattr(self, f"membership_{code}_{field}", None)
        return value if value is not None else fallback

    def point_cost_override(self, code: str, fallback: int) -> int:
        """Return the configured per-use cost for a point product."""
        value = getattr(self, f"point_cost_{code}", None)
        return value if value is not None else fallback

    @model_validator(mode="after")
    def validate_test_providers(self) -> "Settings":
        """Prevent Mock providers from being enabled in production."""
        if self.environment in {"staging", "production"} and self.auto_init_db:
            raise ValueError("staging/production 环境必须关闭 AUTO_INIT_DB")
        if not self.is_test_mode and (
            self.sms_provider == "mock" or self.wechat_provider == "mock" or self.wechat_payment_mode == "mock"
        ):
            raise ValueError("生产环境禁止启用短信、微信登录或微信支付 Mock 服务")
        if self.sms_provider.lower() == "mock" and (
            len(self.sms_mock_code) != 6 or not self.sms_mock_code.isdigit()
        ):
            raise ValueError("SMS_MOCK_CODE 必须是6位数字")
        if (
            self.environment == "production"
            and self.aliyun_content_moderation_enabled
            and not self.aliyun_content_moderation_app_code
        ):
            raise ValueError(
                "生产环境启用阿里云敏感词服务时必须配置 AppCode"
            )
        self._validate_ai_feature_gates()
        return self

    def ai_approvals_complete(self) -> bool:
        """Return whether all three production approval gates are satisfied."""
        return bool(
            self.ai_policy_approved
            and self.ai_provider_approved
            and self.ai_retention_policy_version
        )

    def _validate_ai_feature_gates(self) -> None:
        """Fail closed when any AI switch is enabled without the full gates.

        Production only: a real (non-mock) provider must be approved before any
        AI feature may run.  Missing approval flags or a mock production
        provider raise so ``Settings(...)`` construction fails with a
        ``ValidationError``.
        """
        if self.environment != "production":
            return
        any_ai_enabled = any(
            (
                self.ai_master_enabled,
                self.ai_profile_enabled,
                self.ai_search_enabled,
                self.ai_compatibility_shadow_enabled,
            )
        )
        if not any_ai_enabled:
            return
        if not self.ai_approvals_complete():
            raise ValueError(
                "生产环境启用 AI 功能必须同时满足 ai_policy_approved、"
                "ai_provider_approved 和 ai_retention_policy_version"
            )
        if self.ai_provider == "mock":
            raise ValueError("生产环境禁止使用 mock AI Provider")


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for dependency injection."""
    return Settings()


settings = get_settings()
