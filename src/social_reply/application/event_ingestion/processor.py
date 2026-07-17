import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.jobs import snapshot_to_dict
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Outcome:
    """_process 的结果：status 供 tx1 落 raw 状态；snapshot 非空时 tx1 提交后走 tx2 决策。"""

    status: str
    snapshot: DecisionSnapshot | None = None
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


async def process_raw_event(raw_event_id: str) -> None:
    started = time.perf_counter()
    async with get_session_factory()() as session:
        raw = (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.id == uuid.UUID(raw_event_id))
            )
        ).scalar_one()
        try:
            outcome = await _process(session, raw)
        except (KeyError, ValueError, TypeError, AttributeError):
            # 畸形 payload 不得卡死在 PENDING / 打死信
            await session.rollback()
            outcome = _Outcome("PARSE_FAILED")
        await session.execute(
            update(models.RawEvent)
            # 用入参 UUID 而非 raw.id：异常路径 rollback 后 raw 已过期，
            # 访问 raw.id 会触发同步上下文里的异步刷新 → MissingGreenlet
            .where(models.RawEvent.id == uuid.UUID(raw_event_id))
            .values(processing_status=outcome.status, processed_at=datetime.now(UTC))
        )
        decision_job_id = None
        if outcome.snapshot is not None:
            # 决策任务与入站消息在 tx1 同事务落库。即使进程在 commit 后、入队前崩溃，
            # scheduler 也能从 decision_jobs 补扫恢复，不再出现静默决策丢失。
            decision_job_id = (
                await session.execute(
                    pg_insert(models.DecisionJob)
                    .values(
                        raw_event_id=uuid.UUID(raw_event_id),
                        conversation_id=outcome.conversation_id,
                        message_id=outcome.message_id,
                        account_id=outcome.account_id,
                        snapshot=snapshot_to_dict(outcome.snapshot),
                        status="PENDING",
                    )
                    .on_conflict_do_nothing(index_elements=["message_id"])
                    .returning(models.DecisionJob.id)
                )
            ).scalar_one_or_none()
        await session.commit()

    if decision_job_id is not None:
        from social_reply.application.reply_decision.jobs import process_decision_job

        try:
            # 首次立即执行以保持低延迟；失败已持久化为 FAILED，由 scheduler 补扫恢复。
            await process_decision_job(str(decision_job_id))
        except Exception:
            logger.exception("decision job failed and will be retried, job_id=%s", decision_job_id)
    logger.info(
        "inbound_processed raw_event_id=%s total_ms=%.1f decision_job=%s",
        raw_event_id,
        (time.perf_counter() - started) * 1000,
        decision_job_id,
    )


