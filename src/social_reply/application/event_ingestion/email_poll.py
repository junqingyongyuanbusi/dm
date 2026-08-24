"""Durable, account-isolated IMAP polling without persisting message content."""

import asyncio
import hashlib
import imaplib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.event_ingestion.poll_sync import (
    CheckpointStream,
    ClaimedCheckpoint,
    GapSpec,
    GapType,
    LeaseLostError,
    claim_checkpoint,
    complete_checkpoint,
    ensure_checkpoint,
    fail_run,
    require_claim,
)
from social_reply.application.platform_accounts import (
    PlatformAccountRuntime,
    list_active_accounts_by_platform,
)
from social_reply.connectors.email.adapter import EmailInboundAdapter
from social_reply.connectors.email.contracts import (
    MAX_EMAIL_CREDENTIAL_CHARS,
    MAX_EMAIL_MAILBOX_CHARS,
    MAX_INBOUND_MESSAGE_BYTES,
    STATUS_IGNORED_TOO_LARGE,
    normalize_email_address,
    validate_email_account_text,
)
from social_reply.connectors.email.imap_client import EmailImapClient, ImapClientError
from social_reply.connectors.email.network import EmailNetworkError
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import acquire_xact_lock
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)


def _log_sanitized_exception(message: str, exc: Exception, *args: object) -> None:
    sanitized = RuntimeError("exception details redacted")
    logger.exception(
        message,
        *args,
        type(exc).__name__,
        exc_info=(RuntimeError, sanitized, exc.__traceback__),
    )


_CURSOR_VERSION = 1
_MAX_IMAP_NUMBER = (1 << 32) - 1
_DEFAULT_OWNER = f"email-imap:{uuid.uuid4()}"
_CHECKPOINT_LEASE_SECONDS = 300
_MAX_ACCOUNT_POLL_SECONDS = 240.0
_MAX_CONCURRENT_ACCOUNTS = 4
_SOURCE = "email_poll"
_EVENT_NAMESPACE = "email.imap"
_STABLE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_]{1,128}$")


class _EmailImapClient(Protocol):
    async def connect(self) -> int: ...

    async def search_uids(self, *, start_uid: int = 1) -> tuple[int, ...]: ...

    async def fetch_message_size(self, uid: int) -> int: ...

    async def fetch_message(self, uid: int) -> bytes: ...

    async def aclose(self) -> None: ...


EmailImapClientFactory = Callable[..., _EmailImapClient]


class EmailPollAccountError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EmailCursorError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailCursor:
    uidvalidity: int
    last_uid: int

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": _CURSOR_VERSION,
                "uidvalidity": self.uidvalidity,
                "last_uid": self.last_uid,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def parse(cls, value: str) -> "EmailCursor":
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise EmailCursorError("EMAIL_CURSOR_INVALID") from exc
        if not isinstance(parsed, dict) or set(parsed) != {
            "version",
            "uidvalidity",
            "last_uid",
        }:
            raise EmailCursorError("EMAIL_CURSOR_INVALID")
        if parsed["version"] != _CURSOR_VERSION or type(parsed["version"]) is not int:
            raise EmailCursorError("EMAIL_CURSOR_INVALID")
        uidvalidity = parsed["uidvalidity"]
        last_uid = parsed["last_uid"]
        if type(uidvalidity) is not int or not 1 <= uidvalidity <= _MAX_IMAP_NUMBER:
            raise EmailCursorError("EMAIL_CURSOR_INVALID")
        if type(last_uid) is not int or not 0 <= last_uid <= _MAX_IMAP_NUMBER:
            raise EmailCursorError("EMAIL_CURSOR_INVALID")
        return cls(uidvalidity=uidvalidity, last_uid=last_uid)


@dataclass(frozen=True)
class _EmailAccountContract:
    imap_host: str
    imap_port: int
    username: str
    password: str
    self_address: str
    mailbox: str
    internal_domain_policy: str


@dataclass(frozen=True)
class _RawReservation:
    id: uuid.UUID
    already_normalized: bool


