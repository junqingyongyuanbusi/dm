import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision import persist as persist_module
from social_reply.application.reply_decision.persist import (
    ChatwootDecisionDeferred,
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
    chatwoot_inbox_id=101,
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
            chatwoot_inbox_id=chatwoot_inbox_id,
            config=config or {},
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
            chatwoot_message_id=55,
            reply_target=reply_target or {},
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
    assert ob.destination_type == "chatwoot_conversation"


async def test_disabled_chatwoot_writes_decision_without_outbox(session, monkeypatch):
    account_id, conv_id, msg_id = await _seed(session)
    monkeypatch.setattr(
        persist_module,
        "get_settings",
        lambda: type("Settings", (), {"chatwoot_enabled": False})(),
    )
    decision = ReplyDecision(
        action=ReplyAction.AUTO_REPLY,
        reply_text="您好",
        reply_visibility=Visibility.PUBLIC,
    )
    with pytest.raises(ChatwootDecisionDeferred, match="chatwoot_disabled"):
        await persist_decision(
            session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
        )
    await session.rollback()

    assert (await session.execute(select(models.ReplyDecision))).first() is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None


async def test_disabled_chatwoot_defers_handoff_before_state_change(session, monkeypatch):
    account_id, conv_id, msg_id = await _seed(session)
    monkeypatch.setattr(
        persist_module,
        "get_settings",
        lambda: type("Settings", (), {"chatwoot_enabled": False})(),
    )
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))

    with pytest.raises(ChatwootDecisionDeferred, match="chatwoot_disabled"):
        await persist_decision(
            session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
        )
    await session.rollback()

    state = await session.scalar(
        select(models.AutomationState.state).where(
            models.AutomationState.conversation_id == conv_id
        )
    )
    assert state == "BOT_ACTIVE"
    assert (await session.execute(select(models.ReplyDecision))).first() is None


async def test_disabled_chatwoot_defers_before_existing_decision_shortcut(session, monkeypatch):
    account_id, conv_id, msg_id = await _seed(session)
    await session.execute(
        insert(models.ReplyDecision).values(
            tenant_id="default",
            conversation_id=conv_id,
            message_id=msg_id,
            action="ignore",
            reason_codes=["EXISTING"],
            source="rule",
        )
    )
    await session.commit()
    monkeypatch.setattr(
        persist_module,
        "get_settings",
        lambda: type("Settings", (), {"chatwoot_enabled": False})(),
    )

    with pytest.raises(ChatwootDecisionDeferred, match="chatwoot_disabled"):
        await persist_decision(
            session,
            _snap(conv_id, account_id),
            conv_id,
            msg_id,
            account_id,
            ReplyDecision(action=ReplyAction.IGNORE),
            "v0",
        )

    assert len((await session.execute(select(models.ReplyDecision))).scalars().all()) == 1


async def test_unconfigured_account_does_not_default_to_chatwoot(session):
    account_id, conv_id, msg_id = await _seed(session, chatwoot_inbox_id=None)
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")
    with pytest.raises(DecisionDeliveryConfigurationError, match="chatwoot_inbox_id_missing"):
        await persist_decision(
            session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
        )
    await session.rollback()

    assert (await session.execute(select(models.ReplyDecision))).first() is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None


async def test_unconfigured_handoff_does_not_change_state(session):
    account_id, conv_id, msg_id = await _seed(session, chatwoot_inbox_id=None)
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))

    with pytest.raises(DecisionDeliveryConfigurationError, match="chatwoot_inbox_id_missing"):
        await persist_decision(
            session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
        )
    await session.rollback()

    state = await session.scalar(
        select(models.AutomationState.state).where(
            models.AutomationState.conversation_id == conv_id
        )
    )
    assert state == "BOT_ACTIVE"
    assert (await session.execute(select(models.ReplyDecision))).first() is None


async def test_direct_account_still_creates_platform_outbox_when_chatwoot_disabled(
    session, monkeypatch
):
    account_id, conv_id, msg_id = await _seed(
        session,
        chatwoot_inbox_id=None,
        config={"delivery_mode": "direct"},
        reply_target={"kind": "dm", "chat_id": "9"},
    )
    monkeypatch.setattr(
        persist_module,
        "get_settings",
        lambda: type("Settings", (), {"chatwoot_enabled": False})(),
    )
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()

    assert outbox_id is not None
    outbox = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert outbox.destination_type == "telegram_dm"


async def test_handoff_writes_decision_no_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0"
    )
    await session.commit()
    assert outbox_id is None
    assert (await session.execute(select(models.OutboxMessage))).first() is None
    # handoff 把会话置 HANDOFF_PENDING
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"


async def test_draft_writes_private_outbox(session):
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
    assert outbox_id is not None
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


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
