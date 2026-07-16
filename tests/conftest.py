import os

# 测试套件必须与开发者本地 .env 隔离（密闭性）：
# pydantic-settings 中真实环境变量优先于 .env 文件，这里用 setdefault 钉住
# 所有影响决策/验签行为的配置为"测试默认值"——.env 里的真实凭证/开关不再泄漏进测试，
# 同时保留 CI/开发者显式 export 覆盖的能力。DATABASE_URL/REDIS_URL 不钉，允许指向本地容器。
_TEST_DEFAULTS = {
    "TESTING": "true",
    "CHATWOOT_WEBHOOK_SECRET": "change-me",
    "CHATWOOT_BASE_URL": "http://localhost:3000",
    "CHATWOOT_API_TOKEN": "dev-local-token",
    "LLM_PROVIDER": "stub",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "KNOWLEDGE_RETRIEVAL_ENABLED": "false",
    "REQUIRE_KNOWLEDGE": "false",
    "KNOWLEDGE_VERBATIM_REPLY": "false",
}
for _k, _v in _TEST_DEFAULTS.items():
    os.environ.setdefault(_k, _v)
