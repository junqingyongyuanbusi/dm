import os

import pytest
from sqlalchemy.engine import make_url

# 测试套件必须与开发者本地 .env 隔离（密闭性）：
# pydantic-settings 中真实环境变量优先于 .env 文件，这里用 setdefault 钉住
# 所有影响决策/验签行为的配置为"测试默认值"——.env 里的真实凭证/开关不再泄漏进测试，
# 同时保留 CI/开发者显式 export 覆盖的能力。DATABASE_URL/REDIS_URL 不钉，允许指向本地容器。
_TEST_DEFAULTS = {
    "TESTING": "true",
    # 集成测试会 drop/create 全部业务表，必须固定使用独立测试库，禁止读取 .env 开发库。
    "DATABASE_URL": "postgresql+asyncpg://dev:dev@localhost:5432/social_reply_test",
    "CONTROL_API_KEY": "test-control-key",
    "ADMIN_SESSION_SECRET": "test-admin-session-secret-at-least-32-bytes",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-admin-password",
    "PUBLIC_BASE_URL": "https://reply.example.com",
    "ADMIN_ALLOWED_TENANTS": "default,tenant-a,tenant-b",
    "X_API_KEY": "ck-app",
    "X_API_SECRET": "cs-app",
    "X_LEGACY_DM_ENABLED": "true",
    "X_ACTIVITY_ENABLED": "true",
    "XCHAT_ENABLED": "true",
    "FACEBOOK_MESSENGER_ENABLED": "true",
    "INSTAGRAM_MESSAGING_ENABLED": "true",
    "WHATSAPP_ENABLED": "true",
    "EMAIL_ENABLED": "false",
    "EMAIL_AUTO_REPLY_ENABLED": "false",
    "EMAIL_POLL_INTERVAL_SECONDS": "60",
    "EMAIL_MAX_MESSAGES_PER_POLL": "100",
    "EMAIL_PER_SENDER_DAILY_REPLY_LIMIT": "5",
    "EMAIL_NETWORK_TIMEOUT_SECONDS": "10",
    "EMAIL_ALLOWED_HOSTS": "imap.larksuite.com,smtp.larksuite.com",
    "FACEBOOK_APP_ID": "fb-app",
    "FACEBOOK_APP_SECRET": "fb-app-secret",
    "META_VERIFY_TOKEN": "meta-verify-token",
    "INSTAGRAM_APP_ID": "ig-app",
    "INSTAGRAM_APP_SECRET": "ig-app-secret",
    "INSTAGRAM_VERIFY_TOKEN": "instagram-verify-token",
    "LLM_PROVIDER": "stub",
    "PLATFORM_SECRET_KEYS": "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "KNOWLEDGE_RETRIEVAL_ENABLED": "false",
    "KNOWLEDGE_MATCH_ONLY_REPLY_ENABLED": "false",
    "REQUIRE_KNOWLEDGE": "false",
    "KNOWLEDGE_VERBATIM_REPLY": "false",
}
for _k, _v in _TEST_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

_database_name = make_url(os.environ["DATABASE_URL"]).database or ""
if not _database_name.endswith("_test"):
    raise RuntimeError(f"pytest refuses to use non-test database: {_database_name or '<missing>'}")


@pytest.fixture(autouse=True)
def _reset_dispatch_cursor(monkeypatch):
    """补扫的 `_dispatch_cursor` 是进程内遍历提示，不是业务事实。

    跨测试残留会让批次从上一个测试的 UUID 之后开始（sweep.py:284-291），
    随机漏掉本测试自己插入的行，造成顺序相关的 flake。每个测试从干净进度开始。
    """
    from social_reply.application.message_delivery import sweep

    monkeypatch.setattr(sweep, "_dispatch_cursor", None)
