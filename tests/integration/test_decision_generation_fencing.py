import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management import human_workflow
from social_reply.application.account_management.human_workflow import resume_bot, send_human_reply
from social_reply.application.event_ingestion import processor as chatwoot_processor
from social_reply.application.event_ingestion.direct import ingest_canonical_event
from social_reply.application.event_ingestion.processor import process_raw_event
from social_reply.application.reply_decision import jobs as decision_jobs
from social_reply.application.reply_decision import runner as decision_runner
from social_reply.application.reply_decision.jobs import (
    process_decision_job,
    reserve_conversation_generation,
    reserve_decision_job,
)
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.application.reply_decision.runner import run_and_persist_decision
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.domain.messages.canonical import CanonicalEvent
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_engine

pytestmark = pytest.mark.integration


async def _seed_conversation(session):
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="generation-test",
            chatwoot_inbox_id=101,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=uuid.uuid4().hex,
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:generation:{conversation_id}",
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conversation_id


async def _seed_generation_input(
    session, account_id, conversation_id, *, raw_event_id=None, text="hi"
):
    raw_event_id = raw_event_id or uuid.uuid4()
    message_id = uuid.uuid4()
    await session.execute(
        insert(models.RawEvent).values(
            id=raw_event_id,
            source="chatwoot",
            payload={},
            processing_status="PROCESSED",
        )
    )
    decision_generation = await reserve_conversation_generation(session, conversation_id)
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text=text,
            decision_generation=decision_generation,
        )
    )
    conversation = await session.get(models.Conversation, conversation_id)
    snapshot = DecisionSnapshot(
        text=text,
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key=conversation.conversation_key,
        automation_state="BOT_ACTIVE",
        state_version=1,
    )
    await session.commit()
    return message_id, raw_event_id, snapshot


async def _reserve(session, account_id, conversation_id, *, raw_event_id=None, text="hi"):
    message_id, raw_event_id, snapshot = await _seed_generation_input(
        session,
        account_id,
        conversation_id,
        raw_event_id=raw_event_id,
        text=text,
    )
    job_id = await reserve_decision_job(
        session,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
        account_id=account_id,
        snapshot=snapshot,
    )
    await session.commit()
    return job_id, message_id, raw_event_id, snapshot


async def _prepare_human_action(session, conversation_id, action):
    state = await session.get(models.AutomationState, conversation_id)
    state.state = "HUMAN_ACTIVE" if action == "resume" else "BOT_ACTIVE"
    state.state_version += 1
    await session.commit()


async def _run_human_action(action, conversation_id, message_id):
    if action == "resume":
        await resume_bot(
            conversation_id=conversation_id,
            allowed_tenants=frozenset({"default"}),
            actor="user:reviewer",
            target="BOT_DRAFT_ONLY",
        )
        return
    await send_human_reply(
        conversation_id=conversation_id,
        reply_to_message_id=message_id,
        text="Human reply wins",
        idempotency_key=f"generation-lock-order:{conversation_id}",
        allowed_tenants=frozenset({"default"}),
        actor="user:reviewer",
        user_id=None,
        allow_override=True,
    )


async def _assert_human_action(session, conversation_id, action):
    session.expire_all()
    state = await session.get(models.AutomationState, conversation_id)
    assert state.state == ("BOT_DRAFT_ONLY" if action == "resume" else "HUMAN_ACTIVE")
    if action == "reply":
        manual_outbox = (
            await session.execute(
                select(models.OutboxMessage).where(
                    models.OutboxMessage.conversation_id == conversation_id,
                    models.OutboxMessage.origin_kind == "MANUAL_REPLY",
                )
            )
        ).scalar_one()
        assert manual_outbox.actor_kind == "ADMIN_HUMAN"
        assert manual_outbox.payload["text"] == "Human reply wins"


