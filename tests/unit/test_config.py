# 配置校验测试（Plan 2c Task 0）：生产环境拒绝默认/空凭证
import pytest

from social_reply.shared.config import Settings

# 绕过 .env 与环境变量干扰的相关变量名
_ENV_KEYS = [
    "CHATWOOT_WEBHOOK_SECRET",
    "CHATWOOT_API_TOKEN",
    "CONTROL_API_KEY",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "PLATFORM_SECRET_KEYS",
    "X_API_KEY",
    "X_API_SECRET",
    "FACEBOOK_APP_ID",
    "FACEBOOK_APP_SECRET",
    "META_VERIFY_TOKEN",
    "INSTAGRAM_APP_ID",
    "INSTAGRAM_APP_SECRET",
    "INSTAGRAM_VERIFY_TOKEN",
    "ADMIN_ALLOWED_TENANTS",
    "CONVERSATION_HISTORY_LIMIT",
    "CONVERSATION_HISTORY_MAX_CHARS",
    "TESTING",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 清空相关环境变量，避免本机 .env/环境影响构造参数
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make(**kwargs: object) -> Settings:
    kwargs.setdefault("platform_secret_keys", "Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=")
    # 显式关闭 env 文件读取，纯用构造参数
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_testing_true_默认值可用() -> None:
    settings = _make(testing=True)
    assert settings.chatwoot_api_token == "dev-local-token"
    assert settings.openai_api_key == ""
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_timeout_seconds == 30.0


def test_非测试环境_默认_chatwoot_api_token_拒绝() -> None:
    with pytest.raises(ValueError, match="CHATWOOT_API_TOKEN"):
        _make(testing=False, chatwoot_webhook_secret="real-secret")


def test_非测试环境_空_chatwoot_api_token_拒绝() -> None:
    with pytest.raises(ValueError, match="CHATWOOT_API_TOKEN"):
        _make(testing=False, chatwoot_webhook_secret="real-secret", chatwoot_api_token="")


def test_非测试环境_openai_provider_空_key_拒绝() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            llm_provider="openai",
        )


def test_非测试环境_凭证齐全通过() -> None:
    settings = _make(
        testing=False,
        chatwoot_webhook_secret="real-secret",
        chatwoot_api_token="real-token",
        control_api_key="control-token",
        llm_provider="openai",
        openai_api_key="sk-test",
    )
    assert settings.openai_api_key == "sk-test"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("conversation_history_limit", -1),
        ("conversation_history_limit", 51),
        ("conversation_history_max_chars", -1),
        ("conversation_history_max_chars", 50001),
    ],
)
def test_conversation_history_bounds_are_validated(name: str, value: int) -> None:
    with pytest.raises(ValueError):
        _make(testing=True, **{name: value})


def test_x_app_credentials_must_be_configured_as_a_pair() -> None:
    with pytest.raises(ValueError, match="X_API_KEY"):
        _make(testing=True, x_api_key="key-only")

    settings = _make(testing=True, x_api_key="key", x_api_secret="secret")
    assert settings.x_app_credentials == ("key", "secret")


def test_meta_app_credentials_must_be_configured_as_pairs() -> None:
    with pytest.raises(ValueError, match="FACEBOOK_APP_ID"):
        _make(testing=True, facebook_app_id="facebook-only")
    with pytest.raises(ValueError, match="INSTAGRAM_APP_ID"):
        _make(testing=True, instagram_app_secret="instagram-secret-only")

    settings = _make(
        testing=True,
        facebook_app_id="facebook-app",
        facebook_app_secret="facebook-secret",
        instagram_app_id="instagram-app",
        instagram_app_secret="instagram-secret",
    )
    assert settings.facebook_app_credentials == ("facebook-app", "facebook-secret")
    assert settings.instagram_app_credentials == ("instagram-app", "instagram-secret")


def test_非测试环境_空_admin_allowed_tenants_拒绝() -> None:
    with pytest.raises(ValueError, match="ADMIN_ALLOWED_TENANTS"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            admin_session_secret="x" * 32,
            admin_username="admin",
            admin_password="password",
            public_base_url="https://reply.example.com",
            admin_allowed_tenants="",
            llm_provider="openai",
            openai_api_key="sk-test",
        )


def test_非测试环境_stub_provider_拒绝() -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDER=stub"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
            control_api_key="control-token",
            admin_session_secret="x" * 32,
            admin_username="admin",
            admin_password="password",
            public_base_url="https://reply.example.com",
            llm_provider="stub",
        )


def test_非测试环境_空_control_api_key_拒绝() -> None:
    with pytest.raises(ValueError, match="CONTROL_API_KEY"):
        _make(
            testing=False,
            chatwoot_webhook_secret="real-secret",
            chatwoot_api_token="real-token",
        )
