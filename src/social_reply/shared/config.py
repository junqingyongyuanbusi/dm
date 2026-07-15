from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dev:dev@localhost:5432/social_reply"
    redis_url: str = "redis://localhost:6379/0"
    chatwoot_webhook_secret: str = "change-me"
    chatwoot_signature_tolerance_seconds: int = 300
    tenant_id: str = "default"
    # Literal 收紧：配错 provider 在进程启动即报错，而非每条消息决策丢失（2c 终审 I1）
    llm_provider: Literal["stub", "openai"] = "stub"
    prompt_version: str = "v0-stub"
    chatwoot_base_url: str = "http://localhost:3000"
    chatwoot_api_token: str = "dev-local-token"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: float = 30.0
    # 知识检索（Plan 3）：默认全关，不影响现有决策行为
    knowledge_retrieval_enabled: bool = False
    knowledge_min_similarity: float = 0.5
    knowledge_top_k: int = 3
    # true 时检索无命中直接 HANDOFF/INSUFFICIENT_KNOWLEDGE，不调 LLM（省 token，§十三）
    require_knowledge: bool = False
    testing: bool = False

    @model_validator(mode="after")
    def _reject_default_secret_in_prod(self) -> "Settings":
        # 生产环境（非测试）拒绝空/默认 webhook 密钥（Plan 1 安全评审 backlog）
        if not self.testing and self.chatwoot_webhook_secret in ("", "change-me"):
            raise ValueError(
                "CHATWOOT_WEBHOOK_SECRET 未配置（不能为空或 change-me）；测试环境请设 TESTING=true"
            )
        # 生产环境拒绝空/默认 Chatwoot API token（Plan 2c：真实投递前置）
        if not self.testing and self.chatwoot_api_token in ("", "dev-local-token"):
            raise ValueError(
                "CHATWOOT_API_TOKEN 未配置（不能为空或 dev-local-token）；测试环境请设 TESTING=true"
            )
        # 生产环境启用 openai provider 时必须提供 API key
        if not self.testing and self.llm_provider == "openai" and self.openai_api_key == "":
            raise ValueError(
                "OPENAI_API_KEY 未配置（LLM_PROVIDER=openai 时不能为空）；测试环境请设 TESTING=true"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