async def test_new_generation_supersedes_job_and_cancels_stale_bot_outbox(session):
    account_id, conversation_id = await _seed_conversation(session)
    first_job_id, first_message_id, _raw_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )
    outbox_id = uuid.uuid4()
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="chatwoot_conversation",
            destination_id="conversation",
            message_type="text",
            payload={"text": "stale"},
            reply_to_message_id=first_message_id,
            origin_kind="DECISION",
            actor_kind="BOT",
            idempotency_key=uuid.uuid4().hex,
            status="PENDING",
        )
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=first_message_id,
            action="auto_reply",
            reply_text="stale",
            reason_codes=[],
            source="llm",
            decision_job_id=first_job_id,
            decision_generation=1,
            outbox_id=outbox_id,
        )
    )
    await session.commit()

    second_job_id, _message_id, _raw_id, _snapshot = await _reserve(
        session, account_id, conversation_id, text="newer"
    )

    session.expire_all()
    conversation = await session.get(models.Conversation, conversation_id)
    first_job = await session.get(models.DecisionJob, first_job_id)
    second_job = await session.get(models.DecisionJob, second_job_id)
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert conversation.decision_generation == 2
    assert first_job.status == "SUPERSEDED"
    assert second_job.decision_generation == 2
    assert outbox.status == "CANCELLED"
    assert outbox.last_error_code == "STALE_CONVERSATION_INPUT"


async def test_worker_returning_after_new_generation_cannot_persist(session, monkeypatch):
    account_id, conversation_id = await _seed_conversation(session)
    first_job_id, _message_id, _raw_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_run(*args, **kwargs):
        started.set()
        await release.wait()
        return await run_and_persist_decision(*args, **kwargs)

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", delayed_run)
    worker = asyncio.create_task(process_decision_job(str(first_job_id)))
    await asyncio.wait_for(started.wait(), timeout=2)
    await _reserve(session, account_id, conversation_id, text="newer")
    release.set()

    assert await worker is False
    session.expire_all()
    first_job = await session.get(models.DecisionJob, first_job_id)
    assert first_job.status == "SUPERSEDED"
    assert (
        await session.execute(
            select(models.ReplyDecision).where(models.ReplyDecision.decision_job_id == first_job_id)
        )
    ).first() is None


async def test_claim_token_is_random_for_each_retry(session, monkeypatch):
    account_id, conversation_id = await _seed_conversation(session)
    job_id, _message_id, _raw_id, _snapshot = await _reserve(session, account_id, conversation_id)
    tokens = []

    async def fail(*_args, **kwargs):
        tokens.append(kwargs["claim_token"])
        raise RuntimeError("retry")

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", fail)
    with pytest.raises(RuntimeError, match="retry"):
        await process_decision_job(str(job_id))
    await session.execute(
        update(models.DecisionJob)
        .where(models.DecisionJob.id == job_id)
        .values(next_attempt_at=datetime.now(UTC))
    )
    await session.commit()
    with pytest.raises(RuntimeError, match="retry"):
        await process_decision_job(str(job_id))

    assert len(tokens) == 2
    assert tokens[0] != tokens[1]


async def test_job_finalization_persists_provenance_and_raw_state_atomically(session):
    account_id, conversation_id = await _seed_conversation(session)
    job_id, _message_id, raw_event_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )

    assert await process_decision_job(str(job_id)) is True
    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    decision = (
        await session.execute(
            select(models.ReplyDecision).where(models.ReplyDecision.decision_job_id == job_id)
        )
    ).scalar_one()
    raw_event = await session.get(models.RawEvent, raw_event_id)
    assert job.status == "COMPLETED"
    assert job.claim_token is None
    assert decision.decision_generation == job.decision_generation == 1
    assert decision.decision_claim_token is not None
    assert raw_event.processing_status == "PROCESSED"


