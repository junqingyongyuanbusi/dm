import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import insert, update

from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


@dataclass(frozen=True)
class PollOccurrence:
    payload: dict[str, Any]
    external_event_id: str | None = None
    external_conversation_id: str | None = None
    occurred_at: datetime | None = None
    context: dict[str, Any] = field(default_factory=dict)
    processing_status: str = "PENDING"


async def append_poll_occurrences(
    *,
    tenant_id: str,
    platform_account_id: uuid.UUID,
    source: str,
    event_namespace: str,
    occurrences: list[PollOccurrence],
) -> list[uuid.UUID]:
    if not occurrences:
        return []
    ids = [uuid.uuid4() for _ in occurrences]
    values = [
        {
            "id": raw_event_id,
            "tenant_id": tenant_id,
            "platform_account_id": platform_account_id,
            "source": source,
            "ingress_kind": "poll",
            "event_namespace": event_namespace,
            "external_event_id": occurrence.external_event_id,
            "external_conversation_id": occurrence.external_conversation_id,
            "payload": occurrence.payload,
            "headers": {},
            "context": occurrence.context,
            "schema_version": 1,
            "occurred_at": occurrence.occurred_at,
            "processing_status": occurrence.processing_status,
        }
        for raw_event_id, occurrence in zip(ids, occurrences, strict=True)
    ]
    async with get_session_factory()() as session:
        await session.execute(insert(models.RawEvent), values)
        await session.commit()
    return ids


async def mark_poll_occurrences(raw_event_ids: list[uuid.UUID], status: str) -> None:
    if not raw_event_ids:
        return
    async with get_session_factory()() as session:
        await session.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id.in_(raw_event_ids))
            .values(processing_status=status)
        )
        await session.commit()
