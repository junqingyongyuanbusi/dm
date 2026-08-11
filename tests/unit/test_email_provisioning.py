import ssl
import uuid

import pytest

from social_reply.application.account_management import email
from social_reply.connectors.email.client import EmailClient
from social_reply.connectors.email.imap_client import EmailImapClient, ImapClientError
from social_reply.connectors.errors import PermanentSendError


class _FakeImapClient:
    def __init__(self, *, fail: Exception | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.fail = fail
        self.connected = False
        self.closed = False

    async def connect(self) -> int:
        if self.fail is not None:
            raise self.fail
        self.connected = True
        return 42

    async def aclose(self) -> None:
        self.closed = True


class _FakeSmtpClient:
    def __init__(self, *, fail: Exception | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.fail = fail
        self.probed = False
        self.closed = False

    async def probe(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.probed = True

    async def aclose(self) -> None:
        self.closed = True


async def test_connect_email_probes_both_paths_before_provisioning(monkeypatch, tmp_path):
    settings = email.get_settings().model_copy(
        update={
            "email_enabled": True,
            "email_allowed_hosts": frozenset({"imap.larksuite.com", "smtp.larksuite.com"}),
        }
    )
    monkeypatch.setattr(email, "get_settings", lambda: settings)
    imap_clients = []
    smtp_clients = []
    provisioned = {}

    def imap_factory(**kwargs):
        client = _FakeImapClient(**kwargs)
        imap_clients.append(client)
        return client

    def smtp_factory(**kwargs):
        client = _FakeSmtpClient(**kwargs)
        smtp_clients.append(client)
        return client

    async def fake_provision(**kwargs):
        provisioned.update(kwargs)
        assert imap_clients[0].connected is True
        assert smtp_clients[0].probed is True
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "email_public"

    monkeypatch.setattr(email, "provision_direct_account", fake_provision)

    result = await email.connect_email_account(
        email_address=" Support@Example.COM. ",
        username="  mail-user  ",
        password="  mail-password  ",
        imap_host=" IMAP.LARKSUITE.COM. ",
        smtp_host=" SMTP.LARKSUITE.COM. ",
        smtp_port=587,
        smtp_security="starttls",
        mailbox="  Support Folder  ",
        from_name=None,
        internal_domain_policy="allow",
        public_base_url="https://reply.example.com",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        imap_client_factory=imap_factory,
        smtp_client_factory=smtp_factory,
    )

    assert result.external_account_id == "Support@example.com"
    assert result.webhook_url == ""
    assert result.name == "Support@example.com"
    assert result.manual_steps
    assert imap_clients[0].closed is True
    assert smtp_clients[0].closed is True
    assert provisioned["external_account_id"] == "Support@example.com"
    assert provisioned["credential_bundle"] == {
        "username": "  mail-user  ",
        "password": "  mail-password  ",
    }
    assert imap_clients[0].kwargs["username"] == "  mail-user  "
    assert imap_clients[0].kwargs["password"] == "  mail-password  "
    assert imap_clients[0].kwargs["mailbox"] == "  Support Folder  "
    assert provisioned["capability"] == {"dm": True}
    assert provisioned["status"] == "active"
    assert provisioned["automation_default"] == "BOT_DRAFT_ONLY"
    assert provisioned["config"] == {
        "self_address": "Support@example.com",
        "imap_host": "imap.larksuite.com",
        "imap_port": 993,
        "mailbox": "  Support Folder  ",
        "smtp_host": "smtp.larksuite.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "from_name": None,
        "internal_domain_policy": "allow",
        "email_health_status": "READY",
        "email_health_checked_at": provisioned["config"]["email_health_checked_at"],
        "email_health_error_code": None,
    }


async def test_connect_email_does_not_provision_when_either_probe_fails(monkeypatch):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)

    async def unexpected_provision(**_kwargs):
        raise AssertionError("failed probes must not provision or overwrite an account")

    monkeypatch.setattr(email, "provision_direct_account", unexpected_provision)

    for fail_imap, fail_smtp in (
        (ImapClientError("imap_login_failed"), None),
        (None, RuntimeError("smtp probe failed")),
    ):
        imap_clients = []
        smtp_clients = []

        def imap_factory(*, _failure=fail_imap, _clients=imap_clients, **kwargs):
            client = _FakeImapClient(fail=_failure, **kwargs)
            _clients.append(client)
            return client

        def smtp_factory(*, _failure=fail_smtp, _clients=smtp_clients, **kwargs):
            client = _FakeSmtpClient(fail=_failure, **kwargs)
            _clients.append(client)
            return client

        with pytest.raises(type(fail_imap or fail_smtp)):
            await email.connect_email_account(
                email_address="support@example.com",
                username="mail-user",
                password="mail-password",
                imap_host="imap.larksuite.com",
                smtp_host="smtp.larksuite.com",
                public_base_url="https://reply.example.com",
                imap_client_factory=imap_factory,
                smtp_client_factory=smtp_factory,
            )

        assert imap_clients[0].closed is True
        if smtp_clients:
            assert smtp_clients[0].closed is True


@pytest.mark.parametrize(
    ("failing_probe", "expected_type", "expected_code"),
    [
        ("imap", ImapClientError, "imap_tls_invalid"),
        ("smtp", PermanentSendError, "smtp_tls_invalid"),
    ],
)
async def test_connect_email_maps_noncertificate_tls_failures_before_provisioning(
    monkeypatch, failing_probe, expected_type, expected_code
):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)

    async def unexpected_provision(**_kwargs):
        raise AssertionError("TLS probe failures must not persist account credentials")

    monkeypatch.setattr(email, "provision_direct_account", unexpected_provision)

    def fail_tls(*_args, **_kwargs):
        raise ssl.SSLError("SECRET provider TLS alert")

    def imap_factory(**kwargs):
        if failing_probe != "imap":
            return _FakeImapClient(**kwargs)
        return EmailImapClient(
            **kwargs,
            imap_factory=fail_tls,
            network_validator=lambda _host, _port: None,
        )

    def smtp_factory(**kwargs):
        if failing_probe != "smtp":
            return _FakeSmtpClient(**kwargs)
        return EmailClient(
            **kwargs,
            smtp_factory=fail_tls,
            network_validator=lambda _host, _port: None,
        )

    with pytest.raises(expected_type) as excinfo:
        await email.connect_email_account(
            email_address="support@example.com",
            username="mail-user",
            password="mail-password",
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
            imap_client_factory=imap_factory,
            smtp_client_factory=smtp_factory,
        )

    assert excinfo.value.code == expected_code
    assert str(excinfo.value) == expected_code


@pytest.mark.parametrize(
    ("username", "password", "expected_error"),
    [
        ("", "mail-password", "missing_username"),
        ("mail-user", "", "missing_password"),
        ("u" * 513, "mail-password", "invalid_username"),
        ("mail-user", "p" * 513, "invalid_password"),
    ],
)
async def test_connect_email_rejects_invalid_credentials_before_provisioning(
    monkeypatch, username, password, expected_error
):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)

    def unexpected_client(**_kwargs):
        raise AssertionError("invalid credentials must not reach network probes")

    async def unexpected_provision(**_kwargs):
        raise AssertionError("invalid credentials must not be persisted")

    monkeypatch.setattr(email, "provision_direct_account", unexpected_provision)

    with pytest.raises(ValueError) as exc_info:
        await email.connect_email_account(
            email_address="support@example.com",
            username=username,
            password=password,
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
            imap_client_factory=unexpected_client,
            smtp_client_factory=unexpected_client,
        )

    assert str(exc_info.value) == expected_error
    for credential in (username, password):
        if credential:
            assert credential not in str(exc_info.value)


async def test_connect_email_requires_enabled_draft_only_and_allowlisted_hosts(monkeypatch):
    base = email.get_settings()
    monkeypatch.setattr(
        email,
        "get_settings",
        lambda: base.model_copy(update={"email_enabled": False}),
    )
    with pytest.raises(ValueError, match="email_integration_disabled"):
        await email.connect_email_account(
            email_address="support@example.com",
            username="mail-user",
            password="mail-password",
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
        )

    enabled = base.model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: enabled)
    with pytest.raises(ValueError, match="email_requires_bot_draft_only"):
        await email.connect_email_account(
            email_address="support@example.com",
            username="mail-user",
            password="mail-password",
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
            automation_default="BOT_ACTIVE",
        )
    with pytest.raises(ValueError, match="email_hostname_not_allowed"):
        await email.connect_email_account(
            email_address="support@example.com",
            username="mail-user",
            password="mail-password",
            imap_host="imap.example.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
        )
