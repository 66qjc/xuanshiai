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
    ai_recommend_enabled: bool = False
    # 画像发布门槛：至少确认多少个字段才允许 publish（提前建构阈值）。
    # 良配对齐：默认 7/10 ≈ 67%，"无需完成全部题目，进度 67% 左右可提前
    # 建构画像"。进度提示与发布硬门槛共用此值，避免两套数字漂移。
    ai_profile_min_fields: int = Field(default=7, ge=1, le=20)
    # 墨相师对话建构门槛（设计 D2/D6）：硬字段全齐 + 折算总分百分比阈值。
    ai_master_build_gate: float = 0.60
    # 墨相师硬字段白名单（逗号分隔，空串=内置默认 城市/年龄/婚姻状态）。
    ai_master_hard_fields: str = ""
    # 匹配度外显灰度（方案 WP-C2 / 决策 D6）：off=影子运行不外显（现状）；
    # bucket=按 viewer 稳定哈希放量 ai_compatibility_display_bucket_pct%；
    # on=全量外显。仅改变 ai_compatibility_snapshot.display_eligible 的写入值，
    # 读取端门禁（_apply_display_gate）与 shadow 纪律测试在 off 下保持不变。
    ai_compatibility_display_mode: Literal["off", "bucket", "on"] = "off"
    ai_compatibility_display_bucket_pct: int = Field(default=0, ge=0, le=100)
    # 一期默认 mock；deepseek 为首个真 provider（开发/测试可用，生产启用需
    # 先满足 ai_policy_approved / ai_provider_approved / retention 三道门禁）。
    # dots 为小红书 hi lab dots.llm 的 OpenAI 兼容 API provider。
    ai_provider: Literal["mock", "deepseek", "dots"] = "mock"
    # 生产启用门禁（Task 1 冻结）：缺任一批准项则校验失败。
    ai_policy_approved: bool = False
    ai_provider_approved: bool = False
    ai_retention_policy_version: str | None = None

    # DeepSeek provider 配置（OpenAI 兼容 API）。真实 api_key 仅存于被忽略的
    # .env；.env.example 只放占位符 YOUR_DEEPSEEK_API_KEY。生产启用需先走
    # Provider 审批门禁（ai_policy_approved 等）。deepseek-chat 已弃用，
    # 由 deepseek-v4-flash 接替。
    ai_deepseek_api_key: SecretStr | None = None
    ai_deepseek_base_url: str = "https://api.deepseek.com"
    ai_deepseek_model: str = "deepseek-v4-flash"
    ai_deepseek_max_tokens: int = Field(default=2048, gt=0, le=8192)

    # Dots provider 配置（小红书 hi lab dots.llm，OpenAI 兼容 API）。真实
    # api_key 仅存于被忽略的 .env；.env.example 只放占位符。dots3-note-prev
    # 是推理模型（返回 reasoning_content + content），max_tokens 需覆盖推理
    # 消耗，默认给到 4096。
    ai_dots_api_key: SecretStr | None = None
    ai_dots_base_url: str = "https://note3-prev-api.askdiandian.com/v1"
    ai_dots_model: str = "dots3-note-prev"
    ai_dots_max_tokens: int = Field(default=4096, gt=0, le=8192)

    # Narrative 专用模型覆盖（可选）。画像叙事层 generate_narrative 是重推理
    # 任务，默认走主 provider 的模型。若主 provider 是推理模型（如 dots3-note-prev），
    # 生成耗时可达数十秒；通过此项指定一个更快的非推理模型 + 配套 provider 来
    # 单独驱动 narrative，其余方法（抽取/搜索/回复）不受影响。
    # 为空时回退到主 provider 默认模型。需要配套的 provider key 可用。
    ai_narrative_provider: Literal["", "deepseek", "dots"] = ""
    ai_narrative_model: str = ""
    ai_narrative_max_tokens: int = Field(default=0, ge=0, le=8192)

    # ==================== 语音（STT/TTS）功能开关与配置 ====================
    # P-04 / Phase 4。默认关闭。生产启用需满足三道审批门禁 + AccessKey 配置
    # （见 _validate_ai_feature_gates 的 fail-closed 检查）。
    ai_voice_enabled: bool = False
    ai_voice_provider: Literal["aliyun"] = "aliyun"
    # 阿里云智能语音交互（NLS）配置。api_key/app_key 仅存于被忽略的 .env，
    # 不进 .env.example；生产启用需先走语音 Provider 审批 + DPA / 数据出境审查。
    ai_aliyun_voice_api_key: SecretStr | None = None
    ai_aliyun_voice_app_key: SecretStr | None = None
    ai_aliyun_voice_region: str = "cn-shanghai"
    ai_aliyun_voice_asr_model: str = "paraformer-realtime-v2"
    ai_aliyun_voice_tts_model: str = "cosyvoice-v1"
    # 语音合成单次文本上限（与 voice/base.MAX_TTS_TEXT_LENGTH 对齐）。
    ai_tts_max_text_length: int = Field(default=500, gt=0, le=2000)
    # 语音转写单次音频时长上限（秒，与前端录音 60s 上限对齐）。
    ai_asr_max_duration_seconds: int = Field(default=60, gt=0, le=300)
    # 临时音频文件过期清理（小时），合规要求转写后短期保留即删除。
    ai_voice_audio_retention_hours: int = Field(default=24, gt=0)

    # ==================== 实时半双工语音对话（P-04b）====================
    # 实时对话模式开关：默认关闭。生产环境 fail closed（见
    # ``_validate_ai_feature_gates``）：需满足三道审批门禁 + AccessKey 配置。
    # 实时 ASR 鉴权需要 AccessKey ID/Secret（不只是 api_key/app_key），
    # 用以换取 NLS Token；该凭据仅存于被忽略的 .env，不进 .env.example。
    ai_voice_conversation_enabled: bool = False
    ai_aliyun_voice_access_key_id: SecretStr | None = None
    ai_aliyun_voice_access_key_secret: SecretStr | None = None
    # 单次实时对话轮次最长音频时长（秒），与前端实时录音上限对齐。
    ai_voice_conversation_max_turn_seconds: int = Field(default=60, gt=0, le=300)
    # 实时 ASR WebSocket 接入点（阿里云 NLS 实时语音识别）。
    # 注意必须是连字符域名 nls-gateway-cn-shanghai（官方文档标准）：
    # 点分域名 nls-gateway.cn-shanghai 能握手但引擎不产出任何识别结果。
    ai_aliyun_voice_asr_ws_url: str = (
        "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
    )

    # AI 任务/租约/重试/限流配置。
    ai_lease_seconds: int = Field(default=300, gt=0, le=3600)
    ai_max_attempts: int = Field(default=3, gt=0, le=10)
    ai_search_parse_rate_per_minute: int = Field(default=5, gt=0, le=60)
    # WP-S3：猜你喜欢 AI 生成频控（每用户 24h 窗口内的任务数上限）。
    ai_search_suggest_daily_limit: int = Field(default=5, gt=0, le=50)
    ai_profile_session_expire_days: int = Field(default=7, gt=0)
    ai_search_draft_expire_hours: int = Field(default=24, gt=0)
    ai_compatibility_snapshot_ttl_minutes: int = Field(default=10, gt=0)
    # WP-C1：llm 精算快照 TTL（默认 7 天）——命中期内二次查看不再触发精算。
    ai_compatibility_llm_ttl_minutes: int = Field(default=10080, gt=0)
    ai_gateway_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # WP-P6 三类推荐（D4 快照预计算）：物化有效期 / 候选池上限 / 每视图 top-N。
    ai_recommendation_ttl_minutes: int = Field(default=1440, gt=0)
    ai_recommendation_pool_limit: int = Field(default=200, gt=0)
    ai_recommendation_top_n: int = Field(default=20, gt=0)

    # ==================== 墨相师四阶段融合 Journey 开关（Contract v1.1 §10）====================
    # 墨相师新旅程（候选理解池 + 自动整理邀请）默认关闭。生产启用需经过
    # _validate_ai_feature_gates 的三道审批门禁。关闭时不产生新候选、不推
    # 新邀请，新前端显示暂不可用并保留旧备用入口；旧 profile_build 不受影响。
    ai_moxiang_journey_enabled: bool = False

    # Task 12 审计/指标开关（非敏感，不影响 production fail-closed）。
    ai_audit_enabled: bool = True
    # outbox/purge 积压指标触发本地告警的阈值。
    ai_metrics_backlog_warn_threshold: int = Field(default=1000, ge=0)

    log_level: str = "INFO"

    # AI 军师（助手/润色/搜索/匹配，合著仓方案）。与画像/搜索/语音的
    # ai_provider/ai_master_* 那套互不影响：本套用 ai_enabled + ai_base_url 直连。
    ai_enabled: bool = False
    ai_base_url: str = "https://api.deepseek.com/v1"
    ai_api_key: SecretStr | None = None
    ai_model: str = "deepseek-chat"
    ai_timeout_seconds: float = Field(default=30, gt=0, le=120)
    ai_max_context_messages: int = Field(default=80, ge=10, le=200)
    ai_daily_assistant_limit: int = Field(default=20, ge=1, le=1000)
    ai_daily_polish_limit: int = Field(default=5, ge=1, le=100)
    ai_daily_search_limit: int = Field(default=10, ge=1, le=100)
    ai_daily_match_limit: int = Field(default=10, ge=1, le=100)
    ai_daily_advisor_limit: int = Field(default=20, ge=1, le=1000)
    ai_advisor_max_context_messages: int = Field(default=80, ge=10, le=200)
    ai_advisor_prompt_version: str = "relationship-v1"
    ai_advisor_knowledge_version: str = "seed-v1"

    @property
    def cors_origins(self) -> list[str]:
        """Convert the comma-separated environment value into CORS origins."""
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]

    @property
    def ai_provider_name(self) -> str:
        """当前生效的 provider 名称，用于 AITaskContext 审计元数据。"""
        return self.ai_provider

    @property
    def ai_model_name(self) -> str:
        """当前 provider 对应的模型名，用于 AITaskContext 审计元数据。

        mock 时返回固定占位以保持历史审计一致性；真 provider 返回配置值。
        """
        if self.ai_provider == "deepseek":
            return self.ai_deepseek_model
        if self.ai_provider == "dots":
            return self.ai_dots_model
        return "mock-model-v1"

    @property
    def ai_voice_model_name(self) -> str:
        """语音 provider 对应的模型名，用于语音任务审计元数据。

        STT 与 TTS 模型不同，这里返回 ASR 模型作为代表；TTS 审计由
        VoiceGateway 场景区分。
        """
        return self.ai_aliyun_voice_asr_model

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
                self.ai_recommend_enabled,
                self.ai_voice_enabled,
                self.ai_moxiang_journey_enabled,
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
        if self.ai_voice_conversation_enabled:
            # 实时对话模式同样需要三道审批门禁。
            if not self.ai_approvals_complete():
                raise ValueError(
                    "生产环境启用实时语音对话必须同时满足 ai_policy_approved、"
                    "ai_provider_approved 和 ai_retention_policy_version"
                )
            # 实时 ASR 需要 AccessKey 鉴权（换取 NLS Token），与 REST 模式的
            # api_key/app_key 不同：缺 AccessKey 直接 fail closed。
            if (
                not self.ai_aliyun_voice_access_key_id
                or not self.ai_aliyun_voice_access_key_secret
            ):
                raise ValueError(
                    "生产环境启用实时语音对话必须配置 AccessKey ID/Secret"
                )


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for dependency injection."""
    return Settings()


settings = get_settings()
