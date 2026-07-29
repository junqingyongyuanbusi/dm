import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.platform_accounts import get_platform_account_runtime
from social_reply.connectors.xchat.adapter import canonical_from_decrypted
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import decrypt_live_event, signing_key_entries
from social_reply.connectors.xchat.key_cache import (
    canonical_conversation_id,
    save_conversation_key_events,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_CLAIMABLE_STATUSES = (
    "XCHAT_DECRYPTION_PENDING",
    "XCHAT_PIN_REQUIRED",
    "XCHAT_KEY_RECOVERY_REQUIRED",
    "XCHAT_RETRYABLE_ERROR",
)
_CLAIM_LEASE = timedelta(minutes=5)
_MAX_RETRY_ATTEMPTS = 8


@dataclass(frozen=True)
class XChatClaim:
    payload: dict
    token: str


async def process_xchat_raw_event(raw_event_id: uuid.UUID, account_id: uuid.UUID) -> None:
    account = await get_platform_account_runtime(account_id)
    claim = await _claim(raw_event_id, account_id, account.tenant_id)
    if claim is None:
        return
    data = claim.payload.get("data") or {}
    encrypted = data.get("payload") or {}
    credentials = x_credentials(account)
    private_keys = credentials.get("xchat_private_keys_b64")
    if not private_keys:
        await _mark(raw_event_id, claim.token, "XCHAT_KEY_RECOVERY_REQUIRED")
        logger.error(
            "XCHAT_KEY_RECOVERY_REQUIRED raw_event_id=%s account=%s event_uuid=%s",
            raw_event_id,
            account_id,
            data.get("event_uuid"),
        )
        return
    sender_id = str(encrypted.get("sender_id") or "")
    conversation_id = canonical_conversation_id(str(encrypted.get("conversation_id") or ""))
    if not sender_id or not encrypted.get("encoded_event"):
        await _mark(raw_event_id, claim.token, "XCHAT_UNSUPPORTED_EVENT")
        return
    key_change = encrypted.get("conversation_key_change_event")
    if conversation_id and key_change:
        await save_conversation_key_events(account.id, conversation_id, [str(key_change)])
    if not conversation_id:
        await _mark(raw_event_id, claim.token, "XCHAT_UNSUPPORTED_EVENT")
        return

    client = XChatClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        signing_keys = signing_key_entries(
            sender_id,
            await client.get_user_public_keys(sender_id),
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404 or status == 429 or status >= 500:
            await _mark_retryable(
                raw_event_id,
                claim.token,
                f"XCHAT_PUBLIC_KEY_HTTP_{status}",
            )
            return
        permanent_status = (
            "XCHAT_REAUTHORIZATION_REQUIRED"
            if status == 401
            else "XCHAT_ACCESS_FORBIDDEN"
            if status == 403
            else f"XCHAT_PUBLIC_KEY_HTTP_{status}"
        )
        await _mark(
            raw_event_id,
            claim.token,
            permanent_status,
            error_code=permanent_status,
        )
        logger.error(
            "xchat sender public-key lookup rejected raw_event_id=%s status=%s",
            raw_event_id,
            status,
        )
        return
    except httpx.TransportError:
        await _mark_retryable(
            raw_event_id,
            claim.token,
            "XCHAT_PUBLIC_KEY_TRANSPORT_ERROR",
        )
        return
    except Exception:
        await _mark(raw_event_id, claim.token, "XCHAT_PUBLIC_KEY_LOOKUP_FAILED")
        logger.exception("xchat sender public-key lookup failed raw_event_id=%s", raw_event_id)
        return
    finally:
        await client.aclose()
    if not signing_keys:
        await _mark_retryable(
            raw_event_id,
            claim.token,
            "XCHAT_SIGNING_KEYS_UNAVAILABLE",
        )
        return
    try:
        decrypted = decrypt_live_event(
            private_keys_b64=private_keys,
            payload=encrypted,
            signing_keys=signing_keys,
        )
    except Exception:
        await _mark(
            raw_event_id,
            claim.token,
            "XCHAT_DECRYPT_FAILED",
            error_code="XCHAT_DECRYPT_FAILED",
        )
        logger.exception(
            "xchat webhook decrypt failed raw_event_id=%s account=%s event_uuid=%s",
            raw_event_id,
            account_id,
            data.get("event_uuid"),
        )
        return

    envelope = {
        **encrypted,
        "id": decrypted.get("message_id") or decrypted.get("id") or data.get("event_uuid"),
        "created_at": data.get("created_at") or encrypted.get("created_at"),
    }
    canonical = canonical_from_decrypted(
        account_id=str(account.id),
        external_account_id=str(account.external_account_id),
        envelope=envelope,
        event=decrypted,
    )
    if canonical is None:
        await _mark(raw_event_id, claim.token, "XCHAT_UNSUPPORTED_EVENT")
        return
    await ingest_canonical_event(
        canonical,
        raw_event_id=raw_event_id,
        raw_event_claim_token=claim.token,
    )


async def _claim(
    raw_event_id: uuid.UUID,
    account_id: uuid.UUID,
    tenant_id: str,
) -> XChatClaim | None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.source != "x"
            or row.platform_account_id != account_id
            or row.tenant_id != tenant_id
        ):
            await session.rollback()
            return None
        if row.processing_status == "XCHAT_PROCESSING":
            if row.processing_claim_expires_at and row.processing_claim_expires_at > now:
                await session.rollback()
                return None
        elif row.processing_status not in _CLAIMABLE_STATUSES:
            await session.rollback()
            return None
        claim_token = uuid.uuid4()
        row.processing_status = "XCHAT_PROCESSING"
        row.processing_claim_token = claim_token
        row.processing_claim_expires_at = now + _CLAIM_LEASE
        row.processing_attempt_count = int(row.processing_attempt_count or 0) + 1
        row.processing_next_attempt_at = None
        row.processing_error_code = None
        row.processed_at = None
        payload = dict(row.payload or {})
        await session.commit()
        return XChatClaim(payload=payload, token=str(claim_token))


async def _mark_retryable(
    raw_event_id: uuid.UUID,
    claim_token: str,
    error_code: str,
) -> None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or str(row.processing_claim_token or "") != claim_token:
            await session.rollback()
            return
        attempts = int(row.processing_attempt_count or 0)
        row.processing_claim_token = None
        row.processing_claim_expires_at = None
        row.processing_error_code = error_code
        if attempts >= _MAX_RETRY_ATTEMPTS:
            row.processing_status = "XCHAT_RETRY_EXHAUSTED"
            row.processing_next_attempt_at = None
            row.processed_at = now
        else:
            delay_seconds = min(3600, 30 * (2 ** max(0, attempts - 1)))
            row.processing_status = "XCHAT_RETRYABLE_ERROR"
            row.processing_next_attempt_at = now + timedelta(seconds=delay_seconds)
            row.processed_at = None
        await session.commit()


async def _mark(
    raw_event_id: uuid.UUID,
    claim_token: str,
    status: str,
    *,
    error_code: str | None = None,
) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or str(row.processing_claim_token or "") != claim_token:
            await session.rollback()
            return
        row.processing_claim_token = None
        row.processing_claim_expires_at = None
        row.processing_next_attempt_at = None
        row.processing_error_code = error_code
        row.processing_status = status
        row.processed_at = (
            None
            if status in {"XCHAT_KEY_RECOVERY_REQUIRED", "XCHAT_PIN_REQUIRED"}
            else datetime.now(UTC)
        )
        await session.commit()
