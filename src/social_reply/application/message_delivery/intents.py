import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.message_delivery.contracts import build_direct_reply_destination
from social_reply.domain.platform_accounts import capability_text_limit
from social_reply.infrastructure.database import models


class OutboxOrigin(StrEnum):
    DECISION = "DECISION"
    DRAFT_APPROVAL = "DRAFT_APPROVAL"
    MANUAL_REPLY = "MANUAL_REPLY"
    SYSTEM_NOTICE = "SYSTEM_NOTICE"


class OutboxActor(StrEnum):
    BOT = "BOT"
    ADMIN_HUMAN = "ADMIN_HUMAN"
    SYSTEM = "SYSTEM"


class OutboxIntentError(ValueError):
    pass


class OutboxIdempotencyConflict(OutboxIntentError):
    pass


def decision_idempotency_key(
    account_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    action: str,
) -> str:
    raw = f"{account_id}:{conversation_id}:{message_id}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _stored_idempotency_key(
    *, tenant_id: str, conversation_id: uuid.UUID, idempotency_key: str
) -> str:
    value = idempotency_key.strip()
    if not value or len(value) > 255:
        raise OutboxIntentError("idempotency_key_invalid")
    raw = f"outbox-intent:v1:{tenant_id}:{conversation_id}:{value}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def find_outbox_intent(
    session: AsyncSession,
    *,
    tenant_id: str,
    conversation_id: uuid.UUID,
    idempotency_key: str,
) -> models.OutboxMessage | None:
    stored_key = _stored_idempotency_key(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
    )
    return (
        await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.idempotency_key == stored_key)
        )
    ).scalar_one_or_none()


async def create_or_get_outbox_intent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    platform_account_id: uuid.UUID,
    reply_to_message_id: uuid.UUID | None,
    text: str,
    origin_kind: OutboxOrigin,
    actor_kind: OutboxActor,
    actor_id: str | None,
    idempotency_key: str,
    visibility: str = "public",
    message_type: str = "text",
    payload_metadata: dict | None = None,
) -> uuid.UUID:
    """Create an immutable delivery intent inside the caller's transaction."""
    reply_text = text.strip()
    if not reply_text:
        raise OutboxIntentError("reply_text_required")
    if visibility not in {"public", "private"}:
        raise OutboxIntentError("reply_visibility_invalid")

    row = (
        await session.execute(
            select(models.Conversation, models.PlatformAccount)
            .join(
                models.PlatformAccount,
                models.PlatformAccount.id == models.Conversation.platform_account_id,
            )
            .where(
                models.Conversation.id == conversation_id,
                models.PlatformAccount.id == platform_account_id,
                models.PlatformAccount.tenant_id == models.Conversation.tenant_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise OutboxIntentError("conversation_account_scope_mismatch")
    conversation, account = row
    direct_delivery = (account.config or {}).get("delivery_mode") == "direct"

    source_message = None
    if reply_to_message_id is not None:
        source_message = await session.get(models.Message, reply_to_message_id)
        if (
            source_message is None
            or source_message.conversation_id != conversation.id
            or source_message.direction != "inbound"
        ):
            raise OutboxIntentError("reply_to_message_scope_mismatch")
    if direct_delivery and source_message is None:
        raise OutboxIntentError("reply_to_message_required")

    destination_type = "chatwoot_conversation"
    target: dict = {}
    valid_until = None
    if direct_delivery:
        limit = capability_text_limit(account.platform, dict(account.capability or {}))
        if limit is None:
            raise OutboxIntentError("account_capability_invalid")
        if len(reply_text) > limit:
            raise OutboxIntentError("reply_text_too_long")
        try:
            destination = build_direct_reply_destination(
                platform=account.platform,
                reply_target=dict(source_message.reply_target or {}),
                visibility=visibility,
                occurred_at=source_message.occurred_at,
                now=datetime.now(UTC),
            )
        except ValueError as exc:
            raise OutboxIntentError(str(exc)) from exc
        destination_type = destination.destination_type
        target = destination.target
        valid_until = destination.valid_until
        if valid_until is not None and valid_until <= datetime.now(UTC):
            raise OutboxIntentError("delivery_window_expired")

    metadata = dict(payload_metadata or {})
    if metadata.keys() & {"text", "visibility", "target"}:
        raise OutboxIntentError("payload_metadata_contains_reserved_key")
    payload = {
        "text": reply_text,
        "visibility": visibility,
        "target": target,
        **metadata,
    }
    stored_key = _stored_idempotency_key(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        idempotency_key=idempotency_key,
    )
    candidate_id = uuid.uuid4()
    inserted_id = (
        await session.execute(
            pg_insert(models.OutboxMessage)
            .values(
                id=candidate_id,
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                platform_account_id=account.id,
                destination_type=destination_type,
                destination_id=conversation.conversation_key,
                message_type=message_type,
                payload=payload,
                reply_to_message_id=reply_to_message_id,
                origin_kind=origin_kind,
                actor_kind=actor_kind,
                actor_id=actor_id,
                idempotency_key=stored_key,
                status="PENDING",
                valid_until=valid_until,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(models.OutboxMessage.id)
        )
    ).scalar_one_or_none()
    if inserted_id is not None:
        return inserted_id

    existing = (
        await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.idempotency_key == stored_key)
        )
    ).scalar_one()
    immutable_values = (
        existing.tenant_id == conversation.tenant_id,
        existing.conversation_id == conversation.id,
        existing.platform_account_id == account.id,
        existing.reply_to_message_id == reply_to_message_id,
        existing.origin_kind == origin_kind,
        existing.actor_kind == actor_kind,
        existing.actor_id == actor_id,
        existing.message_type == message_type,
        existing.destination_type == destination_type,
        existing.destination_id == conversation.conversation_key,
        existing.valid_until == valid_until,
        existing.payload == payload,
    )
    if not all(immutable_values):
        raise OutboxIdempotencyConflict("idempotency_key_reused_with_different_intent")
    return existing.id
