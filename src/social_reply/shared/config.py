from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    default_automation_state: str = "BOT_DRAFT_ONLY"
    testing: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
