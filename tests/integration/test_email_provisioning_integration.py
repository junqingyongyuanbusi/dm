import uuid

import pytest
from sqlalchemy import func, select

from social_reply.application.account_management import email
from social_reply.connectors.errors import PermanentSendError
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle


class _FakeImapClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def connect(self) -> int:
        return 42

    async def aclose(self) -> None:
        self.closed = True


class _FakeSmtpClient:
    def __init__(self, *, failure: Exception | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.failure = failure
        self.closed = False

    async def probe(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


def _imap_factory(**kwargs):
    return _FakeImapClient(**kwargs)


def _smtp_factory(**kwargs):
    return _FakeSmtpClient(**kwargs)


async def test_email_provisioning_persists_ready_contract_and_rotates_config_version(
    migrated_db, monkeypatch, tmp_path
):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)

    first = await email.connect_email_account(
        email_address=" Support@Example.COM. ",
        username="mail-user-1",
        password="mail-password-1",
        imap_host="imap.larksuite.com",
        smtp_host="smtp.larksuite.com",
        from_name="Support",
        public_base_url="https://reply.example.com",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        imap_client_factory=_imap_factory,
        smtp_client_factory=_smtp_factory,
    )

    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, first.account_id)
    assert account.platform == "email"
    assert account.external_account_id == "Support@example.com"
    assert account.name == "Support"
    assert account.status == "active"
    assert account.automation_default == "BOT_DRAFT_ONLY"
    assert account.capability == {"dm": True, "max_text_length": 4000}
    assert account.config_version == 1
    assert account.config == {
        "delivery_mode": "direct",
        "self_address": "Support@example.com",
        "imap_host": "imap.larksuite.com",
        "imap_port": 993,
        "mailbox": "INBOX",
        "smtp_host": "smtp.larksuite.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "from_name": "Support",
        "internal_domain_policy": "ignore",
        "email_health_status": "READY",
        "email_health_checked_at": account.config["email_health_checked_at"],
        "email_health_error_code": None,
    }
    assert decrypt_secret_bundle(account.credential_bundle) == {
        "username": "mail-user-1",
        "password": "mail-password-1",
    }

    second = await email.connect_email_account(
        email_address="Support@example.com",
        username="mail-user-2",
        password="mail-password-2",
        imap_host="imap.larksuite.com",
        smtp_host="smtp.larksuite.com",
        smtp_port=587,
        smtp_security="starttls",
        from_name=None,
        internal_domain_policy="allow",
        public_base_url="https://reply.example.com",
        tenant_id="tenant-a",
        brand_id="brand-b",
        secrets_root=tmp_path,
        imap_client_factory=_imap_factory,
        smtp_client_factory=_smtp_factory,
    )

    assert second.account_id == first.account_id
    assert second.public_id == first.public_id
    async with get_session_factory()() as session:
        updated = await session.get(models.PlatformAccount, first.account_id)
    assert updated.config_version == 2
    assert updated.brand_id == "brand-b"
    assert updated.name == "Support@example.com"
    assert updated.config["smtp_port"] == 587
    assert updated.config["smtp_security"] == "starttls"
    assert updated.config["from_name"] is None
    assert updated.config["internal_domain_policy"] == "allow"
    assert decrypt_secret_bundle(updated.credential_bundle) == {
        "username": "mail-user-2",
        "password": "mail-password-2",
    }


async def test_email_provisioning_claim_fence_blocks_stale_account_write(
    migrated_db, monkeypatch, tmp_path
):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)
    job_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            models.ProvisioningJob(
                id=job_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="email",
                actor="admin",
                idempotency_key=uuid.uuid4().hex,
                request={},
                staging_secret=None,
                status="NEEDS_ACTION",
                current_step="FAILED",
                attempt_count=1,
                result={"requires_secret_resubmission": True, "required_secret": "password"},
            )
        )
        await session.commit()

    with pytest.raises(ValueError, match="provisioning_claim_lost"):
        await email.connect_email_account(
            email_address="Support@example.com",
            username="old-user",
            password="old-password",
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            public_base_url="https://reply.example.com",
            tenant_id="tenant-a",
            brand_id="brand-a",
            secrets_root=tmp_path,
            imap_client_factory=_imap_factory,
            smtp_client_factory=_smtp_factory,
            provisioning_job_id=job_id,
            provisioning_attempt_count=1,
        )

    async with get_session_factory()() as session:
        account_count = await session.scalar(
            select(func.count())
            .select_from(models.PlatformAccount)
            .where(
                models.PlatformAccount.tenant_id == "tenant-a",
                models.PlatformAccount.platform == "email",
            )
        )
    assert account_count == 0


async def test_failed_email_reprovision_does_not_overwrite_existing_account(
    migrated_db, monkeypatch, tmp_path
):
    settings = email.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(email, "get_settings", lambda: settings)
    created = await email.connect_email_account(
        email_address="support@example.com",
        username="mail-user",
        password="good-password",
        imap_host="imap.larksuite.com",
        smtp_host="smtp.larksuite.com",
        from_name="Original Support",
        public_base_url="https://reply.example.com",
        tenant_id="tenant-a",
        brand_id="brand-a",
        secrets_root=tmp_path,
        imap_client_factory=_imap_factory,
        smtp_client_factory=_smtp_factory,
    )

    def failing_smtp_factory(**kwargs):
        return _FakeSmtpClient(
            failure=PermanentSendError("smtp_535", "password and banner redacted"),
            **kwargs,
        )

    with pytest.raises(PermanentSendError, match="smtp_535"):
        await email.connect_email_account(
            email_address="support@example.com",
            username="new-user",
            password="bad-password",
            imap_host="imap.larksuite.com",
            smtp_host="smtp.larksuite.com",
            from_name="Overwritten Support",
            public_base_url="https://reply.example.com",
            tenant_id="tenant-a",
            brand_id="brand-overwrite",
            secrets_root=tmp_path,
            imap_client_factory=_imap_factory,
            smtp_client_factory=failing_smtp_factory,
        )

    async with get_session_factory()() as session:
        accounts = list(
            (
                await session.execute(
                    select(models.PlatformAccount).where(
                        models.PlatformAccount.tenant_id == "tenant-a",
                        models.PlatformAccount.platform == "email",
                    )
                )
            ).scalars()
        )
    assert len(accounts) == 1
    [unchanged] = accounts
    assert unchanged.id == created.account_id
    assert unchanged.brand_id == "brand-a"
    assert unchanged.name == "Original Support"
    assert unchanged.config_version == 1
    assert decrypt_secret_bundle(unchanged.credential_bundle) == {
        "username": "mail-user",
        "password": "good-password",
    }
