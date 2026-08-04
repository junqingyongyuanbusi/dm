import pytest
from sqlalchemy import select
from tests.integration.conftest import (
    chatwoot_payload,
    count_rows,
    seed_chatwoot_account,
    seed_raw_event,
)

from social_reply.application.account_management.human_workflow import (
    claim_human_work_item,
    resolve_human_work_item,
)
from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.application.reply_decision.jobs import process_decision_job
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


async def test_inbound_during_handoff_stays_ignored_and_only_new_post_resolve_message_runs(
    session,
):
    await seed_chatwoot_account(session, "BOT_ACTIVE")
    await process_raw_event(
        await seed_raw_event(
            session,
            chatwoot_payload(id=55, content="我要起诉，无法出金"),
        )
    )
    work = (await session.execute(select(models.HumanWorkItem))).scalar_one()

    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload(id=56, content="还在吗？"))
    )
    waiting_message = (
        await session.execute(
            select(models.Message).where(models.Message.chatwoot_message_id == 56)
        )
    ).scalar_one()
    waiting_job = (
        await session.execute(
            select(models.DecisionJob).where(models.DecisionJob.message_id == waiting_message.id)
        )
    ).scalar_one()
    waiting_decision = (
        await session.execute(
            select(models.ReplyDecision).where(
                models.ReplyDecision.message_id == waiting_message.id
            )
        )
    ).scalar_one()
    assert waiting_job.status == "COMPLETED"
    assert waiting_decision.action == "ignore"
    assert waiting_decision.reason_codes == ["HANDOFF_PENDING"]
    assert waiting_decision.outbox_id is None
    assert await count_rows(session, models.OutboxMessage) == 0

    await claim_human_work_item(
        work_item_id=work.id,
        allowed_tenants=frozenset({"default"}),
        actor="user:alice",
        user_id=None,
        expected_version=work.version,
    )
    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload(id=57, content="我补充一下"))
    )
    claimed_message = (
        await session.execute(
            select(models.Message).where(models.Message.chatwoot_message_id == 57)
        )
    ).scalar_one()
    claimed_job = (
        await session.execute(
            select(models.DecisionJob).where(models.DecisionJob.message_id == claimed_message.id)
        )
    ).scalar_one()
    claimed_decision = (
        await session.execute(
            select(models.ReplyDecision).where(
                models.ReplyDecision.message_id == claimed_message.id
            )
        )
    ).scalar_one()
    assert claimed_job.status == "COMPLETED"
    assert claimed_decision.action == "ignore"
    assert claimed_decision.reason_codes == ["HUMAN_ACTIVE"]
    assert claimed_decision.outbox_id is None
    assert await count_rows(session, models.OutboxMessage) == 0
    ignored_message_ids = [waiting_message.id, claimed_message.id]

    await resolve_human_work_item(
        work_item_id=work.id,
        allowed_tenants=frozenset({"default"}),
        actor="user:alice",
        expected_version=work.version + 1,
        allow_override=False,
    )
    assert await process_decision_job(str(waiting_job.id)) is False
    assert await process_decision_job(str(claimed_job.id)) is False
    assert await count_rows(session, models.OutboxMessage) == 0

    await process_raw_event(
        await seed_raw_event(session, chatwoot_payload(id=58, content="新的问题"))
    )
    session.expire_all()
    old_decisions = (
        (
            await session.execute(
                select(models.ReplyDecision).where(
                    models.ReplyDecision.message_id.in_(ignored_message_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(old_decisions) == 2
    assert all(
        decision.action == "ignore" and decision.outbox_id is None for decision in old_decisions
    )
    new_message = (
        await session.execute(
            select(models.Message).where(models.Message.chatwoot_message_id == 58)
        )
    ).scalar_one()
    new_decision = (
        await session.execute(
            select(models.ReplyDecision).where(models.ReplyDecision.message_id == new_message.id)
        )
    ).scalar_one()
    assert new_decision.action == "auto_reply"
    assert new_decision.outbox_id is not None
    assert await count_rows(session, models.OutboxMessage) == 1


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


async def test_context_scope_failure_requires_review_without_retry(session, monkeypatch):
    from social_reply.application.reply_decision.runner import DecisionContextScopeError

    await seed_chatwoot_account(session, "BOT_ACTIVE")

    async def fail(*_args, **_kwargs):
        raise DecisionContextScopeError("decision_context_scope_mismatch")

    monkeypatch.setattr(
        "social_reply.application.reply_decision.jobs.run_and_persist_decision", fail
    )
    await process_raw_event(await seed_raw_event(session, chatwoot_payload()))

    session.expire_all()
    job = (await session.execute(select(models.DecisionJob))).scalar_one()
    assert job.status == "NEEDS_REVIEW"
    assert job.next_attempt_at is None
    assert job.last_error == "decision_context_scope_mismatch"
    raw = (await session.execute(select(models.RawEvent))).scalar_one()
    assert raw.processing_status == "DECISION_NEEDS_REVIEW"
    assert await count_rows(session, models.ReplyDecision) == 0


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
