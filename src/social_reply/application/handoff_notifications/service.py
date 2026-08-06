import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS, AccountPlatform
from social_reply.infrastructure.database import models


class HandoffNotificationError(ValueError):
    pass


def _card_state(work_status: str) -> str:
    if work_status in {"WAITING", "CLAIMED", "RESOLVED", "CANCELLED"}:
        return work_status
    raise HandoffNotificationError("human_work_item_status_invalid")


async def _route_values(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> dict[str, object | None]:
    config = await session.scalar(
        select(models.TenantFeishuHandoffConfig).where(
            models.TenantFeishuHandoffConfig.tenant_id == tenant_id
        )
    )
    if config is None:
        return {
            "notification_config_id": None,
            "config_version": None,
            "feishu_platform_account_id": None,
            "destination_chat_id": None,
            "status": "BLOCKED_CONFIG",
            "last_error_code": "FEISHU_HANDOFF_ROUTE_MISSING",
        }
    if not config.enabled:
        return {
            "notification_config_id": None,
            "config_version": None,
            "feishu_platform_account_id": None,
            "destination_chat_id": None,
            "status": "BLOCKED_CONFIG",
            "last_error_code": "FEISHU_HANDOFF_ROUTE_DISABLED",
        }
    account = await session.get(models.PlatformAccount, config.feishu_platform_account_id)
    if (
        account is None
        or account.tenant_id != tenant_id
        or account.platform != AccountPlatform.FEISHU
        or account.status != ACTIVE_ACCOUNT_STATUS
    ):
        return {
            "notification_config_id": None,
            "config_version": None,
            "feishu_platform_account_id": None,
            "destination_chat_id": None,
            "status": "BLOCKED_CONFIG",
            "last_error_code": "FEISHU_HANDOFF_ACCOUNT_INVALID",
        }
    return {
        "notification_config_id": config.id,
        "config_version": config.config_version,
        "feishu_platform_account_id": config.feishu_platform_account_id,
        "destination_chat_id": config.destination_chat_id,
        "status": "PENDING",
        "last_error_code": None,
    }


async def lock_handoff_notification_action(
    session: AsyncSession,
    *,
    work: models.HumanWorkItem,
    notification_public_id: uuid.UUID,
    expected_card_revision: int,
    expected_action_nonce: uuid.UUID,
) -> models.HandoffNotificationIntent:
    intent = await session.scalar(
        select(models.HandoffNotificationIntent)
        .where(models.HandoffNotificationIntent.human_work_item_id == work.id)
        .with_for_update()
    )
    if intent is None:
        raise HandoffNotificationError("handoff_notification_intent_not_found")
    if intent.tenant_id != work.tenant_id or intent.conversation_id != work.conversation_id:
        raise HandoffNotificationError("handoff_notification_intent_scope_mismatch")
    if (
        intent.public_id != notification_public_id
        or intent.desired_revision != expected_card_revision
        or intent.action_nonce != expected_action_nonce
    ):
        raise HandoffNotificationError("handoff_notification_action_stale")
    return intent


async def advance_handoff_notification_for_work(
    session: AsyncSession,
    *,
    work: models.HumanWorkItem,
) -> models.HandoffNotificationIntent | None:
    intent = await session.scalar(
        select(models.HandoffNotificationIntent)
        .where(models.HandoffNotificationIntent.human_work_item_id == work.id)
        .with_for_update()
    )
    if intent is None:
        return None
    if intent.tenant_id != work.tenant_id or intent.conversation_id != work.conversation_id:
        raise HandoffNotificationError("handoff_notification_intent_scope_mismatch")
    card_state = _card_state(work.status)
    if card_state == intent.desired_card_state:
        return intent
    intent.desired_card_state = card_state
    intent.desired_revision += 1
    intent.action_nonce = uuid.uuid4()
    intent.next_attempt_at = None
    if intent.status != "SENDING":
        if intent.provider_message_id is not None:
            intent.status = "PENDING"
        elif card_state in {"RESOLVED", "CANCELLED"}:
            intent.status = "CANCELLED"
    return intent


async def refresh_handoff_notification_route(
    session: AsyncSession,
    *,
    intent: models.HandoffNotificationIntent,
) -> bool:
    if intent.provider_message_id is not None:
        return False
    route_values = await _route_values(session, tenant_id=intent.tenant_id)
    for field, value in route_values.items():
        setattr(intent, field, value)
    if route_values["status"] == "PENDING":
        intent.next_attempt_at = None
        intent.last_error_message = None
        return True
    return False


async def ensure_handoff_notification_intent(
    session: AsyncSession,
    *,
    work: models.HumanWorkItem,
) -> models.HandoffNotificationIntent:
    conversation_tenant = await session.scalar(
        select(models.Conversation.tenant_id).where(models.Conversation.id == work.conversation_id)
    )
    if conversation_tenant is None:
        raise HandoffNotificationError("conversation_not_found")
    if work.tenant_id != conversation_tenant:
        raise HandoffNotificationError("human_work_item_tenant_mismatch")

    route_values = await _route_values(session, tenant_id=work.tenant_id)
    candidate_id = uuid.uuid4()
    inserted_id = (
        await session.execute(
            pg_insert(models.HandoffNotificationIntent)
            .values(
                id=candidate_id,
                public_id=uuid.uuid4(),
                tenant_id=work.tenant_id,
                human_work_item_id=work.id,
                conversation_id=work.conversation_id,
                provider_uuid=uuid.uuid4(),
                desired_card_state=_card_state(work.status),
                desired_revision=1,
                delivered_revision=0,
                action_nonce=uuid.uuid4(),
                attempt_count=0,
                **route_values,
            )
            .on_conflict_do_nothing(index_elements=["human_work_item_id"])
            .returning(models.HandoffNotificationIntent.id)
        )
    ).scalar_one_or_none()
    intent = (
        await session.get(models.HandoffNotificationIntent, inserted_id)
        if inserted_id is not None
        else await session.scalar(
            select(models.HandoffNotificationIntent).where(
                models.HandoffNotificationIntent.human_work_item_id == work.id
            )
        )
    )
    if intent is None:
        raise HandoffNotificationError("handoff_notification_intent_not_found")
    if intent.tenant_id != work.tenant_id or intent.conversation_id != work.conversation_id:
        raise HandoffNotificationError("handoff_notification_intent_scope_mismatch")
    return intent