async def test_direct_ingestion_commits_new_generation_while_older_llm_is_blocked(
    session, monkeypatch
):
    account_id, conversation_id = await _seed_conversation(session)
    conversation = await session.get(models.Conversation, conversation_id)
    contact = await session.get(models.Contact, conversation.contact_id)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class ControlledLLM:
        async def decide(self, context):
            if context.text == "m1":
                first_started.set()
                await release_first.wait()
            return ReplyDecision(action=ReplyAction.IGNORE, source="llm")

    class EnabledKillSwitch:
        async def is_disabled(self, *_args):
            return False

    from social_reply.application.reply_decision import runner

    monkeypatch.setattr(runner, "_llm", ControlledLLM())
    monkeypatch.setattr(runner, "_make_killswitch", lambda: EnabledKillSwitch())

    def event(external_event_id: str, text: str) -> CanonicalEvent:
        return CanonicalEvent(
            platform="telegram",
            platform_account_key=str(account_id),
            external_event_id=external_event_id,
            external_user_id=contact.external_user_id,
            conversation_key=conversation.conversation_key,
            text=text,
        )

    first_ingestion = asyncio.create_task(ingest_canonical_event(event("m1", "m1")))
    await asyncio.wait_for(first_started.wait(), timeout=2)

    second_job_id = await asyncio.wait_for(ingest_canonical_event(event("m2", "m2")), timeout=2)
    assert not first_ingestion.done()
    release_first.set()
    first_job_id = await asyncio.wait_for(first_ingestion, timeout=2)

    session.expire_all()
    first_job = await session.get(models.DecisionJob, first_job_id)
    second_job = await session.get(models.DecisionJob, second_job_id)
    decisions = (
        (
            await session.execute(
                select(models.ReplyDecision).order_by(models.ReplyDecision.created_at)
            )
        )
        .scalars()
        .all()
    )
    first_message = await session.get(models.Message, first_job.message_id)
    second_message = await session.get(models.Message, second_job.message_id)
    assert first_job.status == "SUPERSEDED"
    assert second_job.status == "COMPLETED"
    assert first_job.decision_generation == first_message.decision_generation == 1
    assert second_job.decision_generation == second_message.decision_generation == 2
    assert [decision.decision_job_id for decision in decisions] == [second_job_id]
    assert await session.scalar(select(func.count()).select_from(models.OutboxMessage)) == 0


async def test_chatwoot_ingestion_commits_new_generation_while_older_llm_is_blocked(
    session, monkeypatch
):
    account_id, _conversation_id = await _seed_conversation(session)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class ControlledLLM:
        async def decide(self, context):
            if context.text == "m1":
                first_started.set()
                await release_first.wait()
            return ReplyDecision(action=ReplyAction.IGNORE, source="llm")

    class EnabledKillSwitch:
        async def is_disabled(self, *_args):
            return False

    from social_reply.application.reply_decision import runner

    monkeypatch.setattr(runner, "_llm", ControlledLLM())
    monkeypatch.setattr(runner, "_make_killswitch", lambda: EnabledKillSwitch())

    def payload(message_id: int, content: str) -> dict:
        return {
            "event": "message_created",
            "id": message_id,
            "content": content,
            "message_type": "incoming",
            "private": False,
            "created_at": "2026-07-31T00:00:00Z",
            "sender": {"id": 9, "type": "contact"},
            "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
            "account": {"id": 1},
        }

    raw_ids = []
    for message_id, content in ((1001, "m1"), (1002, "m2")):
        raw_ids.append(
            (
                await session.execute(
                    insert(models.RawEvent)
                    .values(source="chatwoot", payload=payload(message_id, content))
                    .returning(models.RawEvent.id)
                )
            ).scalar_one()
        )
    await session.commit()

    first_ingestion = asyncio.create_task(process_raw_event(str(raw_ids[0])))
    await asyncio.wait_for(first_started.wait(), timeout=2)
    await asyncio.wait_for(process_raw_event(str(raw_ids[1])), timeout=2)
    assert not first_ingestion.done()
    release_first.set()
    await asyncio.wait_for(first_ingestion, timeout=2)

    session.expire_all()
    jobs = (
        (
            await session.execute(
                select(models.DecisionJob)
                .where(models.DecisionJob.raw_event_id.in_(raw_ids))
                .order_by(models.DecisionJob.decision_generation)
            )
        )
        .scalars()
        .all()
    )
    messages = [await session.get(models.Message, job.message_id) for job in jobs]
    assert [job.status for job in jobs] == ["SUPERSEDED", "COMPLETED"]
    assert [job.decision_generation for job in jobs] == [1, 2]
    assert [message.decision_generation for message in messages] == [1, 2]
    decisions = (
        (
            await session.execute(
                select(models.ReplyDecision).where(
                    models.ReplyDecision.decision_job_id.in_([job.id for job in jobs])
                )
            )
        )
        .scalars()
        .all()
    )
    assert [decision.decision_job_id for decision in decisions] == [jobs[1].id]


