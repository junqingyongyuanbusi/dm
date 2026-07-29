import uuid

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.domain.messages.canonical import CanonicalEvent
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("match_by_platform_message_id", [False, True])
async def test_managed_x_outbox_echo_is_ignored(
    session,
    match_by_platform_message_id,
):
    source_account_id = uuid.uuid4()
    target_account_id = uuid.uuid4()
    source_contact_id = uuid.uuid4()
    source_conversation_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    external_event_id = "2080000000000000000" if match_by_platform_message_id else str(outbox_id)

    for account_id, external_account_id, public_id in (
        (source_account_id, "managed-source", "x_source"),
        (target_account_id, "managed-target", "x_target"),
    ):
        await session.execute(
            insert(models.PlatformAccount).values(
                id=account_id,
                tenant_id="default",
                brand_id="default",
                platform="x",
                name=public_id,
                external_account_id=external_account_id,
                public_id=public_id,
                credential_bundle=encrypt_secret_bundle(
                    {
                        "consumer_key": "ck",
                        "consumer_secret": "cs",
                        "access_token": "at",
                        "access_token_secret": "ats",
                    }
                ),
                config={"delivery_mode": "direct"},
                capability={"dm": True, "x_chat": True},
                automation_default="BOT_ACTIVE",
                status="active",
            )
        )
    await session.execute(
        insert(models.Contact).values(
            id=source_contact_id,
            tenant_id="default",
            platform="x",
            platform_account_id=source_account_id,
            external_user_id="managed-target",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=source_conversation_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            platform_account_id=source_account_id,
            contact_id=source_contact_id,
            conversation_key="x_chat:source:managed-source:managed-target",
            channel_type="dm",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="default",
            conversation_id=source_conversation_id,
            platform_account_id=source_account_id,
            destination_type="x_chat_message",
            destination_id="managed-target",
            message_type="text",
            payload={"text": "managed reply"},
            idempotency_key=f"managed-echo:{outbox_id}",
            status="SENDING",
            platform_message_id=(external_event_id if match_by_platform_message_id else None),
        )
    )
    raw_event_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="x",
            payload={"data": {"event_type": "chat.received"}},
            headers={},
            processing_status="XCHAT_DECRYPTION_PENDING",
        )
    )
    await session.commit()

    result = await ingest_canonical_event(
        CanonicalEvent(
            platform="x",
            platform_account_key=str(target_account_id),
            external_event_id=external_event_id,
            external_user_id="managed-source",
            conversation_key="x_chat:target:managed-source:managed-target",
            text="managed reply",
            reply_target={"kind": "x_chat"},
        ),
        raw_event_id=raw_event_id,
    )

    assert result is None
    session.expire_all()
    raw = await session.get(models.RawEvent, raw_event_id)
    assert raw.processing_status == "IGNORED_MANAGED_OUTBOX_ECHO"
    assert await session.scalar(select(func.count()).select_from(models.NormalizedEvent)) == 0
    assert await session.scalar(select(func.count()).select_from(models.Message)) == 0
    assert await session.scalar(select(func.count()).select_from(models.DecisionJob)) == 0
    audit = (
        await session.execute(
            select(models.AuditLog).where(models.AuditLog.action == "managed_x_outbox_echo_ignored")
        )
    ).scalar_one()
    assert audit.detail["source_account_id"] == str(source_account_id)
    assert audit.detail["target_account_id"] == str(target_account_id)
    assert audit.detail["source_outbox_id"] == str(outbox_id)
