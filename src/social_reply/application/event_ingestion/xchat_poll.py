"""XChat history poller.

Legacy ``/2/dm_events`` cannot see encrypted XChat messages. This poller uses
``/2/chat/conversations/{id}/events`` and Chat XDK decryption so encrypted DMs
are not silently lost when the legacy webhook path goes dark.
"""

import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update

from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.xchat.adapter import canonical_from_decrypted
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import decrypt_history, signing_key_entries
from social_reply.connectors.xchat.key_cache import (
    canonical_conversation_id,
    save_conversation_key_events,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.getenv("XCHAT_POLL_INTERVAL_SECONDS", "900"))
_MAX_CONVERSATION_PAGES = 3
_MAX_CONVERSATIONS_PER_POLL = int(os.getenv("XCHAT_MAX_CONVERSATIONS_PER_POLL", "10"))
_MAX_EVENT_PAGES = 3
_BACKFILL_REPLY_WINDOW = timedelta(hours=24)
_last_poll_at: float = 0.0


async def poll_xchat_messages() -> list[str]:
    global _last_poll_at
    now = time.monotonic()
    if now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        if not account.credential_bundle.get("xchat_private_keys_b64"):
            continue
        try:
            ingested.extend(await _poll_account(account))
        except httpx.HTTPStatusError as exc:
            logger.error(
                "xchat poll http error account=%s status=%s",
                account.id,
                exc.response.status_code,
            )
        except Exception:  # noqa: BLE001 - one account must not block the sweep
            logger.exception("xchat poll failed account=%s", account.id)
    return ingested


