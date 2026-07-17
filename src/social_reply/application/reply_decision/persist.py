import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import AutomationStateEnum
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.infrastructure.database import models


def _idempotency_key(
    account_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, action: str
) -> str:
    # 不含 prompt_version（换版重投不得产生重复发送）
    raw = f"{account_id}:{conversation_id}:{message_id}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def persist_decision(
    session: AsyncSession,
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    account_id: uuid.UUID,
    decision: ReplyDecision,
    prompt_version: str,
) -> uuid.UUID | None:
    """在调用方事务内写 reply_decisions（永远写）+ 按 action 落地副作用。
    auto_reply/draft → 写 outbox（auto_reply 受 state_version CAS 守护，defense 1）。
    返回 outbox_id 或 None。调用方负责 commit。"""
    outbox_id: uuid.UUID | None = None
    message_type: str | None = None
    account = (
        await session.execute(
            select(
                models.PlatformAccount.tenant_id,
                models.PlatformAccount.platform,
                models.PlatformAccount.config,
            ).where(models.PlatformAccount.id == account_id)
        )
    ).one()

    if message_id is not None:
        existing = (
            await session.execute(
                select(models.ReplyDecision.outbox_id).where(
                    models.ReplyDecision.message_id == message_id
                )
            )
        ).first()
        if existing is not None:
            return existing.outbox_id

    if decision.action is ReplyAction.AUTO_REPLY:
        # CAS defense 1：仅当会话仍是 BOT_ACTIVE 且 version 未变时才写 outbox
        current = (
            await session.execute(
                select(models.AutomationState.state, models.AutomationState.state_version).where(
                    models.AutomationState.conversation_id == conversation_id
                )
            )
        ).first()
        if (
            current is not None
            and current.state == AutomationStateEnum.BOT_ACTIVE
            and current.state_version == snapshot.state_version
        ):
            message_type = "text"
    elif decision.action is ReplyAction.DRAFT:
        # Direct platforms have no private-note channel. Retain the ReplyDecision as a
        # draft for the admin inbox; never send it to the customer before approval.
        if (account.config or {}).get("delivery_mode") != "direct":
            message_type = "private_note"
    elif decision.action is ReplyAction.HANDOFF:
        # 转人工：置 HANDOFF_PENDING（仅当当前非终态）
        await session.execute(
            update(models.AutomationState)
            .where(
                models.AutomationState.conversation_id == conversation_id,
                models.AutomationState.state.notin_(
                    [AutomationStateEnum.HUMAN_ACTIVE, AutomationStateEnum.CLOSED]
                ),
            )
            .values(
                state=AutomationStateEnum.HANDOFF_PENDING,
                state_version=models.AutomationState.state_version + 1,
                state_changed_reason="rule_or_guard_handoff",
            )
        )

    if message_type is not None:
        reply_target = {}
        destination_type = "chatwoot_conversation"
        if (account.config or {}).get("delivery_mode") == "direct" and message_id is not None:
            reply_target = dict(
                (
                    await session.execute(
                        select(models.Message.reply_target).where(models.Message.id == message_id)
                    )
                ).scalar_one_or_none()
                or {}
            )
            kind = reply_target.get("kind", "dm")
            if account.platform == "telegram":
                destination_type = "telegram_dm"
            elif account.platform == "facebook":
                destination_type = (
                    "meta_private_reply"
                    if decision.reply_visibility is Visibility.PRIVATE and kind == "comment"
                    else "meta_public_comment"
                    if kind == "comment"
                    else "meta_messenger_dm"
                )
            elif account.platform == "instagram":
                destination_type = (
                    "meta_private_reply"
                    if decision.reply_visibility is Visibility.PRIVATE and kind == "comment"
                    else "meta_public_comment"
                    if kind == "comment"
                    else "meta_instagram_dm"
                )
            elif account.platform == "whatsapp":
                destination_type = "whatsapp_session_message"
            elif account.platform == "x":
                destination_type = "x_post_reply" if kind == "reply" else "x_dm"
            else:
                raise ValueError(f"unsupported_direct_platform:{account.platform}")
            if destination_type == "meta_private_reply":
                reply_target = {**reply_target, "kind": "private_reply"}
        valid_until = None
        if destination_type in {
            "meta_messenger_dm",
            "meta_instagram_dm",
            "whatsapp_session_message",
        }:
            inbound_occurred_at = (
                await session.execute(
                    select(models.Message.occurred_at).where(models.Message.id == message_id)
                )
            ).scalar_one_or_none()
            valid_until = (inbound_occurred_at or datetime.now(UTC)) + timedelta(hours=24)
        elif destination_type == "meta_private_reply":
            valid_until = datetime.now(UTC) + timedelta(days=7)
        candidate_outbox_id = uuid.uuid4()
        inserted_outbox = (
            await session.execute(
                pg_insert(models.OutboxMessage)
                .values(
                    id=candidate_outbox_id,
                    tenant_id=account.tenant_id,
                    conversation_id=conversation_id,
                    platform_account_id=account_id,
                    destination_type=destination_type,
                    destination_id=snapshot.conversation_key,
                    message_type=message_type,
                    payload={
                        "text": decision.reply_text or "",
                        "visibility": decision.reply_visibility,
                        "target": reply_target,
                    },
                    idempotency_key=_idempotency_key(
                        account_id, conversation_id, message_id or conversation_id, decision.action
                    ),
                    status="PENDING",
                    valid_until=valid_until,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(models.OutboxMessage.id)
            )
        ).scalar_one_or_none()
        if inserted_outbox is not None:
            outbox_id = inserted_outbox
        else:
            outbox_id = (
                await session.execute(
                    select(models.OutboxMessage.id).where(
                        models.OutboxMessage.idempotency_key
                        == _idempotency_key(
                            account_id,
                            conversation_id,
                            message_id or conversation_id,
                            decision.action,
                        )
                    )
                )
            ).scalar_one()

    inserted_decision = (
        await session.execute(
            pg_insert(models.ReplyDecision)
            .values(
                id=uuid.uuid4(),
                tenant_id=account.tenant_id,
                conversation_id=conversation_id,
                message_id=message_id,
                action=decision.action,
                intent=decision.intent,
                risk_level=decision.risk_level,
                confidence=decision.confidence,
                reply_text=decision.reply_text,
                reply_visibility=decision.reply_visibility,
                reason_codes=list(decision.reason_codes),
                source=decision.source,
                prompt_version=prompt_version,
                state_version_at_decision=snapshot.state_version,
                outbox_id=outbox_id,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(models.ReplyDecision.outbox_id)
        )
    ).scalar_one_or_none()
    if inserted_decision is None and message_id is not None:
        return (
            await session.execute(
                select(models.ReplyDecision.outbox_id).where(
                    models.ReplyDecision.message_id == message_id
                )
            )
        ).scalar_one()
    return outbox_id