async def poll_email_messages(
    *,
    scheduler_owner: str | None = None,
    client_factory: EmailImapClientFactory = EmailImapClient,
) -> list[str]:
    settings = get_settings()
    if not settings.email_enabled:
        return []

    owner = scheduler_owner or _DEFAULT_OWNER
    accounts = await list_active_accounts_by_platform("email")
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ACCOUNTS)

    async def poll_account(account: PlatformAccountRuntime) -> list[str]:
        async with semaphore:
            return await _poll_one_account(
                account,
                owner=owner,
                poll_interval_seconds=settings.email_poll_interval_seconds,
                max_messages=settings.email_max_messages_per_poll,
                network_timeout_seconds=settings.email_network_timeout_seconds,
                allowed_hosts=settings.email_allowed_hosts,
                client_factory=client_factory,
            )

    # gather preserves account-list order, so completion timing cannot reorder the public result.
    account_results = await asyncio.gather(*(poll_account(account) for account in accounts))
    return [event_id for result in account_results for event_id in result]


async def _poll_one_account(
    account: PlatformAccountRuntime,
    *,
    owner: str,
    poll_interval_seconds: int,
    max_messages: int,
    network_timeout_seconds: float,
    allowed_hosts: frozenset[str],
    client_factory: EmailImapClientFactory,
) -> list[str]:
    claim: ClaimedCheckpoint | None = None
    try:
        checkpoint = await ensure_checkpoint(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            stream=CheckpointStream.EMAIL_IMAP,
            scope_key="",
        )
        claim = await claim_checkpoint(
            checkpoint.id,
            owner=owner,
            lease_seconds=_CHECKPOINT_LEASE_SECONDS,
        )
        if claim is None:
            return []
        budget_seconds = _account_poll_budget_seconds(
            network_timeout_seconds=network_timeout_seconds,
            max_messages=max_messages,
        )
        async with asyncio.timeout(budget_seconds):
            return await _poll_account(
                account,
                claim=claim,
                poll_interval_seconds=poll_interval_seconds,
                max_messages=max_messages,
                network_timeout_seconds=network_timeout_seconds,
                allowed_hosts=allowed_hosts,
                client_factory=client_factory,
            )
    except TimeoutError:
        await _record_account_failure(
            account,
            claim=claim,
            error_code="EMAIL_POLL_TIMEOUT",
            retry_after_seconds=poll_interval_seconds,
        )
        logger.warning("email poll timed out account=%s", account.id)
    except EmailNetworkError as exc:
        code = _bounded_stable_error_code(exc.code, fallback="EMAIL_NETWORK_FAILED")
        await _record_account_failure(
            account,
            claim=claim,
            error_code=code,
            retry_after_seconds=poll_interval_seconds,
        )
        logger.warning("email poll rejected account=%s code=%s", account.id, code)
    except (
        EmailCursorError,
        EmailPollAccountError,
        ImapClientError,
        LeaseLostError,
        imaplib.IMAP4.error,
    ) as exc:
        raw_imap_error = isinstance(exc, imaplib.IMAP4.error)
        code = _bounded_stable_error_code(
            None if raw_imap_error else getattr(exc, "code", str(exc)),
            fallback="IMAP_PROTOCOL_ERROR" if raw_imap_error else "EMAIL_POLL_REJECTED",
        )
        await _record_account_failure(
            account,
            claim=claim,
            error_code=code,
            retry_after_seconds=poll_interval_seconds,
        )
        logger.warning("email poll rejected account=%s code=%s", account.id, code)
    except Exception as exc:  # noqa: BLE001 - one account must not block another
        await _record_account_failure(
            account,
            claim=claim,
            error_code="EMAIL_POLL_FAILED",
            retry_after_seconds=poll_interval_seconds,
        )
        _log_sanitized_exception(
            "email poll failed account=%s error_type=%s",
            exc,
            account.id,
        )
    return []


