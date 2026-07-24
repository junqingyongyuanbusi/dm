"""XChat history poller.

Legacy ``/2/dm_events`` cannot see encrypted XChat messages. This poller uses
``/2/chat/conversations/{id}/events`` and Chat XDK decryption so encrypted DMs
are not silently lost when the legacy webhook path goes dark.
"""

import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.event_ingestion.poll_raw import (
    PollOccurrence,
    append_poll_occurrences,
    mark_poll_occurrences,
)
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.xchat.adapter import canonical_from_decrypted
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import decrypt_history, signing_key_entries
from social_reply.connectors.xchat.key_cache import (
    canonical_conversation_id,
    save_conversation_key_events,
)
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.getenv("XCHAT_POLL_INTERVAL_SECONDS", "900"))
_MAX_CONVERSATION_PAGES = 3
_MAX_CONVERSATIONS_PER_POLL = int(os.getenv("XCHAT_MAX_CONVERSATIONS_PER_POLL", "10"))
_MAX_EVENT_PAGES = 3
_BACKFILL_REPLY_WINDOW = timedelta(hours=24)
_last_poll_at: float | None = None


@dataclass(frozen=True)
class _PolledEnvelope:
    payload: dict
    raw_event_id: uuid.UUID


async def poll_xchat_messages() -> list[str]:
    global _last_poll_at
    now = time.monotonic()
    if _last_poll_at is not None and now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        if not capability_enabled(account.capability or {}, CapabilityKey.X_CHAT):
            continue
        if not x_credentials(account).get("xchat_private_keys_b64"):
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
    credentials = x_credentials(account)
    client = XChatClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        conversations = await _read_conversations(client)
        conversations.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        ingested: list[str] = []
        for conversation in conversations[:_MAX_CONVERSATIONS_PER_POLL]:
            conversation_id = canonical_conversation_id(str(conversation.get("id") or ""))
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
                    conversation=conversation,
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
    conversation: dict,
    conversation_id: str,
    peer_id: str,
) -> list[str]:
    credentials = x_credentials(account)
    cursor = ((account.config or {}).get("xchat_cursors") or {}).get(conversation_id)
    bootstrapped = bool(
        ((account.config or {}).get("xchat_bootstrapped") or {}).get(conversation_id)
    )
    envelopes, key_changes, key_raw_ids, complete = await _read_until_cursor(
        client,
        account=account,
        conversation=conversation,
        conversation_id=conversation_id,
        peer_id=peer_id,
        cursor=cursor,
        poll_run_id=uuid.uuid4(),
    )
    newest_id = _max_event_id(envelopes)
    if key_changes:
        await save_conversation_key_events(account.id, conversation_id, key_changes)
        await mark_poll_occurrences(key_raw_ids, "PROCESSED_KEY_MATERIAL")
    if not envelopes:
        if not bootstrapped:
            await _save_cursor(account.id, conversation_id, newest_id, bootstrapped=True)
        return []

    payloads = [occurrence.payload for occurrence in envelopes]
    senders = {
        str(envelope.get("sender_id"))
        for envelope in payloads
        if envelope.get("sender_id") is not None
    }
    signing_keys: list[dict] = []
    for sender_id in senders:
        signing_keys.extend(
            signing_key_entries(sender_id, await client.get_user_public_keys(sender_id))
        )
    decrypted, _keys, errors = decrypt_history(
        private_keys_b64=credentials["xchat_private_keys_b64"],
        message_events=payloads,
        key_change_events=key_changes,
        signing_keys=signing_keys,
    )
    error_indexes = {
        index
        for value in errors
        if (index := _as_int(value)) is not None and 0 <= index < len(envelopes)
    }
    if error_indexes:
        await mark_poll_occurrences(
            [envelopes[index].raw_event_id for index in sorted(error_indexes)],
            "XCHAT_DECRYPT_FAILED",
        )
    if errors:
        logger.error(
            "xchat decrypt errors account=%s conversation=%s count=%d indexes=%s",
            account.id,
            conversation_id,
            len(errors),
            sorted(errors),
        )

    raw_by_envelope_id: dict[str, list[uuid.UUID]] = defaultdict(list)
    for occurrence in envelopes:
        raw_by_envelope_id[str(occurrence.payload.get("id") or "")].append(occurrence.raw_event_id)
    decrypted_with_raw: list[tuple[dict, uuid.UUID]] = []
    for item in decrypted:
        envelope_id = str((item.get("envelope") or {}).get("id") or "")
        candidates = raw_by_envelope_id.get(envelope_id) or []
        if candidates:
            decrypted_with_raw.append((item, candidates.pop(0)))
    matched_raw_ids = {raw_event_id for _item, raw_event_id in decrypted_with_raw}
    expected_success_ids = {
        occurrence.raw_event_id
        for index, occurrence in enumerate(envelopes)
        if index not in error_indexes
    }
    missing_output_ids = sorted(expected_success_ids - matched_raw_ids, key=str)
    if missing_output_ids:
        await mark_poll_occurrences(missing_output_ids, "XCHAT_DECRYPT_MISSING_OUTPUT")

    ordered = sorted(
        decrypted_with_raw,
        key=lambda value: _as_int((value[0].get("envelope") or {}).get("id")) or 0,
    )
    selected = ordered
    if not bootstrapped:
        newest_sender = (
            str((ordered[-1][0].get("envelope") or {}).get("sender_id")) if ordered else ""
        )
        if newest_sender == str(account.external_account_id):
            selected = []
        else:
            recent_inbound = [
                item
                for item in ordered
                if str((item[0].get("envelope") or {}).get("sender_id"))
                != str(account.external_account_id)
                and _within_backfill_reply_window(item[0].get("envelope") or {})
            ]
            selected = recent_inbound[-1:]
        selected_ids = {raw_event_id for _item, raw_event_id in selected}
        await mark_poll_occurrences(
            [raw_event_id for _item, raw_event_id in ordered if raw_event_id not in selected_ids],
            "IGNORED_BOOTSTRAP",
        )

    ingested: list[str] = []
    for item, raw_event_id in selected:
        envelope = item["envelope"]
        canonical = canonical_from_decrypted(
            account_id=str(account.id),
            external_account_id=str(account.external_account_id),
            envelope=envelope,
            event=item["event"],
        )
        if canonical is None:
            sender_id = str(envelope.get("sender_id") or "")
            status = (
                "IGNORED_SELF"
                if sender_id == str(account.external_account_id)
                else "IGNORED_UNSUPPORTED"
            )
            await mark_poll_occurrences([raw_event_id], status)
            continue
        if await ingest_canonical_event(canonical, raw_event_id=raw_event_id) is not None:
            ingested.append(canonical.external_event_id)

    all_decrypted = not errors and not missing_output_ids and len(decrypted) == len(envelopes)
    if all_decrypted and complete:
        await _save_cursor(
            account.id,
            conversation_id,
            newest_id,
            bootstrapped=True if not bootstrapped else None,
        )
    return ingested