async def _process(session: AsyncSession, raw: models.RawEvent) -> _Outcome:
    msg = parse_message_created(raw.payload)

    if msg.chatwoot_account_id == 0:
        # normalizer 对缺失 account.id 容错为 0——不得用哨兵值建映射
        return _Outcome("PARSE_FAILED")

    account = (
        await session.execute(
            select(models.PlatformAccount).where(
                models.PlatformAccount.chatwoot_inbox_id == msg.chatwoot_inbox_id
            )
        )
    ).scalar_one_or_none()
    if account is None:
        return _Outcome("SKIPPED_UNKNOWN_INBOX")

    # Outbox reconciliation identifies messages sent by this service.
    if await _is_self_echo(session, msg):
        return _Outcome("SKIPPED_ECHO")

    event_class = classify(msg)
    if event_class is EventClass.IGNORE:
        return _Outcome("SKIPPED_IGNORED")
    if event_class is EventClass.BOT_ECHO:
        # Unreconciled bot messages are not safe inputs to the reply pipeline.
        return _Outcome("SKIPPED_ECHO")

    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        mapping_exists = (
            await session.execute(
                select(models.ConversationMapping).where(
                    models.ConversationMapping.chatwoot_account_id == msg.chatwoot_account_id,
                    models.ConversationMapping.chatwoot_conversation_id
                    == msg.chatwoot_conversation_id,
                )
            )
        ).scalar_one_or_none()
        if mapping_exists is None:
            # 乱序坐席消息：不得用坐席 id 建脏联系人/会话
            return _Outcome("SKIPPED_ORPHAN_AGENT_MSG")

    # The normalized-event constraint is the durable webhook deduplication boundary.
    dedup = await session.execute(
        pg_insert(models.NormalizedEvent)
        .values(
            tenant_id=account.tenant_id,
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
        return _Outcome("SKIPPED_DUPLICATE")

    conversation = await _ensure_conversation(session, account, msg)
    await ensure_state(session, conversation.id, account.automation_default)

    message_id = await _store_message(session, conversation.id, msg, event_class)
    await session.execute(
        update(models.NormalizedEvent)
        .where(models.NormalizedEvent.id == normalized_id)
        .values(conversation_id=conversation.id, message_id=message_id)
    )

    if event_class is EventClass.AGENT_PUBLIC_REPLY:
        # 仅人工坐席 outgoing 非 private 触发接管
        await flip_to_human_active(session, conversation.id, msg.sender_id, "agent_public_reply")
        return _Outcome("PROCESSED")

    # 仅 INBOUND_USER 走决策管线：读当前状态快照供 tx2 CAS
    state_row = (
        await session.execute(
            select(models.AutomationState.state, models.AutomationState.state_version).where(
                models.AutomationState.conversation_id == conversation.id
            )
        )
    ).one()
    snapshot = DecisionSnapshot(
        text=msg.content,
        platform=account.platform,
        tenant_id=account.tenant_id,
        brand_id=account.brand_id,
        account_id=str(account.id),
        conversation_key=conversation.conversation_key,
        automation_state=state_row.state,
        state_version=state_row.state_version,
    )
    return _Outcome("PROCESSED", snapshot, conversation.id, message_id, account.id)


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


def _parse_ts(value: str | int | float | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Chatwoot push_event_data 形态：created_at.to_i（epoch 秒）
        return datetime.fromtimestamp(value, tz=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _ensure_conversation(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Conversation:
    # 先按 Chatwoot 会话映射找（坐席消息的 sender 是坐席而非联系人，不能用 sender 建键）
    mapped = (
        await session.execute(
            select(models.Conversation)
            .join(
                models.ConversationMapping,
                models.ConversationMapping.conversation_id == models.Conversation.id,
            )
            .where(
                models.ConversationMapping.chatwoot_account_id == msg.chatwoot_account_id,
                models.ConversationMapping.chatwoot_conversation_id == msg.chatwoot_conversation_id,
            )
        )
    ).scalar_one_or_none()
    if mapped is not None:
        return mapped

    contact = await _ensure_contact(session, account, msg)
    key = build_dm_conversation_key(
        platform=account.platform,
        platform_account_id=str(account.id),
        external_user_id=contact.external_user_id,
    )
    # 查不到即按 conversation_key upsert，消除竞态
    await session.execute(
        pg_insert(models.Conversation)
        .values(
            id=uuid.uuid4(),
            tenant_id=account.tenant_id,
            brand_id=account.brand_id,
            platform=account.platform,
            platform_account_id=account.id,
            contact_id=contact.id,
            conversation_key=key,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "conversation_key"])
    )
    conversation = (
        await session.execute(
            select(models.Conversation).where(
                models.Conversation.tenant_id == account.tenant_id,
                models.Conversation.conversation_key == key,
            )
        )
    ).scalar_one()
    await session.execute(
        pg_insert(models.ConversationMapping)
        .values(
            chatwoot_account_id=msg.chatwoot_account_id,
            chatwoot_conversation_id=msg.chatwoot_conversation_id,
            conversation_id=conversation.id,
        )
        .on_conflict_do_nothing(index_elements=["chatwoot_account_id", "chatwoot_conversation_id"])
    )
    return conversation


async def _ensure_contact(
    session: AsyncSession, account: models.PlatformAccount, msg: ChatwootMessage
) -> models.Contact:
    external_user_id = msg.sender_id or "unknown"
    await session.execute(
        pg_insert(models.Contact)
        .values(
            id=uuid.uuid4(),
            tenant_id=account.tenant_id,
            platform=account.platform,
            platform_account_id=account.id,
            external_user_id=external_user_id,
        )
        .on_conflict_do_nothing(index_elements=["platform_account_id", "external_user_id"])
    )
    return (
        await session.execute(
            select(models.Contact).where(
                models.Contact.platform_account_id == account.id,
                models.Contact.external_user_id == external_user_id,
            )
        )
    ).scalar_one()


async def _store_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    msg: ChatwootMessage,
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
