from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    llm_provider: str = "stub"  # stub / anthropic / openai（Plan 2b/后续接真）
    prompt_version: str = "v0-stub"
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: str = "dev-local-token"
    testing: bool = False

    @model_validator(mode="after")
    def _reject_default_secret_in_prod(self) -> "Settings":
        # 生产环境（非测试）拒绝空/默认 webhook 密钥（Plan 1 安全评审 backlog）
        if not self.testing and self.chatwoot_webhook_secret in ("", "change-me"):
            raise ValueError(
                "CHATWOOT_WEBHOOK_SECRET 未配置（不能为空或 change-me）；测试环境请设 TESTING=true"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
