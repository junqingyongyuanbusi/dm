import logging
import uuid

from sqlalchemy import update

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


async def process_xchat_raw_event(raw_event_id: uuid.UUID, account_id: uuid.UUID) -> None:
    account = await get_platform_account_runtime(account_id)
    async with get_session_factory()() as session:
        raw = await session.get(models.RawEvent, raw_event_id)
        if raw is None:
            raise LookupError(f"xchat_raw_event_not_found:{raw_event_id}")
        payload = dict(raw.payload or {})
    data = payload.get("data") or {}
    encrypted = data.get("payload") or {}
    credentials = x_credentials(account)
    private_keys = credentials.get("xchat_private_keys_b64")
    if not private_keys:
        await _mark(raw_event_id, "XCHAT_PIN_REQUIRED")
        logger.error(
            "XCHAT_PIN_REQUIRED raw_event_id=%s account=%s event_uuid=%s",
            raw_event_id,
            account_id,
            data.get("event_uuid"),
        )
        return
    sender_id = str(encrypted.get("sender_id") or "")
    conversation_id = canonical_conversation_id(
        str(encrypted.get("conversation_id") or "")
    )
    if not sender_id or not encrypted.get("encoded_event"):
        await _mark(raw_event_id, "XCHAT_UNSUPPORTED_EVENT")
        return
    key_change = encrypted.get("conversation_key_change_event")
    if conversation_id and key_change:
        await save_conversation_key_events(account.id, conversation_id, [str(key_change)])
    if not conversation_id:
        await _mark(raw_event_id, "XCHAT_UNSUPPORTED_EVENT")
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
    finally:
        await client.aclose()
    try:
        decrypted = decrypt_live_event(
            private_keys_b64=private_keys,
            payload=encrypted,
            signing_keys=signing_keys,
        )
    except Exception:
        await _mark(raw_event_id, "XCHAT_DECRYPT_FAILED")
        logger.exception(
            "xchat webhook decrypt failed raw_event_id=%s account=%s event_uuid=%s",
            raw_event_id,
            account_id,
            data.get("event_uuid"),
        )
        raise

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
        await _mark(raw_event_id, "XCHAT_UNSUPPORTED_EVENT")
        return
    await ingest_canonical_event(canonical, raw_event_id=raw_event_id)


async def _mark(raw_event_id: uuid.UUID, status: str) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(processing_status=status)
        )
        await session.commit()
