"""Durable X DM polling with append-only evidence and resumable gaps."""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import update

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
from social_reply.connectors.x.adapter import XWebhookAdapter
from social_reply.connectors.x.client import XClient
from social_reply.domain.platform_accounts import CapabilityKey, capability_enabled
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.getenv("X_DM_POLL_INTERVAL_SECONDS", "90"))
_last_poll_at: float | None = None
_MAX_PAGES_PER_POLL = 3
_CURSOR_LOOKBACK_MS = 5 * 60 * 1000
_DEFAULT_OWNER = f"x-dm:{uuid.uuid4()}"


@dataclass(frozen=True)
class _PolledEvent:
    payload: dict
    raw_event_id: uuid.UUID


@dataclass(frozen=True)
class _ReadResult:
    events: list[_PolledEvent]
    page_count: int
    occurrence_count: int
    candidate_cursor: str | None
    gap: GapSpec | None = None


async def poll_x_direct_messages(*, scheduler_owner: str | None = None) -> list[str]:
    global _last_poll_at
    settings = get_settings()
    now = time.monotonic()
    if _last_poll_at is not None and now - _last_poll_at < _POLL_INTERVAL_SECONDS:
        return []
    _last_poll_at = now

    owner = scheduler_owner or _DEFAULT_OWNER
    ingested: list[str] = []
    for account in await list_active_accounts_by_platform("x"):
        dm_capable = capability_enabled(account.capability or {}, CapabilityKey.DM)
        if not settings.x_legacy_dm_enabled and not dm_capable:
            continue
        if not account.external_account_id:
            logger.warning("x account %s has no external_account_id; skipping poll", account.id)
            continue
        config = account.config or {}
        initial_cursor = config.get("x_dm_cursor")
        checkpoint = await ensure_checkpoint(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            stream=CheckpointStream.X_LEGACY_DM,
            initial_cursor=str(initial_cursor) if initial_cursor is not None else None,
            initial_bootstrapped=bool(config.get("x_dm_bootstrapped")),
        )
        claim = await claim_checkpoint(checkpoint.id, owner=owner)
        if claim is None:
            continue
        try:
            ingested.extend(
                await _poll_account(
                    account,
                    claim=claim,
                    reconcile_capability=settings.x_legacy_dm_enabled and not dm_capable,
                )
            )
        except httpx.HTTPStatusError as exc:
            await fail_run(
                claim,
                error_code=f"X_HTTP_{exc.response.status_code}",
                error_message=str(exc),
                retry_after_seconds=_POLL_INTERVAL_SECONDS,
            )
            if exc.response.status_code == 429:
                logger.warning("x dm poll rate-limited account=%s", account.id)
            else:
                logger.error(
                    "x dm poll http error account=%s status=%s",
                    account.id,
                    exc.response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - one account must not block others
            await fail_run(
                claim,
                error_code="X_DM_POLL_FAILED",
                error_message=str(exc),
                retry_after_seconds=_POLL_INTERVAL_SECONDS,
            )
            logger.exception("x dm poll failed account=%s", account.id)
    return ingested


async def _poll_account(
    account,
    *,
    claim: ClaimedCheckpoint,
    reconcile_capability: bool = False,
) -> list[str]:
    credentials = x_credentials(account)
    client = XClient(
        consumer_key=credentials["consumer_key"],
        consumer_secret=credentials["consumer_secret"],
        access_token=credentials["access_token"],
        access_token_secret=credentials["access_token_secret"],
        api_base_url=(account.config or {}).get("api_base_url", "https://api.x.com"),
    )
    try:
        result = await _read_until_cursor(
            client,
            account=account,
            claim=claim,
            bootstrap=not claim.bootstrapped,
        )
    finally:
        await client.aclose()
    await require_claim(claim)
    if reconcile_capability:
        await _mark_dm_capable(account.id)

    if not claim.bootstrapped:
        if result.gap is not None:
            await record_gap(
                claim,
                result.gap,
                retry_after_seconds=_POLL_INTERVAL_SECONDS,
                page_count=result.page_count,
                occurrence_count=result.occurrence_count,
            )
            return []
        await complete_checkpoint(
            claim,
            cursor=result.candidate_cursor,
            bootstrapped=True,
            interval_seconds=_POLL_INTERVAL_SECONDS,
            page_count=result.page_count,
            occurrence_count=result.occurrence_count,
        )
        return []

    adapter = XWebhookAdapter(
        account_id=str(account.id),
        external_account_id=str(account.external_account_id),
    )
    ingested: list[str] = []
    for occurrence in sorted(result.events, key=lambda item: _as_int(item.payload.get("id")) or 0):
        await require_claim(claim)
        event = occurrence.payload
        event_id = str(event.get("id") or "")
        canonical = adapter.normalize({"direct_message_events": [event]})
        if not canonical:
            sender_id = str(event.get("sender_id") or "")
            status = (
                "IGNORED_SELF"
                if sender_id == str(account.external_account_id)
                else "IGNORED_UNSUPPORTED"
            )
            await mark_poll_occurrences([occurrence.raw_event_id], status)
            continue
        for item in canonical:
            if await ingest_canonical_event(item, raw_event_id=occurrence.raw_event_id) is not None:
                ingested.append(event_id)

    await require_claim(claim)
    if result.gap is not None:
        await record_gap(
            claim,
            result.gap,
            retry_after_seconds=_POLL_INTERVAL_SECONDS,
            page_count=result.page_count,
            occurrence_count=result.occurrence_count,
        )
        return ingested

    cursor = _max_cursor(claim.cursor, result.candidate_cursor)
    await complete_checkpoint(
        claim,
        cursor=cursor,
        bootstrapped=True,
        interval_seconds=_POLL_INTERVAL_SECONDS,
        page_count=result.page_count,
        occurrence_count=result.occurrence_count,
    )
    if not result.events:
        logger.info("x_dm_poll idle account=%s cursor=%s", account.id, claim.cursor)
    return ingested


async def _read_until_cursor(
    client: XClient,
    *,
    account,
    claim: ClaimedCheckpoint,
    bootstrap: bool = False,
) -> _ReadResult:
    cursor_num = _as_int(claim.cursor)
    floor_num = cursor_num - (_CURSOR_LOOKBACK_MS << 22) if cursor_num is not None else None
    pagination_token = claim.active_gap.resume_token if claim.active_gap else None
    candidate_cursor = claim.active_gap.candidate_cursor if claim.active_gap else None
    seen_tokens: set[str] = set()
    collected: list[_PolledEvent] = []
    occurrence_count = 0

    for page_index in range(_MAX_PAGES_PER_POLL):
        request_token = pagination_token
        await require_claim(claim)
        try:
            events, pagination_token = await client.read_dm_events(
                max_results=100,
                pagination_token=request_token,
            )
        except Exception as exc:
            resume_failed = page_index == 0 and request_token is not None
            if page_index == 0 and not resume_failed:
                raise
            return _ReadResult(
                events=collected,
                page_count=page_index,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    candidate_cursor=candidate_cursor,
                    resume_token=None if resume_failed else request_token,
                    detail={
                        "error": type(exc).__name__,
                        "page_index": page_index,
                        "restart_from_checkpoint": resume_failed,
                    },
                ),
            )

        await require_claim(claim)
        occurrences: list[PollOccurrence] = []
        relevant: list[bool] = []
        for item_index, event in enumerate(events):
            event_num = _as_int(event.get("id"))
            is_relevant = floor_num is None or event_num is None or event_num > floor_num
            relevant.append(is_relevant)
            occurrences.append(
                PollOccurrence(
                    payload=dict(event),
                    external_event_id=str(event.get("id") or "") or None,
                    external_conversation_id=str(
                        event.get("dm_conversation_id") or event.get("sender_id") or ""
                    )
                    or None,
                    occurred_at=_parse_time(event.get("created_at")),
                    processing_status=(
                        "IGNORED_BOOTSTRAP"
                        if bootstrap
                        else "PENDING"
                        if is_relevant
                        else "IGNORED_BEFORE_LOOKBACK"
                    ),
                    context={
                        "poll_run_id": str(claim.run_id),
                        "page_index": page_index,
                        "item_index": item_index,
                        "pagination_token": request_token,
                        "next_token": pagination_token,
                        "cursor_before": claim.cursor,
                        "lookback_floor": str(floor_num) if floor_num is not None else None,
                        "fetch_mode": "bootstrap" if bootstrap else claim.mode.lower(),
                    },
                )
            )
        raw_ids = await append_poll_occurrences(
            tenant_id=account.tenant_id,
            platform_account_id=account.id,
            source="x_dm_poll",
            event_namespace="x.legacy_dm",
            occurrences=occurrences,
        )
        occurrence_count += len(raw_ids)
        collected.extend(
            _PolledEvent(payload=event, raw_event_id=raw_event_id)
            for event, raw_event_id, is_relevant in zip(events, raw_ids, relevant, strict=True)
            if is_relevant
        )
        candidate_cursor = _max_cursor(candidate_cursor, _max_payload_id(events))
        page_count = page_index + 1
        comparable = [value for event in events if (value := _as_int(event.get("id"))) is not None]
        if not pagination_token or (
            floor_num is not None and comparable and min(comparable) <= floor_num
        ):
            return _ReadResult(
                events=collected,
                page_count=page_count,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
            )
        if pagination_token in seen_tokens:
            return _ReadResult(
                events=collected,
                page_count=page_count,
                occurrence_count=occurrence_count,
                candidate_cursor=candidate_cursor,
                gap=GapSpec(
                    gap_type=GapType.PAGINATION_ERROR,
                    candidate_cursor=candidate_cursor,
                    detail={"error": "PAGINATION_TOKEN_REPEATED", "page_index": page_index},
                ),
            )
        seen_tokens.add(pagination_token)

    logger.warning("x_dm_poll page budget exhausted account=%s", account.id)
    return _ReadResult(
        events=collected,
        page_count=_MAX_PAGES_PER_POLL,
        occurrence_count=occurrence_count,
        candidate_cursor=candidate_cursor,
        gap=GapSpec(
            gap_type=GapType.PAGE_CAP,
            candidate_cursor=candidate_cursor,
            resume_token=pagination_token,
            detail={"page_count": _MAX_PAGES_PER_POLL},
        ),
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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


async def _mark_dm_capable(account_id) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            update(models.PlatformAccount)
            .where(models.PlatformAccount.id == account_id)
            .values(
                capability=models.PlatformAccount.capability.op("||")(
                    {CapabilityKey.DM.value: True}
                ),
                config_version=models.PlatformAccount.config_version + 1,
            )
        )
        await session.commit()
