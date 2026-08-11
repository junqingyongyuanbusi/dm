"""Email account credential probing and direct-account provisioning."""

import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from social_reply.application.account_management.provisioning import provision_direct_account
from social_reply.application.account_management.service import AccountConnectionResult
from social_reply.connectors.email.client import EmailClient
from social_reply.connectors.email.contracts import (
    MAX_EMAIL_CREDENTIAL_CHARS,
    MAX_EMAIL_MAILBOX_CHARS,
    MAX_SENDER_NAME_CHARS,
    normalize_email_address,
    validate_email_account_text,
)
from social_reply.connectors.email.imap_client import EmailImapClient
from social_reply.connectors.email.network import (
    normalize_hostname,
    require_allowed_host,
    validate_port,
)
from social_reply.shared.config import get_settings


class _ImapProbeClient(Protocol):
    async def connect(self) -> int: ...

    async def aclose(self) -> None: ...


class _SmtpProbeClient(Protocol):
    async def probe(self) -> None: ...

    async def aclose(self) -> None: ...


EmailImapClientFactory = Callable[..., _ImapProbeClient]
EmailSmtpClientFactory = Callable[..., _SmtpProbeClient]


def _required_secret(value: str, field_name: str, *, maximum: int) -> str:
    try:
        return validate_email_account_text(value, maximum=maximum)
    except ValueError as exc:
        error = (
            f"missing_{field_name}"
            if not isinstance(value, str) or not value.strip()
            else f"invalid_{field_name}"
        )
        raise ValueError(error) from exc


def _validated_text(
    value: str | None,
    field_name: str,
    *,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"missing_{field_name}")
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid_{field_name}")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"invalid_{field_name}")
    return value


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def connect_email_account(
    *,
    email_address: str,
    username: str,
    password: str,
    imap_host: str,
    imap_port: int = 993,
    mailbox: str = "INBOX",
    smtp_host: str,
    smtp_port: int = 465,
    smtp_security: str = "ssl",
    from_name: str | None = None,
    internal_domain_policy: str = "ignore",
    public_base_url: str,
    tenant_id: str = "default",
    brand_id: str = "default",
    name: str | None = None,
    public_id: str | None = None,
    secrets_root: Path = Path(".secrets/accounts"),
    automation_default: str = "BOT_DRAFT_ONLY",
    imap_client_factory: EmailImapClientFactory = EmailImapClient,
    smtp_client_factory: EmailSmtpClientFactory = EmailClient,
    provisioning_job_id: uuid.UUID | None = None,
    provisioning_attempt_count: int | None = None,
) -> AccountConnectionResult:
    """Probe read-only IMAP and SMTP auth before atomically provisioning an email account."""

    del public_base_url  # Email polling and delivery do not expose a webhook endpoint.
    settings = get_settings()
    if not settings.email_enabled:
        raise ValueError("email_integration_disabled")
    if automation_default != "BOT_DRAFT_ONLY":
        raise ValueError("email_requires_bot_draft_only")
    if smtp_security not in {"ssl", "starttls"}:
        raise ValueError("smtp_security_invalid")
    if internal_domain_policy not in {"ignore", "allow"}:
        raise ValueError("internal_domain_policy_invalid")

    canonical_address = normalize_email_address(email_address)
    canonical_imap_host = normalize_hostname(imap_host)
    canonical_smtp_host = normalize_hostname(smtp_host)
    canonical_imap_port = validate_port(imap_port)
    canonical_smtp_port = validate_port(smtp_port)
    canonical_mailbox = _required_secret(
        mailbox,
        "mailbox",
        maximum=MAX_EMAIL_MAILBOX_CHARS,
    )
    canonical_from_name = _validated_text(
        from_name,
        "from_name",
        maximum=MAX_SENDER_NAME_CHARS,
        optional=True,
    )
    canonical_username = _required_secret(
        username,
        "username",
        maximum=MAX_EMAIL_CREDENTIAL_CHARS,
    )
    canonical_password = _required_secret(
        password,
        "password",
        maximum=MAX_EMAIL_CREDENTIAL_CHARS,
    )
    require_allowed_host(canonical_imap_host, settings.email_allowed_hosts)
    require_allowed_host(canonical_smtp_host, settings.email_allowed_hosts)

    imap_client = imap_client_factory(
        imap_host=canonical_imap_host,
        imap_port=canonical_imap_port,
        username=canonical_username,
        password=canonical_password,
        mailbox=canonical_mailbox,
        timeout=settings.email_network_timeout_seconds,
        allowed_hosts=settings.email_allowed_hosts,
    )
    smtp_client: _SmtpProbeClient | None = None
    try:
        await imap_client.connect()
        smtp_client = smtp_client_factory(
            smtp_host=canonical_smtp_host,
            smtp_port=canonical_smtp_port,
            smtp_security=smtp_security,
            username=canonical_username,
            password=canonical_password,
            self_address=canonical_address,
            from_name=canonical_from_name,
            timeout=settings.email_network_timeout_seconds,
            allowed_hosts=settings.email_allowed_hosts,
        )
        await smtp_client.probe()
    finally:
        if smtp_client is not None:
            await smtp_client.aclose()
        await imap_client.aclose()

    resolved_name = name or canonical_from_name or canonical_address
    account_id, resolved_public_id = await provision_direct_account(
        platform="email",
        external_account_id=canonical_address,
        tenant_id=tenant_id,
        brand_id=brand_id,
        name=resolved_name,
        public_id=public_id,
        public_id_prefix="email",
        secrets_root=secrets_root,
        credential_bundle={"username": canonical_username, "password": canonical_password},
        webhook_secret_bundle=None,
        config={
            "self_address": canonical_address,
            "imap_host": canonical_imap_host,
            "imap_port": canonical_imap_port,
            "mailbox": canonical_mailbox,
            "smtp_host": canonical_smtp_host,
            "smtp_port": canonical_smtp_port,
            "smtp_security": smtp_security,
            "from_name": canonical_from_name,
            "internal_domain_policy": internal_domain_policy,
            "email_health_status": "READY",
            "email_health_checked_at": _utc_now_iso(),
            "email_health_error_code": None,
        },
        capability={"dm": True},
        automation_default=automation_default,
        status="active",
        provisioning_job_id=provisioning_job_id,
        provisioning_attempt_count=provisioning_attempt_count,
    )
    return AccountConnectionResult(
        account_id=account_id,
        platform="email",
        external_account_id=canonical_address,
        public_id=resolved_public_id,
        webhook_url="",
        name=resolved_name,
        automation_default=automation_default,
        manual_steps=(
            "确认邮箱仅由 Reply Core 以只读 IMAP 方式收取新邮件。",
            "在启用自动发送前先保持 BOT_DRAFT_ONLY，并由人工审核草稿。",
        ),
    )
