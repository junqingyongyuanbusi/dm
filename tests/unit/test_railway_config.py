import pytest
from scripts.validate_railway_config import validate

_REQUIRED = {
    "DATABASE_URL": "postgresql://db",
    "REDIS_URL": "redis://redis",
    "PLATFORM_SECRET_KEYS": "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=",
    "CONTROL_API_KEY": "control-key",
    "ADMIN_SESSION_SECRET": "session-secret-at-least-32-characters",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "password",
    "ADMIN_ALLOWED_TENANTS": "default",
    "PUBLIC_BASE_URL": "https://relay.example.com",
    "LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "openai-key",
    "TESTING": "false",
    "CHATWOOT_ENABLED": "false",
    "X_LEGACY_DM_ENABLED": "true",
    "X_ACTIVITY_ENABLED": "true",
    "XCHAT_ENABLED": "false",
    "X_PUBLIC_REPLY_ENABLED": "false",
    "FACEBOOK_MESSENGER_ENABLED": "false",
    "INSTAGRAM_MESSAGING_ENABLED": "false",
    "WHATSAPP_ENABLED": "false",
    "FEISHU_ENABLED": "false",
    "EMAIL_ENABLED": "false",
    "EMAIL_AUTO_REPLY_ENABLED": "false",
    "FEISHU_HANDOFF_NOTIFICATIONS_ENABLED": "false",
    "META_AUTO_REPLY_ENABLED": "false",
    "META_COMMENT_REPLY_ENABLED": "false",
    "KNOWLEDGE_RETRIEVAL_ENABLED": "false",
    "KNOWLEDGE_VERBATIM_REPLY": "false",
    "REQUIRE_KNOWLEDGE": "false",
}


def _variables(**overrides: dict[str, str]) -> dict[str, dict[str, str]]:
    return {
        service: {**_REQUIRED, "SERVICE_ROLE": service, **overrides.get(service, {})}
        for service in ("api", "worker", "scheduler")
    }


def test_validate_accepts_consistent_production_configuration():
    validate(_variables(), public_base_url="https://relay.example.com")


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"worker": {"SERVICE_ROLE": "api"}}, "worker:SERVICE_ROLE_must_equal_worker"),
        ({"scheduler": {"TESTING": "true"}}, "scheduler:TESTING_must_equal_false"),
        ({"api": {"PUBLIC_BASE_URL": "https://other.example.com"}}, "PUBLIC_BASE_URL"),
        ({"worker": {"PLATFORM_SECRET_KEYS": "different"}}, "PLATFORM_SECRET_KEYS"),
        ({"scheduler": {"XCHAT_ENABLED": ""}}, "scheduler:missing_XCHAT_ENABLED"),
        ({"worker": {"XCHAT_ENABLED": "flase"}}, "worker:invalid_settings"),
        (
            {"api": {"OPENAI_TIMEOUT_SECONDS": "31"}},
            "shared_variable_partial:OPENAI_TIMEOUT_SECONDS",
        ),
    ],
)
def test_validate_rejects_unsafe_or_divergent_configuration(
    overrides: dict[str, dict[str, str]],
    expected: str,
):
    with pytest.raises(ValueError, match=expected):
        validate(
            _variables(**overrides),
            public_base_url="https://relay.example.com",
        )
