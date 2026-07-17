import pytest
from sqlalchemy import select
from tests.integration.conftest import (
    chatwoot_payload,
    count_rows,
    seed_chatwoot_account,
    seed_raw_event,
)

from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def test_bot_active_inbound_fast_path_sends_outbox(session):
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(await seed_raw_event(session, chatwoot_payload()))
    assert await count_rows(session, models.ReplyDecision) == 1
    job = (await session.execute(select(models.DecisionJob))).scalar_one()
    assert job.status == "COMPLETED" and job.attempt_count == 1
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.status == "SENT" and ob.message_type == "text"
    assert ob.chatwoot_message_id is not None


async def test_draft_only_inbound_produces_private_note_outbox(session):
    await seed_chatwoot_account(session, "BOT_DRAFT_ONLY")
    await process_raw_event(await seed_raw_event(session, chatwoot_payload()))
    ob = (await session.execute(select(models.OutboxMessage))).scalar_one()
    assert ob.message_type == "private_note"


async def test_risk_word_inbound_handoff_no_outbox(session):
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload(content="我要起诉，无法出金"))
    )
    assert await count_rows(session, models.OutboxMessage) == 0
    dec = (await session.execute(select(models.ReplyDecision))).scalar_one()
    assert dec.action == "handoff"
    st = (await session.execute(select(models.AutomationState))).scalar_one()
    assert st.state == "HANDOFF_PENDING"


async def test_agent_reply_does_not_trigger_decision(session):
    # 坐席公开回复只触发 HUMAN_ACTIVE，不产生决策/outbox
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload())
    )  # 先建会话 + 1 decision
    await process_raw_event(
        await seed_raw_event(
            session,
            chatwoot_payload(id=56, message_type="outgoing", sender={"id": 3, "type": "user"}),
        )
    )
    assert await count_rows(session, models.ReplyDecision) == 1  # 仍是 1，坐席回复不决策
    assert await count_rows(session, models.DecisionJob) == 1


async def test_decision_failure_is_persisted_for_retry(session, monkeypatch):
    await seed_chatwoot_account(session, "BOT_ACTIVE")

    async def fail(*args, **kwargs):
        raise RuntimeError("temporary decision failure")

    monkeypatch.setattr(
        "social_reply.application.reply_decision.jobs.run_and_persist_decision", fail
    )
    await process_raw_event(await seed_raw_event(session, chatwoot_payload()))

    session.expire_all()
    job = (await session.execute(select(models.DecisionJob))).scalar_one()
    assert job.status == "FAILED"
    assert job.attempt_count == 1
    assert job.next_attempt_at is not None
    assert "temporary decision failure" in job.last_error
    assert await count_rows(session, models.ReplyDecision) == 0