@pytest.mark.parametrize("human_action", ["resume", "reply"])
async def test_chatwoot_ingestion_cannot_deadlock_with_human_action(
    session, monkeypatch, human_action
):
    account_id, conversation_id = await _seed_conversation(session)
    reply_to_message_id = uuid.uuid4()
    await session.execute(
        insert(models.ConversationMapping).values(
            chatwoot_account_id=1,
            chatwoot_conversation_id=77,
            conversation_id=conversation_id,
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=reply_to_message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="Earlier message",
        )
    )
    raw_event_id = (
        await session.execute(
            insert(models.RawEvent)
            .values(
                source="chatwoot",
                payload={
                    "event": "message_created",
                    "id": 2001,
                    "content": "Concurrent inbound",
                    "message_type": "incoming",
                    "private": False,
                    "created_at": "2026-07-31T00:00:00Z",
                    "sender": {"id": 9, "type": "contact"},
                    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
                    "account": {"id": 1},
                },
            )
            .returning(models.RawEvent.id)
        )
    ).scalar_one()
    await session.commit()
    await _prepare_human_action(session, conversation_id, human_action)

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)
    monkeypatch.setattr(decision_jobs, "process_decision_job", no_dispatch)

    original_chatwoot_lock = chatwoot_processor.acquire_conversation_delivery_xact_lock
    chatwoot_has_lock = asyncio.Event()
    release_chatwoot = asyncio.Event()

    async def paused_chatwoot_lock(lock_session, locked_conversation_id):
        await original_chatwoot_lock(lock_session, locked_conversation_id)
        chatwoot_has_lock.set()
        await release_chatwoot.wait()

    monkeypatch.setattr(
        chatwoot_processor,
        "acquire_conversation_delivery_xact_lock",
        paused_chatwoot_lock,
    )
    original_store_message = chatwoot_processor._store_message

    async def observed_store_message(*args, **kwargs):
        assert chatwoot_has_lock.is_set()
        return await original_store_message(*args, **kwargs)

    monkeypatch.setattr(chatwoot_processor, "_store_message", observed_store_message)

    original_human_lock = human_workflow.acquire_conversation_delivery_xact_lock
    human_waiting = asyncio.Event()

    async def observed_human_lock(lock_session, locked_conversation_id):
        human_waiting.set()
        await original_human_lock(lock_session, locked_conversation_id)

    monkeypatch.setattr(
        human_workflow, "acquire_conversation_delivery_xact_lock", observed_human_lock
    )

    chatwoot_task = asyncio.create_task(process_raw_event(str(raw_event_id)))
    human_task = None
    try:
        await asyncio.wait_for(chatwoot_has_lock.wait(), timeout=2)
        human_task = asyncio.create_task(
            _run_human_action(human_action, conversation_id, reply_to_message_id)
        )
        await asyncio.wait_for(human_waiting.wait(), timeout=2)
        release_chatwoot.set()
        await asyncio.wait_for(asyncio.gather(chatwoot_task, human_task), timeout=4)
    finally:
        release_chatwoot.set()
        for task in (chatwoot_task, human_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (chatwoot_task, human_task) if task is not None),
            return_exceptions=True,
        )

    session.expire_all()
    raw_event = await session.get(models.RawEvent, raw_event_id)
    conversation = await session.get(models.Conversation, conversation_id)
    job = (
        await session.execute(
            select(models.DecisionJob).where(models.DecisionJob.raw_event_id == raw_event_id)
        )
    ).scalar_one()
    stored_message = await session.get(models.Message, job.message_id)
    assert raw_event.processing_status == "PROCESSED"
    assert stored_message.text == "Concurrent inbound"
    assert job.status == "PENDING"
    assert stored_message.decision_generation == 1
    assert job.decision_generation == conversation.decision_generation == 1
    await _assert_human_action(session, conversation_id, human_action)


