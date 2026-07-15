# 配置校验测试（Plan 2c Task 0）：生产环境拒绝默认/空凭证
import pytest

from social_reply.shared.config import Settings

# 绕过 .env 与环境变量干扰的相关变量名
_ENV_KEYS = [
    "CHATWOOT_WEBHOOK_SECRET",
    "CHATWOOT_API_TOKEN",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "TESTING",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 清空相关环境变量，避免本机 .env/环境影响构造参数
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make(**kwargs: object) -> Settings:
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
            llm_provider="openai",
        )


def test_非测试环境_凭证齐全通过() -> None:
    settings = _make(
        testing=False,
        chatwoot_webhook_secret="real-secret",
        chatwoot_api_token="real-token",
        llm_provider="openai",
        openai_api_key="sk-test",
    )
    assert settings.openai_api_key == "sk-test"
