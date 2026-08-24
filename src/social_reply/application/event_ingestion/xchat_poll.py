"""Durable XChat polling with per-conversation checkpoints and recoverable gaps."""

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from social_reply.application.account_management.x_credentials import x_credentials
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.event_ingestion.poll_raw import (
    PollOccurrence,
    append_poll_occurrences,
    mark_poll_occurrences,
)
from social_reply.application.event_ingestion.poll_sync import (
    CheckpointStream,
    ClaimedCheckpoint,
    GapSpec,
    GapType,
    claim_checkpoint,
    complete_checkpoint,
    ensure_checkpoint,
    fail_run,
    record_gap,
    require_claim,
)
from social_reply.application.platform_accounts import list_active_accounts_by_platform
from social_reply.connectors.xchat.adapter import canonical_from_decrypted
from social_reply.connectors.xchat.client import XChatClient
from social_reply.connectors.xchat.crypto import decrypt_history, signing_key_entries
from social_reply.connectors.xchat.key_cache import (
    canonical_conversation_id,
    conversation_key_events,
    save_conversation_key_events,
)
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

_MAX_CONVERSATION_PAGES = 3
_MAX_EVENT_PAGES = 3
_BACKFILL_REPLY_WINDOW = timedelta(hours=24)
_last_poll_at: float | None = None
_DEFAULT_OWNER = f"xchat:{uuid.uuid4()}"


@dataclass(frozen=True)
class _PolledEnvelope:
    payload: dict
    raw_event_id: uuid.UUID


@dataclass(frozen=True)
class _ConversationRead:
    conversations: list[dict]
    page_count: int
    gap: GapSpec | None = None


@dataclass(frozen=True)
class _EventRead:
    envelopes: list[_PolledEnvelope]
    key_changes: list[str]
    key_raw_ids: list[uuid.UUID]
    page_count: int
    occurrence_count: int
    candidate_cursor: str | None
    gap: GapSpec | None = None


async def poll_xchat_messages(*, scheduler_owner: str | None = None) -> list[str]:
    global _last_poll_at
    settings = get_settings()
    poll_interval_seconds = settings.xchat_poll_interval_seconds
    max_conversations = settings.xchat_max_conversations_per_poll
    now = time.monotonic()
    if _last_poll_at is not None and now - _last_poll_at < poll_interval_seconds:
        return []
    _last_poll_at = now

    owner = scheduler_owner or _DEFAULT_OWNER
    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        if not capability_enabled(account.capability or {}, CapabilityKey.X_CHAT):
            continue
        if not x_credentials(account).get("xchat_private_keys_b64"):
            continue
        checkpoint = await ensure_checkpoint(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            stream=CheckpointStream.XCHAT_DISCOVERY,
        )
        claim = await claim_checkpoint(checkpoint.id, owner=owner)
        if claim is None:
            continue
        try:
            ingested.extend(
                await _poll_account(
                    account,
                    claim=claim,
                    owner=owner,
                    poll_interval_seconds=poll_interval_seconds,
                    max_conversations=max_conversations,
                )
            )
        except httpx.HTTPStatusError as exc:
            await fail_run(
                claim,
                error_code=f"XCHAT_HTTP_{exc.response.status_code}",
                error_message=str(exc),
                retry_after_seconds=poll_interval_seconds,
            )
            logger.error(
                "xchat poll http error account=%s status=%s",
                account.id,
                exc.response.status_code,
            )
        except Exception as exc:  # noqa: BLE001 - one account must not block the sweep
            await fail_run(
                claim,
                error_code="XCHAT_DISCOVERY_FAILED",
                error_message=str(exc),
                retry_after_seconds=poll_interval_seconds,
            )
            logger.exception("xchat poll failed account=%s", account.id)
    return ingested