async def test_claimed_chatwoot_ingestion_locks_conversation_before_raw_event(session, monkeypatch):
    account_id, conversation_id = await _seed_conversation(session)
    await session.execute(
        insert(models.ConversationMapping).values(
            chatwoot_account_id=1,
            chatwoot_conversation_id=77,
            conversation_id=conversation_id,
        )
    )
    claim_token = uuid.uuid4()
    raw_event_id = (
        await session.execute(
            insert(models.RawEvent)
            .values(
                tenant_id="default",
                platform_account_id=account_id,
                source="chatwoot",
                payload={
                    "event": "message_created",
                    "id": 2002,
                    "content": "Claimed inbound",
                    "message_type": "incoming",
                    "private": False,
                    "created_at": "2026-07-31T00:00:00Z",
                    "sender": {"id": 9, "type": "contact"},
                    "conversation": {"id": 77, "inbox_id": 101, "status": "pending"},
                    "account": {"id": 1},
                },
                processing_status="INITIAL_DISPATCHING",
                processing_claim_token=claim_token,
                processing_claim_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            .returning(models.RawEvent.id)
        )
    ).scalar_one()
    await session.commit()

    lock_attempted = asyncio.Event()
    original_lock = chatwoot_processor.acquire_conversation_delivery_xact_lock

    async def observed_lock(lock_session, locked_conversation_id):
        lock_attempted.set()
        await original_lock(lock_session, locked_conversation_id)

    monkeypatch.setattr(
        chatwoot_processor,
        "acquire_conversation_delivery_xact_lock",
        observed_lock,
    )

    connection = await get_engine().connect()
    task = None
    try:
        await connection.begin()
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"social-reply:conversation-delivery:{conversation_id}"},
        )
        task = asyncio.create_task(
            process_raw_event(
                str(raw_event_id),
                raw_event_claim_token=claim_token,
            )
        )
        await asyncio.wait_for(lock_attempted.wait(), timeout=2)
        await connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
        updated = await connection.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id == raw_event_id)
            .values(processing_status="CLAIM_REPLACED")
        )
        assert updated.rowcount == 1
        await connection.commit()
        await asyncio.wait_for(task, timeout=4)
    finally:
        if connection.in_transaction():
            await connection.rollback()
        await connection.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    session.expire_all()
    raw_event = await session.get(models.RawEvent, raw_event_id)
    assert raw_event.processing_status == "CLAIM_REPLACED"
    assert await session.scalar(select(func.count()).select_from(models.Message)) == 0
    assert await session.scalar(select(func.count()).select_from(models.DecisionJob)) == 0