async def _record_account_failure(
    account: PlatformAccountRuntime,
    *,
    claim: ClaimedCheckpoint | None,
    error_code: str,
    retry_after_seconds: int,
) -> None:
    if claim is None:
        return
    try:
        await fail_run(
            claim,
            error_code=error_code,
            error_message=error_code,
            retry_after_seconds=retry_after_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - failure reporting must preserve account isolation
        _log_sanitized_exception(
            "email poll failure recording failed account=%s error_code=%s error_type=%s",
            exc,
            account.id,
            error_code,
        )


async def _poll_account(
    account: PlatformAccountRuntime,
    *,
    claim: ClaimedCheckpoint,
    poll_interval_seconds: int,
    max_messages: int,
    network_timeout_seconds: float,
    allowed_hosts: frozenset[str],
    client_factory: EmailImapClientFactory,
) -> list[str]:
    cursor = EmailCursor.parse(claim.cursor) if claim.cursor is not None else None
    contract = _account_contract(account)
    client = client_factory(
        imap_host=contract.imap_host,
        imap_port=contract.imap_port,
        username=contract.username,
        password=contract.password,
        mailbox=contract.mailbox,
        timeout=network_timeout_seconds,
        allowed_hosts=allowed_hosts,
    )
    try:
        uidvalidity = await client.connect()
        await require_claim(claim)
        if cursor is None:
            anchor_uid = await _current_max_uid(client)
            await require_claim(claim)
            await _complete_checkpoint_or_raise(
                claim,
                cursor=EmailCursor(uidvalidity, anchor_uid).serialize(),
                bootstrapped=True,
                interval_seconds=poll_interval_seconds,
                page_count=1,
                occurrence_count=0,
            )
            return []

        if cursor.uidvalidity != uidvalidity:
            anchor_uid = await _current_max_uid(client)
            # Intentionally skip history in the new UID epoch: replaying it could trigger an
            # automatic-reply storm. This is an explicit data-loss boundary, while the resolved
            # gap below preserves an auditable record of the re-anchor.
            logger.warning(
                "email UIDVALIDITY changed; intentionally reanchoring without history "
                "account=%s previous=%s current=%s anchor_uid=%s",
                account.id,
                cursor.uidvalidity,
                uidvalidity,
                anchor_uid,
            )
            reanchored = EmailCursor(uidvalidity, anchor_uid)
            await require_claim(claim)
            await _complete_checkpoint_or_raise(
                claim,
                cursor=reanchored.serialize(),
                bootstrapped=True,
                interval_seconds=poll_interval_seconds,
                page_count=1,
                occurrence_count=0,
                resolved_gap=GapSpec(
                    gap_type=GapType.EMAIL_UIDVALIDITY_CHANGED,
                    candidate_cursor=reanchored.serialize(),
                    detail={
                        "previous_uidvalidity": cursor.uidvalidity,
                        "current_uidvalidity": uidvalidity,
                        "reanchored_last_uid": anchor_uid,
                    },
                ),
            )
            return []

        uids = await _new_uids(client, cursor.last_uid)
        selected_uids = sorted(uids)[:max_messages]
        adapter = EmailInboundAdapter(
            account_id=str(account.id),
            self_address=contract.self_address,
            internal_domain_policy=contract.internal_domain_policy,
        )
        ingested: list[str] = []
        last_uid = cursor.last_uid
        for uid in selected_uids:
            await require_claim(claim)
            message_size = await client.fetch_message_size(uid)
            await require_claim(claim)
            if message_size > MAX_INBOUND_MESSAGE_BYTES:
                reservation = await _reserve_raw_event(
                    account=account,
                    claim=claim,
                    uid=uid,
                    uidvalidity=uidvalidity,
                    size=message_size,
                    sha256=None,
                )
                await _mark_raw_event(reservation.id, STATUS_IGNORED_TOO_LARGE, claim=claim)
                last_uid = uid
                continue

            raw = await client.fetch_message(uid)
            await require_claim(claim)
            if len(raw) > MAX_INBOUND_MESSAGE_BYTES:
                reservation = await _reserve_raw_event(
                    account=account,
                    claim=claim,
                    uid=uid,
                    uidvalidity=uidvalidity,
                    size=len(raw),
                    sha256=None,
                )
                await _mark_raw_event(reservation.id, STATUS_IGNORED_TOO_LARGE, claim=claim)
                last_uid = uid
                continue

            digest = await asyncio.to_thread(_sha256_hex, raw)
            reservation = await _reserve_raw_event(
                account=account,
                claim=claim,
                uid=uid,
                uidvalidity=uidvalidity,
                size=len(raw),
                sha256=digest,
            )
            events, disposition = await asyncio.to_thread(
                adapter.normalize_message,
                raw,
                uid=uid,
                uidvalidity=uidvalidity,
            )
            await require_claim(claim)
            if not events:
                await _mark_raw_event(reservation.id, disposition, claim=claim)
                last_uid = uid
                continue

            for event in events:
                if reservation.already_normalized or await _normalized_event_exists(
                    account=account,
                    raw_event_id=reservation.id,
                    external_event_id=event.external_event_id,
                ):
                    if not reservation.already_normalized:
                        await _mark_raw_event(
                            reservation.id,
                            "SKIPPED_DUPLICATE",
                            claim=claim,
                        )
                    continue
                await require_claim(claim)
                if await ingest_canonical_event(event, raw_event_id=reservation.id) is not None:
                    ingested.append(event.external_event_id)
            last_uid = uid

        await require_claim(claim)
        await _complete_checkpoint_or_raise(
            claim,
            cursor=EmailCursor(uidvalidity, last_uid).serialize(),
            bootstrapped=True,
            interval_seconds=poll_interval_seconds,
            page_count=1,
            occurrence_count=len(selected_uids),
        )
        return ingested
    finally:
        try:
            await client.aclose()
        except (ImapClientError, imaplib.IMAP4.error, OSError) as exc:
            code = _bounded_stable_error_code(
                getattr(exc, "code", None),
                fallback="IMAP_CLOSE_FAILED",
            )
            logger.warning(
                "email IMAP close failed account=%s code=%s error_type=%s",
                account.id,
                code,
                type(exc).__name__,
            )


def _account_poll_budget_seconds(
    *,
    network_timeout_seconds: float,
    max_messages: int,
) -> float:
    operation_budget = network_timeout_seconds * (3 + 2 * max_messages)
    return min(operation_budget, _MAX_ACCOUNT_POLL_SECONDS)


def _bounded_stable_error_code(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and _STABLE_ERROR_CODE.fullmatch(value):
        return value
    return fallback


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def _complete_checkpoint_or_raise(
    claim: ClaimedCheckpoint,
    **kwargs,
) -> None:
    if not await complete_checkpoint(claim, **kwargs):
        raise LeaseLostError(f"sync_lease_lost:{claim.id}")


def _account_contract(account: PlatformAccountRuntime) -> _EmailAccountContract:
    config = account.config or {}
    credentials = account.credential_bundle
    imap_host = _required_string(config, "imap_host")
    imap_port = config.get("imap_port")
    if type(imap_port) is not int or not 1 <= imap_port <= 65535:
        raise EmailPollAccountError("EMAIL_IMAP_PORT_INVALID")
    username = _account_text(
        credentials,
        "username",
        maximum=MAX_EMAIL_CREDENTIAL_CHARS,
    )
    password = _account_text(
        credentials,
        "password",
        maximum=MAX_EMAIL_CREDENTIAL_CHARS,
    )
    try:
        self_address = normalize_email_address(_required_string(config, "self_address"))
    except ValueError as exc:
        raise EmailPollAccountError("EMAIL_SELF_ADDRESS_INVALID") from exc
    mailbox = _account_text(
        config,
        "mailbox",
        maximum=MAX_EMAIL_MAILBOX_CHARS,
        default="INBOX",
    )
    internal_domain_policy = config.get("internal_domain_policy", "ignore")
    if internal_domain_policy not in {"allow", "ignore"}:
        raise EmailPollAccountError("EMAIL_INTERNAL_DOMAIN_POLICY_INVALID")
    return _EmailAccountContract(
        imap_host=imap_host,
        imap_port=imap_port,
        username=username,
        password=password,
        self_address=self_address,
        mailbox=mailbox,
        internal_domain_policy=internal_domain_policy,
    )


def _required_string(values: dict, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EmailPollAccountError(f"EMAIL_{key.upper()}_INVALID")
    return value.strip()


def _account_text(
    values: dict,
    key: str,
    *,
    maximum: int,
    default: str | None = None,
) -> str:
    value = values.get(key, default)
    try:
        return validate_email_account_text(value, maximum=maximum)
    except ValueError as exc:
        raise EmailPollAccountError(f"EMAIL_{key.upper()}_INVALID") from exc


async def _current_max_uid(client: _EmailImapClient) -> int:
    uids = await client.search_uids(start_uid=1)
    if any(type(uid) is not int or not 1 <= uid <= _MAX_IMAP_NUMBER for uid in uids):
        raise ImapClientError("imap_search_response_invalid")
    if len(set(uids)) != len(uids):
        raise ImapClientError("imap_search_response_invalid")
    return max(uids, default=0)


async def _new_uids(client: _EmailImapClient, last_uid: int) -> tuple[int, ...]:
    if last_uid >= _MAX_IMAP_NUMBER:
        return ()
    uids = await client.search_uids(start_uid=last_uid + 1)
    if any(type(uid) is not int or not last_uid < uid <= _MAX_IMAP_NUMBER for uid in uids):
        raise ImapClientError("imap_search_response_invalid")
    if len(set(uids)) != len(uids):
        raise ImapClientError("imap_search_response_invalid")
    return uids


async def _reserve_raw_event(
    *,
    account: PlatformAccountRuntime,
    claim: ClaimedCheckpoint,
    uid: int,
    uidvalidity: int,
    size: int,
    sha256: str | None,
) -> _RawReservation:
    external_event_id = f"{uidvalidity}:{uid}"
    evidence = {
        "uid": uid,
        "uidvalidity": uidvalidity,
        "size": size,
    }
    if sha256 is not None:
        evidence["sha256"] = sha256
    async with get_session_factory()() as session:
        await acquire_xact_lock(session, f"email-raw:{account.id}:{external_event_id}")
        await _lock_and_require_claim(session, claim)
        existing = (
            await session.execute(
                select(models.RawEvent)
                .where(
                    models.RawEvent.platform_account_id == account.id,
                    models.RawEvent.source == _SOURCE,
                    models.RawEvent.ingress_kind == "poll",
                    models.RawEvent.external_event_id == external_event_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.RawEvent(
                id=uuid.uuid4(),
                tenant_id=account.tenant_id,
                platform_account_id=account.id,
                source=_SOURCE,
                ingress_kind="poll",
                event_namespace=_EVENT_NAMESPACE,
                external_event_id=external_event_id,
                payload=evidence,
                headers={},
                context={},
                schema_version=1,
                processing_status="PENDING",
            )
            session.add(existing)
            already_normalized = False
        else:
            if existing.tenant_id != account.tenant_id:
                raise PermissionError("email_raw_event_tenant_mismatch")
            existing.payload = evidence
            existing.context = {}
            already_normalized = (
                await session.execute(
                    select(models.NormalizedEvent.id).where(
                        models.NormalizedEvent.raw_event_id == existing.id
                    )
                )
            ).scalar_one_or_none() is not None
        await session.commit()
        return _RawReservation(id=existing.id, already_normalized=already_normalized)


async def _normalized_event_exists(
    *,
    account: PlatformAccountRuntime,
    raw_event_id: uuid.UUID,
    external_event_id: str,
) -> bool:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(models.NormalizedEvent.id).where(
                    or_(
                        models.NormalizedEvent.raw_event_id == raw_event_id,
                        (
                            (models.NormalizedEvent.tenant_id == account.tenant_id)
                            & (models.NormalizedEvent.platform == "email")
                            & (models.NormalizedEvent.platform_account_id == account.id)
                            & (models.NormalizedEvent.external_event_id == external_event_id)
                        ),
                    )
                )
            )
        ).scalar_one_or_none() is not None


async def _lock_and_require_claim(
    session: AsyncSession,
    claim: ClaimedCheckpoint,
) -> None:
    database_now = await session.scalar(select(func.clock_timestamp()))
    checkpoint_id = await session.scalar(
        select(models.PlatformCheckpoint.id)
        .where(
            models.PlatformCheckpoint.id == claim.id,
            models.PlatformCheckpoint.claim_token == claim.claim_token,
            models.PlatformCheckpoint.revision == claim.revision,
            models.PlatformCheckpoint.claim_expires_at > database_now,
        )
        .with_for_update()
    )
    if checkpoint_id is None:
        raise LeaseLostError(f"sync_lease_lost:{claim.id}")


async def _mark_raw_event(
    raw_event_id: uuid.UUID,
    status: str,
    *,
    claim: ClaimedCheckpoint,
) -> None:
    async with get_session_factory()() as session:
        await _lock_and_require_claim(session, claim)
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(processing_status=status)
        )
        await session.commit()
