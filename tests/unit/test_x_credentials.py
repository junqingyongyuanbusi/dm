from types import SimpleNamespace

from social_reply.application.account_management import x_credentials as module


def test_x_credentials_prefer_deployment_app_keys(monkeypatch):
    account = SimpleNamespace(
        credential_bundle={
            "consumer_key": "env-key",
            "consumer_secret": "old-secret",
            "access_token": "user-token",
            "access_token_secret": "user-secret",
        }
    )
    settings = SimpleNamespace(x_app_credentials=("env-key", "env-secret"))
    monkeypatch.setattr(module, "get_settings", lambda: settings)

    assert module.x_credentials(account) == {
        "consumer_key": "env-key",
        "consumer_secret": "env-secret",
        "access_token": "user-token",
        "access_token_secret": "user-secret",
    }


def test_x_credentials_do_not_reassign_accounts_from_another_app(monkeypatch):
    account = SimpleNamespace(
        credential_bundle={
            "consumer_key": "other-app-key",
            "consumer_secret": "other-app-secret",
            "access_token": "user-token",
            "access_token_secret": "user-secret",
        }
    )
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(x_app_credentials=("env-key", "env-secret")),
    )

    assert module.x_credentials(account)["consumer_key"] == "other-app-key"


def test_x_credentials_keep_legacy_accounts_usable_without_env(monkeypatch):
    account = SimpleNamespace(
        credential_bundle={
            "consumer_key": "legacy-key",
            "consumer_secret": "legacy-secret",
            "access_token": "user-token",
            "access_token_secret": "user-secret",
        }
    )
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(x_app_credentials=None),
    )

    assert module.x_credentials(account)["consumer_key"] == "legacy-key"