async def test_direct_ingestion_takes_conversation_lock_before_raw_event_locks(
    session, monkeypatch
):
    account_id, conversation_id = await _seed_conversation(session)
    conversation = await session.get(models.Conversation, conversation_id)
    contact = await session.get(models.Contact, conversation.contact_id)
    raw_ids = []
    for _ in range(2):
        raw_ids.append(
            (
                await session.execute(
                    insert(models.RawEvent)
                    .values(
                        tenant_id="default",
                        platform_account_id=account_id,
                        source="telegram",
                        payload={},
                        processing_status="PENDING",
                    )
                    .returning(models.RawEvent.id)
                )
            ).scalar_one()
        )
    await session.commit()

    from social_reply.application.event_ingestion import direct

    original_lock = direct.acquire_conversation_delivery_xact_lock
    lock_attempts = 0
    both_waiting = asyncio.Event()

    async def observed_lock(lock_session, locked_conversation_id):
        nonlocal lock_attempts
        lock_attempts += 1
        if lock_attempts == 2:
            both_waiting.set()
        await original_lock(lock_session, locked_conversation_id)

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(direct, "acquire_conversation_delivery_xact_lock", observed_lock)
    monkeypatch.setattr(direct, "dispatch_actor", no_dispatch)

    def event(suffix: str) -> CanonicalEvent:
        return CanonicalEvent(
            platform="telegram",
            platform_account_key=str(account_id),
            external_event_id=f"lock-order-{suffix}",
            external_user_id=contact.external_user_id,
            conversation_key=conversation.conversation_key,
            text=suffix,
        )

    connection = await get_engine().connect()
    tasks = []
    try:
        await connection.begin()
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"social-reply:conversation-delivery:{conversation_id}"},
        )
        tasks = [
            asyncio.create_task(ingest_canonical_event(event(suffix), raw_event_id=raw_event_id))
            for suffix, raw_event_id in zip(("one", "two"), raw_ids, strict=True)
        ]
        await asyncio.wait_for(both_waiting.wait(), timeout=2)
        await connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
        updated = await connection.execute(
            update(models.RawEvent)
            .where(models.RawEvent.id.in_(raw_ids))
            .values(processing_status="FINALIZER_CHECK")
        )
        assert updated.rowcount == 2
        await connection.commit()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=4)
    finally:
        if connection.in_transaction():
            await connection.rollback()
        await connection.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    rows = (
        await session.execute(
            select(models.RawEvent.processing_status).where(models.RawEvent.id.in_(raw_ids))
        )
    ).scalars()
    assert set(rows) == {"PROCESSED", "DECISION_PENDING"}


@pytest.mark.parametrize("human_action", ["resume", "reply"])
async def test_generation_reservation_cannot_deadlock_with_human_action(
    session, monkeypatch, human_action
):
    account_id, conversation_id = await _seed_conversation(session)
    message_id, raw_event_id, snapshot = await _seed_generation_input(
        session, account_id, conversation_id
    )
    await _prepare_human_action(session, conversation_id, human_action)

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)
    original_human_lock = human_workflow.acquire_conversation_delivery_xact_lock
    human_waiting = asyncio.Event()

    async def observed_human_lock(lock_session, locked_conversation_id):
        human_waiting.set()
        await original_human_lock(lock_session, locked_conversation_id)

    monkeypatch.setattr(
        human_workflow, "acquire_conversation_delivery_xact_lock", observed_human_lock
    )

    connection = await get_engine().connect()
    human_task = None
    try:
        async with AsyncSession(bind=connection, expire_on_commit=False) as generation_session:
            await generation_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"social-reply:conversation-delivery:{conversation_id}"},
            )
            human_task = asyncio.create_task(
                _run_human_action(human_action, conversation_id, message_id)
            )
            await asyncio.wait_for(human_waiting.wait(), timeout=2)
            await generation_session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            job_id = await reserve_decision_job(
                generation_session,
                raw_event_id=raw_event_id,
                conversation_id=conversation_id,
                message_id=message_id,
                account_id=account_id,
                snapshot=snapshot,
            )
            await generation_session.commit()
        await asyncio.wait_for(human_task, timeout=4)
    finally:
        if connection.in_transaction():
            await connection.rollback()
        await connection.close()
        if human_task is not None and not human_task.done():
            human_task.cancel()
            await asyncio.gather(human_task, return_exceptions=True)

    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    conversation = await session.get(models.Conversation, conversation_id)
    assert job.status == "PENDING"
    assert job.decision_generation == conversation.decision_generation == 1
    await _assert_human_action(session, conversation_id, human_action)


