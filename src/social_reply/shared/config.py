from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_META_PLATFORMS = {"facebook", "instagram"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_enabled: bool = False
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    # Literal 收紧：配错 provider 在进程启动即报错，而非每条消息决策丢失
    llm_provider: Literal["stub", "openai"] = "stub"
    prompt_version: str = "v1-wikifx-multilingual"
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: str = "dev-local-token"
    # 控制面：CONTROL_API_KEY 仅供服务间调用；浏览器管理员使用签名会话。
    control_api_key: SecretStr = SecretStr("")
    admin_session_secret: SecretStr = SecretStr("")
    admin_username: str = ""
    admin_password: SecretStr = SecretStr("")
    public_base_url: str = "http://localhost:8000"
    admin_allowed_tenants: str = "default"
    # X App-level OAuth 1.0a credentials. Like Postiz, these belong to the
    # deployment, while each authorized account stores only its user token pair.
    x_api_key: SecretStr = SecretStr("")
    x_api_secret: SecretStr = SecretStr("")
    x_legacy_dm_enabled: bool = True
    x_activity_enabled: bool = True
    xchat_enabled: bool = True
    scheduler_tick_seconds: float = Field(default=0.5, ge=0.05, le=10)
    scheduler_core_interval_seconds: float = Field(default=3, ge=0.5, le=60)
    scheduler_core_warn_after_seconds: float = Field(default=30, ge=1, le=3600)
    scheduler_inspection_warn_after_seconds: float = Field(default=300, ge=1, le=7200)
    chatwoot_reconcile_interval_seconds: int = Field(default=3, ge=1, le=3600)
    x_dm_poll_interval_seconds: int = Field(default=90, ge=0, le=86400)
    x_webhook_check_interval_seconds: int = Field(default=600, ge=0, le=86400)
    xchat_poll_interval_seconds: int = Field(default=900, ge=0, le=86400)
    xchat_max_conversations_per_poll: int = Field(default=10, ge=1, le=1000)
    xchat_subscription_check_interval_seconds: int = Field(default=600, ge=0, le=86400)
    xchat_recovery_sweep_interval_seconds: int = Field(default=30, ge=0, le=3600)
    xchat_ready_probe_interval_seconds: int = Field(default=21600, ge=0, le=604800)
    xchat_pending_probe_interval_seconds: int = Field(default=600, ge=0, le=86400)
    # 公开回复 @mention。X 开发者条款对“AI 生成并发布的回复”要求事先报批，且每次互动
    # 最多回 1 条；默认关，开启前确保已获 X 批准并已标注为自动账号。
    x_public_reply_enabled: bool = False
    x_oauth_legacy_state_write: bool = False
    facebook_messenger_enabled: bool = True
    instagram_messaging_enabled: bool = True
    whatsapp_enabled: bool = True
    feishu_enabled: bool = False
    feishu_handoff_notifications_enabled: bool = False
    feishu_handoff_sweep_interval_seconds: float = Field(default=3, ge=0.5, le=60)
    feishu_handoff_sender_lease_seconds: int = Field(default=30, ge=5, le=600)
    feishu_handoff_max_attempts: int = Field(default=8, ge=1, le=100)
    # Meta 发布范围默认是「人工审核后才外发」。只有部署方完成 App Review 并显式接受
    # 自动回复的合规责任后，才能把 Meta 账号提升为 BOT_ACTIVE。
    meta_auto_reply_enabled: bool = False
    # Facebook Page / Instagram 专业账号公开评论回复。需要对应评论权限与 webhook 字段。
    meta_comment_reply_enabled: bool = False
    facebook_app_id: str = ""
    facebook_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    instagram_app_id: str = ""
    instagram_app_secret: SecretStr = SecretStr("")
    instagram_verify_token: SecretStr = SecretStr("")
    meta_health_check_interval_seconds: int = Field(default=600, ge=60, le=86400)
    feishu_health_check_interval_seconds: int = Field(default=600, ge=60, le=86400)
    account_secrets_root: Path = Path(".secrets/accounts")
    platform_secret_keys: SecretStr = SecretStr("")
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: float = 30.0
    # 知识检索：默认全关，不影响现有决策行为
    knowledge_retrieval_enabled: bool = False
    knowledge_min_similarity: float = 0.5
    knowledge_top_k: int = 3
    # true 时命中模板原文直出（不经 LLM 改写/翻译）；false 则模板作参考交 LLM 生成
    knowledge_verbatim_reply: bool = False
    # true 时检索无命中直接 HANDOFF/INSUFFICIENT_KNOWLEDGE，不调 LLM（省 token，§十三）
    require_knowledge: bool = False
    # 决策时注入同会话最近历史消息（不含当前这条）；0 关闭多轮上下文。
    conversation_history_limit: int = Field(default=20, ge=0, le=50)
    # 历史总字符预算，避免长会话放大请求成本或超过模型上下文。
    conversation_history_max_chars: int = Field(default=12000, ge=0, le=50000)
    testing: bool = False

    @model_validator(mode="after")
    def _normalize_database_url(self) -> "Settings":
        # 托管平台（Railway 等）注入裸 postgres:// / postgresql:// URL，
        # 而本项目全程用 asyncpg，需归一到 postgresql+asyncpg://（DRY：迁移与运行共用）。
        url = self.database_url
        if url.startswith("postgres://"):
            self.database_url = "postgresql+asyncpg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            self.database_url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return self

    @model_validator(mode="after")
    def _reject_default_secret_in_prod(self) -> "Settings":
        if self.chatwoot_enabled and not self.testing:
            if self.chatwoot_webhook_secret in ("", "change-me"):
                raise ValueError(
                    "CHATWOOT_WEBHOOK_SECRET 未配置（CHATWOOT_ENABLED=true 时不能为空或 change-me）"
                )
            if self.chatwoot_api_token in ("", "dev-local-token"):
                raise ValueError(
                    "CHATWOOT_API_TOKEN 未配置"
                    "（CHATWOOT_ENABLED=true 时不能为空或 dev-local-token）"
                )
        if not self.testing and not self.control_api_key.get_secret_value():
            raise ValueError("CONTROL_API_KEY 未配置；账号管理 API 在生产环境必须鉴权")
        if not self.testing and len(self.admin_session_secret.get_secret_value()) < 32:
            raise ValueError("ADMIN_SESSION_SECRET 未配置或长度不足 32 字节")
        if not self.testing and (
            not self.admin_username or not self.admin_password.get_secret_value()
        ):
            raise ValueError("ADMIN_USERNAME/ADMIN_PASSWORD 未配置")
        if not self.testing and not self.allowed_admin_tenants:
            raise ValueError("ADMIN_ALLOWED_TENANTS 至少需要配置一个 Tenant")
        if not self.testing and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL 在生产环境必须使用 https://")
        x_key = self.x_api_key.get_secret_value()
        x_secret = self.x_api_secret.get_secret_value()
        if bool(x_key) != bool(x_secret):
            raise ValueError("X_API_KEY 与 X_API_SECRET 必须同时配置或同时留空")
        facebook_secret = self.facebook_app_secret.get_secret_value()
        facebook_values = (self.facebook_app_id, facebook_secret)
        if any(facebook_values) and not all(facebook_values):
            raise ValueError("FACEBOOK_APP_ID 与 FACEBOOK_APP_SECRET 必须同时配置")
        instagram_secret = self.instagram_app_secret.get_secret_value()
        instagram_values = (self.instagram_app_id, instagram_secret)
        if any(instagram_values) and not all(instagram_values):
            raise ValueError("INSTAGRAM_APP_ID 与 INSTAGRAM_APP_SECRET 必须同时配置")
        if not self.platform_secret_key_list:
            raise ValueError("PLATFORM_SECRET_KEYS 未配置；平台凭证必须使用应用层加密")
        from social_reply.infrastructure.secret_crypto import SecretCipher

        SecretCipher(self.platform_secret_key_list)
        if not self.testing and self.llm_provider == "stub":
            raise ValueError("LLM_PROVIDER=stub 仅允许测试环境，生产环境禁止公开测试回复")
        # 生产环境启用 openai provider 时必须提供 API key
        if not self.testing and self.llm_provider == "openai" and self.openai_api_key == "":
            raise ValueError(
                "OPENAI_API_KEY 未配置（LLM_PROVIDER=openai 时不能为空）；测试环境请设 TESTING=true"
            )
        return self

    @property
    def platform_secret_key_list(self) -> tuple[str, ...]:
        return tuple(
            key.strip()
            for key in self.platform_secret_keys.get_secret_value().split(",")
            if key.strip()
        )

    @property
    def x_app_credentials(self) -> tuple[str, str] | None:
        key = self.x_api_key.get_secret_value().strip()
        secret = self.x_api_secret.get_secret_value().strip()
        return (key, secret) if key and secret else None

    @property
    def x_integration_enabled(self) -> bool:
        return (
            self.x_legacy_dm_enabled
            or self.x_activity_enabled
            or self.xchat_enabled
            or self.x_public_reply_enabled
        )

    @property
    def x_mention_ingest_enabled(self) -> bool:
        """Mentions ride the Activity webhook, so both switches must be on."""
        return self.x_activity_enabled and self.x_public_reply_enabled

    def platform_integration_enabled(self, platform: str) -> bool:
        if platform == "facebook":
            return self.facebook_messenger_enabled
        if platform == "instagram":
            return self.instagram_messaging_enabled
        if platform == "whatsapp":
            return self.whatsapp_enabled
        if platform == "x":
            return self.x_integration_enabled
        if platform == "feishu":
            return self.feishu_enabled
        return True

    def platform_disabled_code(self, platform: str) -> str | None:
        if self.platform_integration_enabled(platform):
            return None
        return {
            "facebook": "FACEBOOK_MESSENGER_DISABLED",
            "instagram": "INSTAGRAM_MESSAGING_DISABLED",
            "whatsapp": "WHATSAPP_DISABLED",
            "x": "X_INTEGRATION_DISABLED",
            "feishu": "FEISHU_DISABLED",
        }.get(platform, "PLATFORM_DISABLED")

    def meta_automation_default_allowed(self, platform: str, automation_default: str) -> bool:
        """Whether this deployment lets a Meta account default to something other than draft.

        Draft-only stays allowed unconditionally; the switch only unlocks BOT_ACTIVE, and it
        must be set on API and Worker alike because provisioning runs in the Worker.
        """
        if platform not in _META_PLATFORMS:
            return True
        return automation_default == "BOT_DRAFT_ONLY" or self.meta_auto_reply_enabled

    @property
    def facebook_app_credentials(self) -> tuple[str, str] | None:
        app_id = self.facebook_app_id.strip()
        secret = self.facebook_app_secret.get_secret_value().strip()
        return (app_id, secret) if app_id and secret else None

    @property
    def instagram_app_credentials(self) -> tuple[str, str] | None:
        app_id = self.instagram_app_id.strip()
        secret = self.instagram_app_secret.get_secret_value().strip()
        return (app_id, secret) if app_id and secret else None

    @property
    def allowed_admin_tenants(self) -> frozenset[str]:
        return frozenset(
            tenant.strip() for tenant in self.admin_allowed_tenants.split(",") if tenant.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