async def _poll_account(account) -> list[str]:
    self_id = account.external_account_id
    if not self_id:
        logger.warning("xchat account %s has no external_account_id", account.id)
        return []
    client = XChatClient(
        consumer_key=account.credential_bundle["consumer_key"],
        consumer_secret=account.credential_bundle["consumer_secret"],
        access_token=account.credential_bundle["access_token"],
        access_token_secret=account.credential_bundle["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        conversations = await _read_conversations(client)
        conversations.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        ingested: list[str] = []
        for conversation in conversations[:_MAX_CONVERSATIONS_PER_POLL]:
            conversation_id = canonical_conversation_id(
                str(conversation.get("id") or "")
            )
            if not conversation_id:
                continue
            participant_ids = [str(value) for value in conversation.get("participant_ids") or []]
            if conversation.get("type") != "direct" or len(participant_ids) != 1:
                logger.info(
                    "xchat poll skips non-direct conversation account=%s conversation=%s type=%s",
                    account.id,
                    conversation_id,
                    conversation.get("type"),
                )
                continue
            ingested.extend(
                await _poll_conversation(
                    client=client,
                    account=account,
                    conversation_id=conversation_id,
                    peer_id=participant_ids[0],
                )
            )
        return ingested
    finally:
        await client.aclose()


async def _read_conversations(client: XChatClient) -> list[dict]:
    token: str | None = None
    conversations: list[dict] = []
    seen_tokens: set[str] = set()
    for _ in range(_MAX_CONVERSATION_PAGES):
        page, token = await client.read_conversations(
            max_results=100,
            pagination_token=token,
        )
        conversations.extend(page)
        if not token:
            return conversations
        if token in seen_tokens:
            raise RuntimeError("xchat_conversation_pagination_token_repeated")
        seen_tokens.add(token)
    logger.warning("xchat conversation page budget exhausted")
    return conversations


async def _poll_conversation(
    *,
    client: XChatClient,
    account,
    conversation_id: str,
    peer_id: str,
) -> list[str]:
    cursor = ((account.config or {}).get("xchat_cursors") or {}).get(conversation_id)
    bootstrapped = bool(
        ((account.config or {}).get("xchat_bootstrapped") or {}).get(conversation_id)
    )
    envelopes, key_changes, complete = await _read_until_cursor(
        client, conversation_id, cursor
    )
    newest_id = _max_event_id(envelopes)
    if not envelopes:
        if not bootstrapped:
            await _save_cursor(account.id, conversation_id, newest_id, bootstrapped=True)
        return []

    senders = {
        str(envelope.get("sender_id"))
        for envelope in envelopes
        if envelope.get("sender_id") is not None
    }
    signing_keys: list[dict] = []
    for sender_id in senders:
        signing_keys.extend(
            signing_key_entries(sender_id, await client.get_user_public_keys(sender_id))
        )
    decrypted, _keys, errors = decrypt_history(
        private_keys_b64=account.credential_bundle["xchat_private_keys_b64"],
        message_events=envelopes,
        key_change_events=key_changes,
        signing_keys=signing_keys,
    )
    if key_changes:
        await save_conversation_key_events(account.id, conversation_id, key_changes)
    if errors:
        logger.error(
            "xchat decrypt errors account=%s conversation=%s count=%d indexes=%s",
            account.id,
            conversation_id,
            len(errors),
            sorted(errors),
        )

    # Existing conversations may already contain encrypted messages that legacy
    # /2/dm_events never exposed. On first unlock, reply only to the newest recent
    # inbound message per conversation; replying to every historical turn would create
    # a burst of stale automated messages. Future polls process every new event normally.
    ordered = sorted(
        decrypted,
        key=lambda value: _as_int((value.get("envelope") or {}).get("id")) or 0,
    )
    if not bootstrapped:
        # If the newest history event is ours, the conversation has already been
        # answered in XChat and no historical inbound should trigger another reply.
        newest_sender = (
            str((ordered[-1].get("envelope") or {}).get("sender_id")) if ordered else ""
        )
        if newest_sender == str(account.external_account_id):
            ordered = []
        else:
            recent_inbound = [
                item
                for item in ordered
                if str((item.get("envelope") or {}).get("sender_id"))
                != str(account.external_account_id)
                and _within_backfill_reply_window(item.get("envelope") or {})
            ]
            ordered = recent_inbound[-1:]
    ingested: list[str] = []
    for item in ordered:
        envelope = item["envelope"]
        canonical = canonical_from_decrypted(
            account_id=str(account.id),
            external_account_id=str(account.external_account_id),
            envelope=envelope,
            event=item["event"],
        )
        if canonical is not None and await ingest_canonical_event(canonical) is not None:
            ingested.append(canonical.external_event_id)

    # Only advance when every encrypted message in the fetched range decrypted. A
    # missing key or signature record must be retried rather than skipped forever.
    if not errors and complete:
        await _save_cursor(
            account.id,
            conversation_id,
            newest_id,
            bootstrapped=True if not bootstrapped else None,
        )
    return ingested


async def _read_until_cursor(
    client: XChatClient,
    conversation_id: str,
    cursor: str | None,
) -> tuple[list[dict], list[str], bool]:
    token: str | None = None
    seen_tokens: set[str] = set()
    messages: list[dict] = []
    key_changes: list[str] = []
    cursor_num = _as_int(cursor)
    for _ in range(_MAX_EVENT_PAGES):
        page, page_keys, token = await client.read_conversation_events(
            conversation_id,
            pagination_token=token,
        )
        key_changes.extend(page_keys)
        for event in page:
            event_num = _as_int(event.get("id"))
            if cursor_num is None or event_num is None or event_num > cursor_num:
                messages.append(event)
        numeric = [_as_int(event.get("id")) for event in page]
        comparable = [value for value in numeric if value is not None]
        if not token or (cursor_num is not None and comparable and min(comparable) <= cursor_num):
            return messages, _dedupe(key_changes), True
        if token in seen_tokens:
            raise RuntimeError("xchat_event_pagination_token_repeated")
        seen_tokens.add(token)
    logger.warning("xchat event page budget exhausted conversation=%s", conversation_id)
    return messages, _dedupe(key_changes), False


def _within_backfill_reply_window(envelope: dict) -> bool:
    value = envelope.get("created_at")
    if isinstance(value, str):
        try:
            occurred_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            occurred_at = None
    else:
        occurred_at = None
    if occurred_at is None and envelope.get("created_at_msec") is not None:
        try:
            occurred_at = datetime.fromtimestamp(
                int(envelope["created_at_msec"]) / 1000,
                tz=UTC,
            )
        except (TypeError, ValueError, OSError):
            occurred_at = None
    return occurred_at is not None and occurred_at >= datetime.now(UTC) - _BACKFILL_REPLY_WINDOW


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _max_event_id(events: list[dict]) -> str | None:
    values = [_as_int(event.get("id")) for event in events]
    numeric = [value for value in values if value is not None]
    return str(max(numeric)) if numeric else None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _save_cursor(
    account_id,
    conversation_id: str,
    newest_id: str | None,
    *,
    bootstrapped: bool | None = None,
) -> None:
    async with get_session_factory()() as session:
        row = await session.get(models.PlatformAccount, account_id)
        if row is None:
            return
        config = dict(row.config or {})
        cursors = dict(config.get("xchat_cursors") or {})
        bootstraps = dict(config.get("xchat_bootstrapped") or {})
        if newest_id is not None:
            cursors[conversation_id] = newest_id
        if bootstrapped is not None:
            bootstraps[conversation_id] = bootstrapped
        config["xchat_cursors"] = cursors
        config["xchat_bootstrapped"] = bootstraps
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(config=config)
        )
        await session.commit()