@pytest.mark.parametrize("human_action", ["resume", "reply"])
async def test_generation_finalization_cannot_deadlock_with_human_action(
    session, monkeypatch, human_action
):
    account_id, conversation_id = await _seed_conversation(session)
    job_id, message_id, _raw_event_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )
    await _prepare_human_action(session, conversation_id, human_action)

    class IgnoreLLM:
        async def decide(self, _context):
            return ReplyDecision(action=ReplyAction.IGNORE, source="llm")

    class EnabledKillSwitch:
        async def is_disabled(self, *_args):
            return False

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(decision_runner, "_llm", IgnoreLLM())
    monkeypatch.setattr(decision_runner, "_make_killswitch", lambda: EnabledKillSwitch())
    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)

    original_finalization_lock = decision_runner.acquire_conversation_delivery_xact_lock
    finalization_has_lock = asyncio.Event()
    release_finalization = asyncio.Event()

    async def paused_finalization_lock(lock_session, locked_conversation_id):
        await original_finalization_lock(lock_session, locked_conversation_id)
        finalization_has_lock.set()
        await release_finalization.wait()

    monkeypatch.setattr(
        decision_runner,
        "acquire_conversation_delivery_xact_lock",
        paused_finalization_lock,
    )
    original_human_lock = human_workflow.acquire_conversation_delivery_xact_lock
    human_waiting = asyncio.Event()

    async def observed_human_lock(lock_session, locked_conversation_id):
        human_waiting.set()
        await original_human_lock(lock_session, locked_conversation_id)

    monkeypatch.setattr(
        human_workflow, "acquire_conversation_delivery_xact_lock", observed_human_lock
    )

    finalization_task = asyncio.create_task(process_decision_job(str(job_id)))
    human_task = None
    try:
        await asyncio.wait_for(finalization_has_lock.wait(), timeout=2)
        human_task = asyncio.create_task(
            _run_human_action(human_action, conversation_id, message_id)
        )
        await asyncio.wait_for(human_waiting.wait(), timeout=2)
        release_finalization.set()
        assert await asyncio.wait_for(finalization_task, timeout=4) is True
        await asyncio.wait_for(human_task, timeout=4)
    finally:
        release_finalization.set()
        for task in (finalization_task, human_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (finalization_task, human_task) if task is not None),
            return_exceptions=True,
        )

    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    decision = (
        await session.execute(
            select(models.ReplyDecision).where(models.ReplyDecision.decision_job_id == job_id)
        )
    ).scalar_one()
    assert job.status == "COMPLETED"
    assert decision.action == "ignore"
    assert decision.decision_generation == job.decision_generation == 1
    await _assert_human_action(session, conversation_id, human_action)