async def _poll_account(
    account,
    *,
    claim: ClaimedCheckpoint,
    owner: str,
    poll_interval_seconds: int,
    max_conversations: int,
) -> list[str]:
    if not account.external_account_id:
        await fail_run(
            claim,
            error_code="XCHAT_ACCOUNT_ID_MISSING",
            error_message="platform account has no external_account_id",
            retry_after_seconds=poll_interval_seconds,
        )
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
        discovery = await _read_conversations(
            client,
            claim=claim,
            max_conversations=max_conversations,
        )
        discovery.conversations.sort(
            key=lambda item: str(item.get("updated_at") or ""), reverse=True
        )
        ingested: list[str] = []
        for conversation in discovery.conversations:
            await require_claim(claim)
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
                    owner=owner,
                    poll_interval_seconds=poll_interval_seconds,
                )
            )
            await require_claim(claim)
        if discovery.gap is not None:
            await record_gap(
                claim,
                discovery.gap,
                retry_after_seconds=poll_interval_seconds,
                page_count=discovery.page_count,
                occurrence_count=0,
            )
        else:
            await complete_checkpoint(
                claim,
                cursor=None,
                bootstrapped=True,
                interval_seconds=poll_interval_seconds,
                page_count=discovery.page_count,
                occurrence_count=0,
            )
        return ingested
    finally:
        await client.aclose()


async def _read_conversations(
    client: XChatClient,
    *,
    claim: ClaimedCheckpoint,
    max_conversations: int,
) -> _ConversationRead:
    token = claim.active_gap.resume_token if claim.active_gap else None
    conversations: list[dict] = []
    seen_tokens: set[str] = set()
    work_budget = max(1, max_conversations)
    for page_index in range(_MAX_CONVERSATION_PAGES):
        request_token = token
        remaining = max(work_budget - len(conversations), 1)
        await require_claim(claim)
        try:
            page, token = await client.read_conversations(
                max_results=min(remaining, 100),
                pagination_token=request_token,
            )
        except Exception as exc:
            resume_failed = page_index == 0 and request_token is not None
            if page_index == 0 and not resume_failed:
                raise
            return _ConversationRead(
                conversations=conversations,
                page_count=page_index,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    resume_token=None if resume_failed else request_token,
                    detail={
                        "error": type(exc).__name__,
                        "page_index": page_index,
                        "restart_from_checkpoint": resume_failed,
                    },
                ),
            )
        await require_claim(claim)
        conversations.extend(page)
        page_count = page_index + 1
        if not token:
            return _ConversationRead(conversations=conversations, page_count=page_count)
        if token in seen_tokens:
            return _ConversationRead(
                conversations=conversations,
                page_count=page_count,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    detail={"error": "PAGINATION_TOKEN_REPEATED", "page_index": page_index},
                ),
            )
        seen_tokens.add(token)
        if len(conversations) >= work_budget:
            return _ConversationRead(
                conversations=conversations,
                page_count=page_count,
                gap=GapSpec(
                    gap_type=GapType.PAGE_CAP,
                    resume_token=token,
                    detail={"work_budget": work_budget, "page_count": page_count},
                ),
            )
    logger.warning("xchat conversation page budget exhausted")
    return _ConversationRead(
        conversations=conversations,
        page_count=_MAX_CONVERSATION_PAGES,
        gap=GapSpec(
            gap_type=GapType.PAGE_CAP,
            resume_token=token,
            detail={"page_count": _MAX_CONVERSATION_PAGES},
        ),
    )


