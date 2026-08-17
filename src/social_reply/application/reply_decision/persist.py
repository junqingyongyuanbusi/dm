import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.human_workflow import (
    ensure_open_human_work_item,
)
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
)
from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxOrigin,
    create_or_get_outbox_intent,
    decision_idempotency_key,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import AutomationStateEnum
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import (
    acquire_conversation_delivery_xact_lock,
)
from social_reply.shared.config import get_settings


class ChatwootDecisionDeferred(RuntimeError):
    pass


class DecisionDeliveryConfigurationError(RuntimeError):
    pass


def ensure_decision_delivery_available(
    *,
    account_config: dict,
    chatwoot_inbox_id: int | None,
    chatwoot_enabled: bool,
) -> bool:
    direct_delivery = account_config.get("delivery_mode") == "direct"
    if direct_delivery:
        return True
    if chatwoot_inbox_id is None:
        raise DecisionDeliveryConfigurationError("chatwoot_inbox_id_missing")
    if not chatwoot_enabled:
        raise ChatwootDecisionDeferred("chatwoot_disabled")
    return False


def _idempotency_key(
    account_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, action: str
) -> str:
    return decision_idempotency_key(account_id, conversation_id, message_id, action)


async def persist_decision(
    session: AsyncSession,
    snapshot: DecisionSnapshot,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    account_id: uuid.UUID,
    decision: ReplyDecision,
    prompt_version: str,
    *,
    decision_job_id: uuid.UUID | None = None,
    decision_generation: int | None = None,
    decision_claim_token: uuid.UUID | None = None,
    handoff_notification_ids: list[uuid.UUID] | None = None,
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
                models.PlatformAccount.chatwoot_inbox_id,
            ).where(models.PlatformAccount.id == account_id)
        )
    ).one()
    direct_delivery = ensure_decision_delivery_available(
        account_config=dict(account.config or {}),
        chatwoot_inbox_id=account.chatwoot_inbox_id,
        chatwoot_enabled=get_settings().chatwoot_enabled,
    )

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
        if not direct_delivery and decision.reply_text:
            message_type = "private_note"
    elif decision.action is ReplyAction.HANDOFF:
        await acquire_conversation_delivery_xact_lock(session, conversation_id)
        current = (
            await session.execute(
                select(models.AutomationState)
                .where(models.AutomationState.conversation_id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        may_create_work = current is not None and (
            current.state == AutomationStateEnum.HANDOFF_PENDING
            or (
                current.state_version == snapshot.state_version
                and current.state
                not in [AutomationStateEnum.HUMAN_ACTIVE, AutomationStateEnum.CLOSED]
            )
        )
        if may_create_work:
            reason_code = decision.reason_codes[-1] if decision.reason_codes else "HANDOFF"
            if current.state != AutomationStateEnum.HANDOFF_PENDING:
                current.state = AutomationStateEnum.HANDOFF_PENDING
                current.state_version += 1
                current.state_changed_reason = reason_code
            work = await ensure_open_human_work_item(
                session,
                tenant_id=account.tenant_id,
                conversation_id=conversation_id,
                reason_code=reason_code,
            )
            notification = await ensure_handoff_notification_intent(session, work=work)
            if handoff_notification_ids is not None and notification.status == "PENDING":
                handoff_notification_ids.append(notification.id)

    if message_type is not None:
        if direct_delivery and message_id is None:
            raise DecisionDeliveryConfigurationError("direct_delivery_message_id_missing")
        try:
            outbox_id = await create_or_get_outbox_intent(
                session,
                conversation_id=conversation_id,
                platform_account_id=account_id,
                reply_to_message_id=message_id,
                text=decision.reply_text or "",
                origin_kind=OutboxOrigin.DECISION,
                actor_kind=OutboxActor.BOT,
                actor_id=None,
                idempotency_key=_idempotency_key(
                    account_id,
                    conversation_id,
                    message_id or conversation_id,
                    decision.action,
                ),
                visibility=decision.reply_visibility,
                message_type=message_type,
            )
        except ValueError as exc:
            raise DecisionDeliveryConfigurationError(str(exc)) from exc

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
                original_reply_text=decision.reply_text,
                review_action="PENDING" if decision.action is ReplyAction.DRAFT else None,
                reply_visibility=decision.reply_visibility,
                reason_codes=list(decision.reason_codes),
                source=decision.source,
                prompt_version=prompt_version,
                request_language=decision.request_language,
                reply_language=decision.reply_language,
                knowledge_content_hash=decision.knowledge_content_hash,
                knowledge_similarity=decision.knowledge_similarity,
                knowledge_similarity_margin=decision.knowledge_similarity_margin,
                multilingual_shadow=decision.multilingual_shadow,
                multilingual_contract_version=decision.multilingual_contract_version,
                multilingual_shadow_evidence=decision.multilingual_shadow_evidence,
                request_language_confidence=decision.request_language_confidence,
                request_language_source=decision.request_language_source,
                knowledge_top2_content_hash=decision.knowledge_top2_content_hash,
                knowledge_top2_similarity=decision.knowledge_top2_similarity,
                knowledge_match_status=decision.knowledge_match_status,
                knowledge_gate_version=decision.knowledge_gate_version,
                knowledge_min_similarity_threshold=decision.knowledge_min_similarity_threshold,
                knowledge_min_margin_threshold=decision.knowledge_min_margin_threshold,
                grounding_verified=decision.grounding_verified,
                grounding_verifier_version=decision.grounding_verifier_version,
                grounding_latency_ms=decision.grounding_latency_ms,
                state_version_at_decision=snapshot.state_version,
                decision_job_id=decision_job_id,
                decision_generation=decision_generation,
                decision_claim_token=decision_claim_token,
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