async def test_reclaimed_job_token_prevents_stale_worker_overwrite(session, monkeypatch):
    account_id, conversation_id = await _seed_conversation(session)
    job_id, _message_id, _raw_event_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    tokens = []
    llm_calls = 0

    class VersionedLLM:
        async def decide(self, _context):
            nonlocal llm_calls
            llm_calls += 1
            label = "B" if llm_calls == 1 else "A"
            return ReplyDecision(
                action=ReplyAction.IGNORE,
                reason_codes=(label,),
                source="llm",
            )

    class EnabledKillSwitch:
        async def is_disabled(self, *_args):
            return False

    from social_reply.application.reply_decision import runner

    monkeypatch.setattr(runner, "_llm", VersionedLLM())
    monkeypatch.setattr(runner, "_make_killswitch", lambda: EnabledKillSwitch())
    actual_run = run_and_persist_decision

    async def delayed_first(*args, **kwargs):
        tokens.append(kwargs["claim_token"])
        if len(tokens) == 1:
            first_started.set()
            await release_first.wait()
        return await actual_run(*args, **kwargs)

    monkeypatch.setattr(decision_jobs, "run_and_persist_decision", delayed_first)
    monkeypatch.setattr(
        "social_reply.application.reply_decision.actors.process_reply_decision.send",
        lambda *_args, **_kwargs: None,
    )

    stale_worker = asyncio.create_task(process_decision_job(str(job_id)))
    await asyncio.wait_for(first_started.wait(), timeout=2)
    await session.execute(
        update(models.DecisionJob)
        .where(models.DecisionJob.id == job_id)
        .values(
            locked_at=(datetime.now(UTC) - decision_jobs._STALE_PROCESSING - timedelta(seconds=1))
        )
    )
    await session.commit()
    assert await decision_jobs.sweep_decision_jobs() == [job_id]

    assert await process_decision_job(str(job_id)) is True
    release_first.set()
    assert await asyncio.wait_for(stale_worker, timeout=2) is False

    session.expire_all()
    job = await session.get(models.DecisionJob, job_id)
    decisions = (
        (
            await session.execute(
                select(models.ReplyDecision).where(models.ReplyDecision.decision_job_id == job_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]
    assert job.status == "COMPLETED"
    assert job.claim_token is None
    assert len(decisions) == 1
    assert decisions[0].reason_codes == ["B"]


async def test_reservation_is_idempotent_per_message_and_monotonic_across_messages(session):
    account_id, conversation_id = await _seed_conversation(session)
    first_job_id, first_message_id, _raw_event_id, snapshot = await _reserve(
        session, account_id, conversation_id
    )
    duplicate_job_id = await reserve_decision_job(
        session,
        raw_event_id=None,
        conversation_id=conversation_id,
        message_id=first_message_id,
        account_id=account_id,
        snapshot=snapshot,
    )
    await session.commit()
    second_job_id, _message_id, _raw_event_id, _snapshot = await _reserve(
        session, account_id, conversation_id, text="second"
    )

    session.expire_all()
    first_job = await session.get(models.DecisionJob, first_job_id)
    second_job = await session.get(models.DecisionJob, second_job_id)
    conversation = await session.get(models.Conversation, conversation_id)
    assert duplicate_job_id == first_job_id
    first_message = await session.get(models.Message, first_job.message_id)
    second_message = await session.get(models.Message, second_job.message_id)
    assert first_job.decision_generation == first_message.decision_generation == 1
    assert second_job.decision_generation == second_message.decision_generation == 2
    assert conversation.decision_generation == 2
    assert await session.scalar(select(func.count()).select_from(models.DecisionJob)) == 2


async def test_supersession_cancels_only_unsent_bot_decision_outboxes(session):
    account_id, conversation_id = await _seed_conversation(session)
    first_job_id, first_message_id, _raw_event_id, _snapshot = await _reserve(
        session, account_id, conversation_id
    )
    cases = [
        ("DECISION", "BOT", "PENDING", "CANCELLED"),
        ("DECISION", "BOT", "FAILED", "CANCELLED"),
        ("DECISION", "BOT", "SENT", "SENT"),
        ("MANUAL_REPLY", "ADMIN_HUMAN", "PENDING", "PENDING"),
        ("DRAFT_APPROVAL", "ADMIN_HUMAN", "PENDING", "PENDING"),
    ]
    outbox_ids = []
    for index, (origin, actor, status, _expected) in enumerate(cases):
        outbox_id = uuid.uuid4()
        outbox_ids.append(outbox_id)
        await session.execute(
            insert(models.OutboxMessage).values(
                id=outbox_id,
                conversation_id=conversation_id,
                platform_account_id=account_id,
                destination_type="chatwoot_conversation",
                destination_id="conversation",
                message_type="text",
                payload={"text": str(index)},
                reply_to_message_id=first_message_id,
                origin_kind=origin,
                actor_kind=actor,
                idempotency_key=uuid.uuid4().hex,
                status=status,
            )
        )
        await session.execute(
            insert(models.ReplyDecision).values(
                tenant_id="default",
                conversation_id=conversation_id,
                message_id=None,
                action="auto_reply",
                reply_text=str(index),
                reason_codes=[],
                source="llm",
                decision_generation=1,
                outbox_id=outbox_id,
            )
        )
    await session.commit()

    await _reserve(session, account_id, conversation_id, text="newer")
    rows = (
        await session.execute(
            select(
                models.OutboxMessage.id,
                models.OutboxMessage.status,
                models.OutboxMessage.last_error_code,
            ).where(models.OutboxMessage.id.in_(outbox_ids))
        )
    ).all()
    statuses = {row.id: (row.status, row.last_error_code) for row in rows}
    assert [statuses[outbox_id][0] for outbox_id in outbox_ids] == [
        expected for *_case, expected in cases
    ]
    assert [statuses[outbox_id][1] for outbox_id in outbox_ids] == [
        "STALE_CONVERSATION_INPUT",
        "STALE_CONVERSATION_INPUT",
        None,
        None,
        None,
    ]
