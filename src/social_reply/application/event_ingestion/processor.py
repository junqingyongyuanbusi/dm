import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.connectors.chatwoot.normalizer import (
    ChatwootMessage,
    EventClass,
    classify,
    parse_message_created,
)
from social_reply.domain.automation.state_machine import ensure_state, flip_to_human_active
from social_reply.domain.messages.events import build_dm_conversation_key
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings


async def process_raw_event(raw_event_id: str) -> None:
    async with get_session_factory()() as session:
        raw = (await session.execute(
            select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_event_id))
        )).scalar_one()
        try:
            status = await _process(session, raw)
        except (KeyError, ValueError, TypeError):
            # 畸形 payload 不得卡死在 PENDING / 打死信（Task 4 评审 I1）
            await session.rollback()
            status = "PARSE_FAILED"
        await session.execute(
            update(models.RawEvent)
            # 用入参 UUID 而非 raw.id：异常路径 rollback 后 raw 已过期，
            # 访问 raw.id 会触发同步上下文里的异步刷新 → MissingGreenlet
            .where(models.RawEvent.id == uuid.UUID(raw_event_id))
            .values(processing_status=status, processed_at=datetime.now().astimezone())
        )
        await session.commit()


async def _process(session: AsyncSession, raw: models.RawEvent) -> str:
    msg = parse_message_created(raw.payload)

    if msg.chatwoot_account_id == 0:
        # normalizer 对缺失 account.id 容错为 0——不得用哨兵值建映射（Task 4 评审 M1）
        return "PARSE_FAILED"

    account = (await session.execute(
        select(models.PlatformAccount)
        .where(models.PlatformAccount.chatwoot_inbox_id == msg.chatwoot_inbox_id)
    )).scalar_one_or_none()
    if account is None:
        return "SKIPPED_UNKNOWN_INBOX"

    # PLAN.md §四 回声断路器规则 1：与 Outbox 已记录的 chatwoot_message_id 比对
    if await _is_self_echo(session, msg):
        return "SKIPPED_ECHO"

    event_class = classify(msg)
    if event_class is EventClass.IGNORE:
        return "SKIPPED_IGNORED"
    if event_class is EventClass.BOT_ECHO:
        # sender=agent_bot 但 Outbox 无记录（如手动经 bot token 发送）：仅对账，不入管线
        return "SKIPPED_ECHO"

    # PLAN.md §十二 去重：normalized_events 唯一约束，冲突即重复投递
    settings = get_settings()
    dedup = await session.execute(
        pg_insert(models.NormalizedEvent)
        .values(
            tenant_id=settings.tenant_id,
            platform=account.platform,
            platform_account_id=account.id,
            external_event_id=str(msg.chatwoot_message_id),
            event_type=_event_type(event_class),
            raw_event_id=raw.id,
            occurred_at=_parse_ts(msg.occurred_at_iso),
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "platform", "platform_account_id", "external_event_id"]
        )
        .returning(models.NormalizedEvent.id)
    )
    normalized_id = dedup.scalar_one_or_none()
    if normalized_id is None:
        return "SKIPPED_DUPLICATE"

    conversation = await _ensure_conversation(session, account, msg)
    await ensure_state(session, conversation.id, account.automation_default)

    message_id = await _store_message(session, conversation.id, msg, event_class)
    await session.execute(
        update(models.NormalizedEvent)
        .where(models.NormalizedEvent.id == normalized_id)
        .values(conversation_id=conversation.id, message_id=message_id)
    )

    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        # PLAN.md §六：仅人工坐席 outgoing 非 private 触发接管
        await flip_to_human_active(
            session, conversation.id, msg.sender_id, "agent_public_reply"
        )
    return "PROCESSED"


async def _is_self_echo(session: AsyncSession, msg: ChatwootMessage) -> bool:
    row = await session.execute(
        select(models.OutboxMessage.id)
        .where(models.OutboxMessage.chatwoot_message_id == msg.chatwoot_message_id)
        .limit(1)
    )
    return row.first() is not None


def _event_type(event_class: EventClass) -> str:
    return {
        EventClass.INBOUND_USER: "dm.message.created",
        EventClass.AGENT_PUBLIC_REPLY: "agent.message.created",
    }[event_class]


def _parse_ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


async def _ensure_conversation(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Conversation:
    # 先按 Chatwoot 会话映射找（坐席消息的 sender 是坐席而非联系人，不能用 sender 建键）
    mapped = (await session.execute(
        select(models.Conversation)
        .join(models.ConversationMapping,
              models.ConversationMapping.conversation_id == models.Conversation.id)
        .where(
            models.ConversationMapping.chatwoot_account_id == msg.chatwoot_account_id,
            models.ConversationMapping.chatwoot_conversation_id == msg.chatwoot_conversation_id,
        )
    )).scalar_one_or_none()
    if mapped is not None:
        return mapped

    contact = await _ensure_contact(session, account, msg)
    key = build_dm_conversation_key(
        platform=account.platform,
        platform_account_id=str(account.id),
        external_user_id=contact.external_user_id,
    )
    # PLAN.md §十：查不到即按 conversation_key upsert，消除竞态
    await session.execute(
        pg_insert(models.Conversation)
        .values(
            id=uuid.uuid4(), tenant_id=get_settings().tenant_id, brand_id=account.brand_id,
            platform=account.platform, platform_account_id=account.id,
            contact_id=contact.id, conversation_key=key,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "conversation_key"])
    )
    conversation = (await session.execute(
        select(models.Conversation).where(
            models.Conversation.tenant_id == get_settings().tenant_id,
            models.Conversation.conversation_key == key,
        )
    )).scalar_one()
    await session.execute(
        pg_insert(models.ConversationMapping)
        .values(
            chatwoot_account_id=msg.chatwoot_account_id,
            chatwoot_conversation_id=msg.chatwoot_conversation_id,
            conversation_id=conversation.id,
        )
        .on_conflict_do_nothing(
            index_elements=["chatwoot_account_id", "chatwoot_conversation_id"]
        )
    )
    return conversation


async def _ensure_contact(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Contact:
    external_user_id = msg.sender_id or "unknown"
    await session.execute(
        pg_insert(models.Contact)
        .values(
            id=uuid.uuid4(), platform=account.platform, platform_account_id=account.id,
            external_user_id=external_user_id,
        )
        .on_conflict_do_nothing(index_elements=["platform_account_id", "external_user_id"])
    )
    return (await session.execute(
        select(models.Contact).where(
            models.Contact.platform_account_id == account.id,
            models.Contact.external_user_id == external_user_id,
        )
    )).scalar_one()


async def _store_message(
    session: AsyncSession, conversation_id: uuid.UUID, msg: ChatwootMessage,
    event_class: EventClass,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    inbound = event_class is EventClass.INBOUND_USER
    await session.execute(
        pg_insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound" if inbound else "outbound",
            sender_type="contact" if inbound else "agent",
            text=msg.content,
            chatwoot_message_id=msg.chatwoot_message_id,
            private=msg.private,
            occurred_at=_parse_ts(msg.occurred_at_iso),
        )
    )
    return message_id
