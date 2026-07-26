"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    browse_daily_limit: int = 20
    browse_high_match_bonus: int = 5
    apply_daily_free_limit: int = 3
    apply_daily_vip_limit: int = 10
    # 社区同城浏览偏好变更冷却（天）；对齐 PRD 居住城市一周更新一次精神
    community_city_cooldown_days: int = Field(default=7, ge=0)
    # 红娘服务只能通过现金订单获得，不为新用户自动发放免费次数。
    matchmaker_service_default_quota: int = 0
    superlike_daily_free_limit: int = 1
    superlike_daily_vip_limit: int = 3

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
            self.sms_provider == "mock" or self.wechat_provider == "mock"
        ):
            raise ValueError("生产环境禁止启用短信或微信 Mock 服务")
        if self.sms_provider.lower() == "mock" and (
            len(self.sms_mock_code) != 6 or not self.sms_mock_code.isdigit()
        ):
            raise ValueError("SMS_MOCK_CODE 必须是6位数字")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for dependency injection."""
    return Settings()


settings = get_settings()
