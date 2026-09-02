import uuid

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.reply_decision.persist import (
    DecisionDeliveryConfigurationError,
    persist_decision,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed(
    session,
    *,
    config: dict | None = None,
    reply_target: dict | None = None,
):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="acc",
            config={"delivery_mode": "direct"} if config is None else config,
            capability={"dm": True, "max_text_length": 4096},
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:x:9",
        )
    )
    msg_id = uuid.uuid4()
    await session.execute(
        insert(models.Message).values(
            id=msg_id,
            conversation_id=conv_id,
            direction="inbound",
            sender_type="contact",
            text="hi",
            platform_message_id="55",
            reply_target=reply_target or {"kind": "dm", "chat_id": "9"},
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conv_id, msg_id


def _snap(conv_id, account_id, state="BOT_ACTIVE", version=1):
    return DecisionSnapshot(
        text="hi",
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key="telegram:x:9",
        automation_state=state,
        state_version=version,
    )


async def test_auto_reply_writes_decision_and_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="您好",
        reply_visibility=Visibility.PUBLIC,
        reason_codes=("STUB_LLM",),
    )
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()
    assert outbox_id is not None
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "auto_reply" and dec.outbox_id == outbox_id
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING" and ob.payload["text"] == "您好"
    assert ob.message_type == "text"
    assert ob.destination_type == "telegram_dm"
    assert ob.payload["target"] == {"kind": "dm", "chat_id": "9"}


async def test_private_x_post_reply_is_rejected_before_outbox_creation(session):
    account_id, conv_id, msg_id = await _seed(
        session,
        config={"delivery_mode": "direct"},
        reply_target={"kind": "reply", "in_reply_to_post_id": "post-1"},
    )
    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(
            platform="x",
            capability={"mentions": True, "max_text_length": 280},
        )
    )
    await session.commit()
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="private detail",
        reply_visibility=Visibility.PRIVATE,
    )

    with pytest.raises(
        DecisionDeliveryConfigurationError,
        match="x_post_reply_requires_public_visibility",
    ):
        await persist_decision(
            session,
            _snap(conv_id, account_id),
            conv_id,
            msg_id,
            account_id,
            decision,
            "v0",
        )
    await session.rollback()
    assert (await session.execute(select(models.OutboxMessage))).first() is None


@pytest.mark.parametrize("delivery_mode", [None, "", "chatwoot", "unknown"])
async def test_unsupported_delivery_mode_fails_before_decision_or_outbox(
    session,
    delivery_mode,
):
    config = {} if delivery_mode is None else {"delivery_mode": delivery_mode}
    account_id, conv_id, msg_id = await _seed(session, config=config)
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")

    with pytest.raises(
        DecisionDeliveryConfigurationError,
        match="delivery_mode_unsupported",
    ):
        await persist_decision(
            session,
            _snap(conv_id, account_id),
            conv_id,
            msg_id,
            account_id,
            decision,
            "v0",
        )
    await session.rollback()

    assert (await session.execute(select(models.ReplyDecision))).first() is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None


async def test_handoff_writes_decision_no_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()
    assert outbox_id is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"
    work = (await session.execute(select(models.HumanWorkItem))).scalar_one()
    intent = (await session.execute(select(models.HandoffNotificationIntent))).scalar_one()
    assert intent.human_work_item_id == work.id
    assert intent.conversation_id == conv_id
    assert intent.status == "BLOCKED_CONFIG"
    assert intent.last_error_code == "FEISHU_HANDOFF_ROUTE_MISSING"


async def test_handoff_snapshots_enabled_feishu_notification_route(session):
    account_id, conv_id, msg_id = await _seed(session)
    feishu_account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=feishu_account_id,
            tenant_id="default",
            brand_id="b1",
            platform="feishu",
            name="support notifications",
            status="active",
            config={"feishu_health_status": "READY"},
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
        )
    )
    config_id = uuid.uuid4()
    await session.execute(
        insert(models.TenantFeishuHandoffConfig).values(
            id=config_id,
            tenant_id="default",
            feishu_platform_account_id=feishu_account_id,
            destination_chat_id="oc_support",
            enabled=True,
            config_version=3,
        )
    )
    await session.commit()

    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()

    intent = (await session.execute(select(models.HandoffNotificationIntent))).scalar_one()
    assert intent.status == "PENDING"
    assert intent.notification_config_id == config_id
    assert intent.config_version == 3
    assert intent.feishu_platform_account_id == feishu_account_id
    assert intent.destination_chat_id == "oc_support"
    assert intent.last_error_code is None


async def test_direct_draft_persists_without_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.DRAFT, reply_text="草稿供参考")
    outbox_id = await persist_decision(
        session,
        _snap(conv_id, account_id, state="BOT_DRAFT_ONLY"),
        conv_id,
        msg_id,
        account_id,
        decision,
        "v0",
    )
    await session.commit()
    assert outbox_id is None
    decision_row = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert decision_row.action == "draft"
    assert decision_row.outbox_id is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None


async def test_duplicate_persist_returns_existing_decision_and_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="您好",
        reply_visibility=Visibility.PUBLIC,
        reason_codes=("STUB_LLM",),
    )
    first = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()
    second = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()

    assert second == first
    decisions = (await session.execute(select(models.ReplyDecision))).scalars().all()
    outboxes = (await session.execute(select(models.OutboxMessage))).scalars().all()
    assert len(decisions) == 1
    assert len(outboxes) == 1


async def test_cas_fails_when_state_version_moved(session):
    # 决策快照 version=1，但会话已被翻转（version=2）→ auto_reply 不写 outbox
    account_id, conv_id, msg_id = await _seed(session)
    from social_reply.domain.automation.state_machine import flip_to_human_active

    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")  # version→2
    await session.commit()
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id, version=1), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()
    assert outbox_id is None  # CAS 落空
    assert (await session.execute(select(models.OutboxMessage))).first() is None
