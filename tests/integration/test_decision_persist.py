import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.reply_decision.persist import persist_decision
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision, Visibility
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed(session):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="acc", chatwoot_inbox_id=101))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key="telegram:x:9"))
    msg_id = uuid.uuid4()
    await session.execute(insert(models.Message).values(
        id=msg_id, conversation_id=conv_id, direction="inbound", sender_type="contact",
        text="hi", chatwoot_message_id=55))
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conv_id, msg_id


def _snap(conv_id, account_id, state="BOT_ACTIVE", version=1):
    return DecisionSnapshot(
        text="hi", platform="telegram", brand_id="b1", account_id=str(account_id),
        conversation_key="telegram:x:9", automation_state=state, state_version=version,
    )


async def test_auto_reply_writes_decision_and_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好",
                             reply_visibility=Visibility.PUBLIC, reason_codes=("STUB_LLM",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0")
    await session.commit()
    assert outbox_id is not None
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "auto_reply" and dec.outbox_id == outbox_id
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "PENDING" and ob.payload["text"] == "您好"
    assert ob.message_type == "text"


async def test_handoff_writes_decision_no_outbox(session):
    account_id, conv_id, msg_id = await _seed(session)
    decision = ReplyDecision(action=ReplyAction.HANDOFF, reason_codes=("RISK_WORD",))
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id), conv_id, msg_id, account_id, decision, "v0")
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
        session, _snap(conv_id, account_id, state="BOT_DRAFT_ONLY"), conv_id, msg_id,
        account_id, decision, "v0")
    await session.commit()
    assert outbox_id is not None
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


async def test_cas_fails_when_state_version_moved(session):
    # 决策快照 version=1，但会话已被翻转（version=2）→ auto_reply 不写 outbox
    account_id, conv_id, msg_id = await _seed(session)
    from social_reply.domain.automation.state_machine import flip_to_human_active
    await flip_to_human_active(session, conv_id, "3", "agent_public_reply")  # version→2
    await session.commit()
    decision = ReplyDecision(action=ReplyAction.AUTO_REPLY, reply_text="您好")
    outbox_id = await persist_decision(
        session, _snap(conv_id, account_id, version=1), conv_id, msg_id, account_id, decision, "v0")
    await session.commit()
    assert outbox_id is None  # CAS 落空
    assert (await session.execute(select(models.OutboxMessage))).first() is None