async def _read_until_cursor(
    client: XChatClient,
    *,
    account,
    conversation: dict,
    conversation_id: str,
    peer_id: str,
    cursor: str | None,
    poll_run_id: uuid.UUID,
) -> tuple[list[_PolledEnvelope], list[str], list[uuid.UUID], bool]:
    token: str | None = None
    seen_tokens: set[str] = set()
    messages: list[_PolledEnvelope] = []
    key_changes: list[str] = []
    key_raw_ids: list[uuid.UUID] = []
    cursor_num = _as_int(cursor)
    for page_index in range(_MAX_EVENT_PAGES):
        request_token = token
        page, page_keys, token = await client.read_conversation_events(
            conversation_id,
            pagination_token=request_token,
        )
        common_context = {
            "poll_run_id": str(poll_run_id),
            "page_index": page_index,
            "pagination_token": request_token,
            "next_token": token,
            "cursor_before": cursor,
            "conversation": {
                "id": conversation_id,
                "peer_id": peer_id,
                "participant_ids": list(conversation.get("participant_ids") or []),
                "type": conversation.get("type"),
                "updated_at": conversation.get("updated_at"),
            },
        }
        occurrences: list[PollOccurrence] = []
        relevant: list[bool] = []
        for item_index, event in enumerate(page):
            event_num = _as_int(event.get("id"))
            is_relevant = cursor_num is None or event_num is None or event_num > cursor_num
            relevant.append(is_relevant)
            occurrences.append(
                PollOccurrence(
                    payload=dict(event),
                    external_event_id=str(event.get("id") or "") or None,
                    external_conversation_id=conversation_id,
                    occurred_at=_occurred_at(event),
                    processing_status="PENDING" if is_relevant else "IGNORED_BEFORE_CURSOR",
                    context={**common_context, "item_index": item_index},
                )
            )
        raw_ids = await append_poll_occurrences(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            source="xchat_poll",
            event_namespace="x.xchat.message",
            occurrences=occurrences,
        )
        messages.extend(
            _PolledEnvelope(payload=event, raw_event_id=raw_event_id)
            for event, raw_event_id, is_relevant in zip(page, raw_ids, relevant, strict=True)
            if is_relevant
        )
        key_occurrences = [
            PollOccurrence(
                payload={"key_change": value},
                external_conversation_id=conversation_id,
                context={**common_context, "item_index": item_index},
            )
            for item_index, value in enumerate(page_keys)
        ]
        key_raw_ids.extend(
            await append_poll_occurrences(
                tenant_id=account.tenant_id,
                platform_account_id=account.id,
                source="xchat_poll",
                event_namespace="x.xchat.key_change",
                occurrences=key_occurrences,
            )
        )
        key_changes.extend(page_keys)
        numeric = [_as_int(event.get("id")) for event in page]
        comparable = [value for value in numeric if value is not None]
        if not token or (cursor_num is not None and comparable and min(comparable) <= cursor_num):
            return messages, _dedupe(key_changes), key_raw_ids, True
        if token in seen_tokens:
            raise RuntimeError("xchat_event_pagination_token_repeated")
        seen_tokens.add(token)
    logger.warning("xchat event page budget exhausted conversation=%s", conversation_id)
    return messages, _dedupe(key_changes), key_raw_ids, False


def _within_backfill_reply_window(envelope: dict) -> bool:
    occurred_at = _occurred_at(envelope)
    return occurred_at is not None and occurred_at >= datetime.now(UTC) - _BACKFILL_REPLY_WINDOW


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _occurred_at(envelope: dict) -> datetime | None:
    value = envelope.get("created_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if envelope.get("created_at_msec") is not None:
        try:
            return datetime.fromtimestamp(int(envelope["created_at_msec"]) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            return None
    return None


def _max_event_id(events: list[_PolledEnvelope]) -> str | None:
    values = [_as_int(event.payload.get("id")) for event in events]
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
