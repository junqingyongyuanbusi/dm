import uuid

import pytest

from social_reply.application.platform_accounts import PlatformAccountRuntime
from social_reply.connectors import registry
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


def _account(
    *,
    config_version: int = 1,
    credentials: dict | None = None,
    config: dict | None = None,
    external_account_id: str | None = "Support@example.com",
) -> PlatformAccountRuntime:
    return PlatformAccountRuntime(
        id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
        tenant_id="default",
        brand_id="default",
        platform="email",
        platform_app_id=None,
        name="Email",
        external_account_id=external_account_id,
        public_id="email_test",
        credential_bundle_data=encrypt_secret_bundle(
            credentials or {"username": "smtp-user", "password": "password-1"}
        ),
        webhook_secret_bundle_data=None,
        config=config
        or {
            "smtp_host": " SMTP.Example.COM. ",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "self_address": " Support@Example.COM ",
            "from_name": "Customer Support",
        },
        capability={"dm": True},
        config_version=config_version,
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )


def test_email_sender_exact_construction_contract(monkeypatch):
    created = []

    class FakeEmailClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr(registry, "EmailClient", FakeEmailClient)

    sender = registry._build_sender(
        _account(credentials={"username": " smtp-user ", "password": " password-1 "})
    )

    assert sender is created[0]
    assert sender.kwargs == {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "username": " smtp-user ",
        "password": " password-1 ",
        "self_address": "Support@example.com",
        "from_name": "Customer Support",
        "timeout": 10.0,
        "allowed_hosts": frozenset({"imap.larksuite.com", "smtp.larksuite.com"}),
    }


def test_email_sender_allows_optional_from_name(monkeypatch):
    created = []

    class FakeEmailClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr(registry, "EmailClient", FakeEmailClient)
    account = _account(
        config={
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "self_address": "Support@example.com",
        }
    )

    sender = registry._build_sender(account)

    assert sender is created[0]
    assert sender.kwargs["from_name"] is None


@pytest.mark.parametrize(
    ("credentials", "config", "external_account_id", "error_code"),
    [
        ({"password": "password"}, None, "Support@example.com", "email_username_invalid"),
        ({"username": "smtp-user"}, None, "Support@example.com", "email_password_invalid"),
        (None, {"smtp_port": 587}, "Support@example.com", "email_smtp_security_invalid"),
        (
            None,
            {
                "smtp_host": "localhost",
                "smtp_port": 587,
                "smtp_security": "starttls",
                "self_address": "Support@example.com",
                "from_name": "Support",
            },
            "Support@example.com",
            "email_hostname_forbidden",
        ),
        (
            None,
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "smtp_security": "starttls",
                "self_address": "Support@example.com",
                "from_name": "Support",
            },
            "Support@example.com",
            "email_port_invalid",
        ),
        (
            None,
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_security": "plain",
                "self_address": "Support@example.com",
                "from_name": "Support",
            },
            "Support@example.com",
            "email_smtp_security_invalid",
        ),
        (
            None,
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_security": "starttls",
                "self_address": "not-an-address",
                "from_name": "Support",
            },
            "Support@example.com",
            "email_self_address_invalid",
        ),
        (None, None, "support@example.com", "email_self_address_scope_mismatch"),
    ],
)
def test_email_sender_fails_closed_for_missing_or_invalid_fields(
    credentials,
    config,
    external_account_id,
    error_code,
):
    account = _account(
        credentials=credentials,
        config=config,
        external_account_id=external_account_id,
    )

    with pytest.raises(LookupError, match=error_code):
        registry._build_sender(account)