async def _poll_conversation(
    *,
    client: XChatClient,
    account,
    conversation: dict,
    conversation_id: str,
    peer_id: str,
    owner: str,
    poll_interval_seconds: int,
) -> list[str]:
    config = account.config or {}
    initial_cursor = (config.get("xchat_cursors") or {}).get(conversation_id)
    initial_bootstrapped = bool((config.get("xchat_bootstrapped") or {}).get(conversation_id))
    checkpoint = await ensure_checkpoint(
        tenant_id=account.tenant_id,
        platform_account_id=account.id,
        stream=CheckpointStream.XCHAT_CONVERSATION,
        scope_key=conversation_id,
        initial_cursor=str(initial_cursor) if initial_cursor is not None else None,
        initial_bootstrapped=initial_bootstrapped,
    )
    claim = await claim_checkpoint(checkpoint.id, owner=owner)
    if claim is None:
        return []
    try:
        return await _process_conversation(
            client=client,
            account=account,
            conversation=conversation,
            conversation_id=conversation_id,
            peer_id=peer_id,
            claim=claim,
            poll_interval_seconds=poll_interval_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - one conversation must not block discovery
        await fail_run(
            claim,
            error_code="XCHAT_CONVERSATION_FAILED",
            error_message=str(exc),
            retry_after_seconds=poll_interval_seconds,
        )
        logger.exception(
            "xchat conversation poll failed account=%s conversation=%s",
            account.id,
            conversation_id,
        )
        return []


async def _process_conversation(
    *,
    client: XChatClient,
    account,
    conversation: dict,
    conversation_id: str,
    peer_id: str,
    claim: ClaimedCheckpoint,
    poll_interval_seconds: int,
) -> list[str]:
    credentials = x_credentials(account)
    result = await _read_until_cursor(
        client,
        account=account,
        conversation=conversation,
        conversation_id=conversation_id,
        peer_id=peer_id,
        claim=claim,
    )
    await require_claim(claim)
    if result.key_changes:
        await save_conversation_key_events(account.id, conversation_id, result.key_changes)
        await mark_poll_occurrences(result.key_raw_ids, "PROCESSED_KEY_MATERIAL")

    payloads = [occurrence.payload for occurrence in result.envelopes]
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
    cached_keys = conversation_key_events(account.config or {}, conversation_id)
    key_changes = _dedupe([*cached_keys, *result.key_changes])
    decrypted, _keys, errors = decrypt_history(
        private_keys_b64=credentials["xchat_private_keys_b64"],
        message_events=payloads,
        key_change_events=key_changes,
        signing_keys=signing_keys,
    )
    await require_claim(claim)
    error_indexes = {
        index
        for value in errors
        if (index := _as_int(value)) is not None and 0 <= index < len(result.envelopes)
    }
    if error_indexes:
        await mark_poll_occurrences(
            [result.envelopes[index].raw_event_id for index in sorted(error_indexes)],
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
    for occurrence in result.envelopes:
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
        for index, occurrence in enumerate(result.envelopes)
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
    incomplete_bootstrap = not claim.bootstrapped and (
        claim.mode == "BACKFILL"
        or result.gap is not None
        or bool(errors)
        or bool(missing_output_ids)
    )
    if not claim.bootstrapped:
        if incomplete_bootstrap:
            selected = []
        else:
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
        await require_claim(claim)
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

    all_decrypted = (
        not errors and not missing_output_ids and len(decrypted) == len(result.envelopes)
    )
    await require_claim(claim)
    if not all_decrypted:
        missing_output_set = set(missing_output_ids)
        failed_ids = [
            str(occurrence.payload.get("id") or "")
            for index, occurrence in enumerate(result.envelopes)
            if occurrence.raw_event_id in missing_output_set or index in error_indexes
        ]
        await record_gap(
            claim,
            GapSpec(
                gap_type=GapType.DECRYPT_ERROR,
                candidate_cursor=result.candidate_cursor,
                detail={
                    "conversation_id": conversation_id,
                    "failed_envelope_ids": failed_ids,
                    "error_indexes": sorted(error_indexes),
                },
            ),
            retry_after_seconds=poll_interval_seconds,
            page_count=result.page_count,
            occurrence_count=result.occurrence_count,
        )
        return ingested
    if result.gap is not None:
        await record_gap(
            claim,
            result.gap,
            retry_after_seconds=poll_interval_seconds,
            page_count=result.page_count,
            occurrence_count=result.occurrence_count,
        )
        return ingested

    await complete_checkpoint(
        claim,
        cursor=_max_cursor(claim.cursor, result.candidate_cursor),
        bootstrapped=True,
        interval_seconds=poll_interval_seconds,
        page_count=result.page_count,
        occurrence_count=result.occurrence_count,
    )
    return ingested


async def _read_until_cursor(
    client: XChatClient,
    *,
    account,
    conversation: dict,
    conversation_id: str,
    peer_id: str,
    claim: ClaimedCheckpoint,
) -> _EventRead:
    token = claim.active_gap.resume_token if claim.active_gap else None
    candidate_cursor = claim.active_gap.candidate_cursor if claim.active_gap else None
    seen_tokens: set[str] = set()
    messages: list[_PolledEnvelope] = []
    key_changes: list[str] = []
    key_raw_ids: list[uuid.UUID] = []
    occurrence_count = 0
    cursor_num = _as_int(claim.cursor)
    for page_index in range(_MAX_EVENT_PAGES):
        request_token = token
        await require_claim(claim)
        try:
            page, page_keys, token = await client.read_conversation_events(
                conversation_id,
                pagination_token=request_token,
            )
        except Exception as exc:
            resume_failed = page_index == 0 and request_token is not None
            if page_index == 0 and not resume_failed:
                raise
            return _EventRead(
                envelopes=messages,
                key_changes=_dedupe(key_changes),
                key_raw_ids=key_raw_ids,
                page_count=page_index,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    candidate_cursor=candidate_cursor,
                    resume_token=None if resume_failed else request_token,
                    detail={
                        "conversation_id": conversation_id,
                        "error": type(exc).__name__,
                        "page_index": page_index,
                        "restart_from_checkpoint": resume_failed,
                    },
                ),
            )
        await require_claim(claim)
        common_context = {
            "poll_run_id": str(claim.run_id),
            "page_index": page_index,
            "pagination_token": request_token,
            "next_token": token,
            "cursor_before": claim.cursor,
            "fetch_mode": claim.mode.lower(),
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
                    processing_status=("PENDING" if is_relevant else "IGNORED_BEFORE_CURSOR"),
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
        occurrence_count += len(raw_ids)
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
        page_key_raw_ids = await append_poll_occurrences(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            source="xchat_poll",
            event_namespace="x.xchat.key_change",
            occurrences=key_occurrences,
        )
        occurrence_count += len(page_key_raw_ids)
        key_raw_ids.extend(page_key_raw_ids)
        key_changes.extend(page_keys)
        candidate_cursor = _max_cursor(candidate_cursor, _max_payload_id(page))
        page_count = page_index + 1
        comparable = [value for event in page if (value := _as_int(event.get("id"))) is not None]
        if not token or (cursor_num is not None and comparable and min(comparable) <= cursor_num):
            return _EventRead(
                envelopes=messages,
                key_changes=_dedupe(key_changes),
                key_raw_ids=key_raw_ids,
                page_count=page_count,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
            )
        if token in seen_tokens:
            return _EventRead(
                envelopes=messages,
                key_changes=_dedupe(key_changes),
                key_raw_ids=key_raw_ids,
                page_count=page_count,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    candidate_cursor=candidate_cursor,
                    detail={
                        "conversation_id": conversation_id,
                        "error": "PAGINATION_TOKEN_REPEATED",
                        "page_index": page_index,
                    },
                ),
            )
        seen_tokens.add(token)
    logger.warning("xchat event page budget exhausted conversation=%s", conversation_id)
    return _EventRead(
        envelopes=messages,
        key_changes=_dedupe(key_changes),
        key_raw_ids=key_raw_ids,
        page_count=_MAX_EVENT_PAGES,
        occurrence_count=occurrence_count,
        candidate_cursor=candidate_cursor,
        gap=GapSpec(
            gap_type=GapType.PAGE_CAP,
            candidate_cursor=candidate_cursor,
            resume_token=token,
            detail={
                "conversation_id": conversation_id,
                "page_count": _MAX_EVENT_PAGES,
            },
        ),
    )


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


def _max_payload_id(events: list[dict]) -> str | None:
    values = [_as_int(event.get("id")) for event in events]
    numeric = [value for value in values if value is not None]
    return str(max(numeric)) if numeric else None


def _max_cursor(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value is not None]
    if not values:
        return None
    numeric = [_as_int(value) for value in values]
    comparable = [value for value in numeric if value is not None]
    return str(max(comparable)) if comparable else right or left


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
