from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    # Literal 收紧：配错 provider 在进程启动即报错，而非每条消息决策丢失
    llm_provider: Literal["stub", "openai"] = "stub"
    prompt_version: str = "v0-stub"
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: str = "dev-local-token"
    # 控制面：CONTROL_API_KEY 仅供服务间调用；浏览器管理员使用签名会话。
    control_api_key: SecretStr = SecretStr("")
    admin_session_secret: SecretStr = SecretStr("")
    admin_username: str = ""
    admin_password: SecretStr = SecretStr("")
    public_base_url: str = "http://localhost:8000"
    admin_allowed_tenants: str = "default"
    account_secrets_root: Path = Path(".secrets/accounts")
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
        # 生产环境（非测试）拒绝空/默认 webhook 密钥
        if not self.testing and self.chatwoot_webhook_secret in ("", "change-me"):
            raise ValueError(
                "CHATWOOT_WEBHOOK_SECRET 未配置（不能为空或 change-me）；测试环境请设 TESTING=true"
            )
        # 生产环境拒绝空/默认 Chatwoot API token
        if not self.testing and self.chatwoot_api_token in ("", "dev-local-token"):
            raise ValueError(
                "CHATWOOT_API_TOKEN 未配置（不能为空或 dev-local-token）；测试环境请设 TESTING=true"
            )
        if not self.testing and not self.control_api_key.get_secret_value():
            raise ValueError("CONTROL_API_KEY 未配置；账号管理 API 在生产环境必须鉴权")
        if not self.testing and len(self.admin_session_secret.get_secret_value()) < 32:
            raise ValueError("ADMIN_SESSION_SECRET 未配置或长度不足 32 字节")
        if not self.testing and (
            not self.admin_username or not self.admin_password.get_secret_value()
        ):
            raise ValueError("ADMIN_USERNAME/ADMIN_PASSWORD 未配置")
        if not self.testing and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL 在生产环境必须使用 https://")
        # 生产环境启用 openai provider 时必须提供 API key
        if not self.testing and self.llm_provider == "openai" and self.openai_api_key == "":
            raise ValueError(
                "OPENAI_API_KEY 未配置（LLM_PROVIDER=openai 时不能为空）；测试环境请设 TESTING=true"
            )
        return self


    @property
    def allowed_admin_tenants(self) -> frozenset[str]:
        return frozenset(
            tenant.strip() for tenant in self.admin_allowed_tenants.split(",") if tenant.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
