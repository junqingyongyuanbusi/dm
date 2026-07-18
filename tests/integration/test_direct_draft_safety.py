import uuid

from sqlalchemy import insert, select

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


async def test_direct_platform_draft_never_creates_customer_outbox(migrated_db, session):
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            name="bot",
            external_account_id="42",
            public_id="tg_draft_safety",
            credential_bundle=encrypt_secret_bundle({"bot_token": "not-read"}),
            config={"delivery_mode": "direct"},
            capability={"dm": True},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="tenant-a",
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:account:user-1",
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id, state="BOT_DRAFT_ONLY", state_version=1
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="hello",
            reply_target={"chat_id": 1},
        )
    )
    await session.commit()

    snapshot = DecisionSnapshot(
        tenant_id="tenant-a",
        brand_id="brand-a",
        platform="telegram",
        account_id=str(account_id),
        conversation_key="telegram:account:user-1",
        text="hello",
        automation_state="BOT_DRAFT_ONLY",
        state_version=1,
    )
    outbox_id = await persist_decision(
        session,
        snapshot,
        conversation_id,
        message_id,
        account_id,
        ReplyDecision(action=ReplyAction.DRAFT, reply_text="draft only"),
        "v0",
    )
    await session.commit()
    assert outbox_id is None
    decision = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision.action == "draft"
    assert (await session.execute(select(models.OutboxMessage))).first() is None
