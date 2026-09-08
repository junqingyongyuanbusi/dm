from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.company_permission_support import signed_card_request
from tests.integration.test_handoff_notification_route_locking import (
    _OpenKillSwitch,
    _seed_shared_source,
)

from social_reply.application.handoff_notifications import callbacks as callback_module
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 10
_CLEANUP_SECONDS = 10


async def _wait_for_event(event: asyncio.Event, description: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=_WAIT_SECONDS)
    except TimeoutError as error:
        raise AssertionError(f"Interleaving did not reach: {description}") from error


async def _finish_tasks(tasks: list[asyncio.Task]) -> list:
    # Observe all failures, but do not turn a deadlock or timeout into a passing test.
    completed, pending = await asyncio.wait(tasks, timeout=_WAIT_SECONDS)
    assert not pending, "Concurrent operations did not complete within the deadlock bound"
    assert len(completed) == len(tasks)
    return [task.result() for task in tasks]


async def _cleanup_tasks(tasks: list[asyncio.Task], *releases: asyncio.Event) -> None:
    for release in releases:
        release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    if not tasks:
        return
    completed, pending = await asyncio.wait(tasks, timeout=_CLEANUP_SECONDS)
    for task in completed:
        if not task.cancelled():
            task.exception()
    assert not pending, "Cancelled delivery tasks did not release their connection contexts"


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []

    async def send_text(self, *, target, text):
        self.calls.append((dict(target), text))
        return "deadlock-regression-provider-message"


def _install_sender(monkeypatch, source_account_id) -> _RecordingSender:
    sender = _RecordingSender()

    async def get_sender(account_id):
        assert account_id == source_account_id
        return sender

    monkeypatch.setattr(outbox_module, "get_platform_sender", get_sender)
    monkeypatch.setattr(outbox_module, "make_killswitch_checker", lambda: _OpenKillSwitch())
    return sender


async def test_same_conversation_callback_and_outbox_complete_without_deadlock(
    session, monkeypatch
) -> None:
    seed = await _seed_shared_source(session)
    conversation = seed.c2_conversation
    # Reuse the existing bot intent, but put it on the callback's conversation. The
    # original route-lock test uses two conversations and cannot expose this cycle.
    await session.execute(
        update(models.OutboxMessage)
        .where(models.OutboxMessage.id == seed.c1_outbox_id)
        .values(
            conversation_id=conversation.conversation_id,
            reply_to_message_id=conversation.message_id,
            destination_id="route-lock-customer-2",
            payload={
                "text": "route-lock reply",
                "target": {"chat_id": "route-lock-customer-2"},
            },
        )
    )
    await session.execute(
        update(models.ReplyDecision)
        .where(models.ReplyDecision.outbox_id == seed.c1_outbox_id)
        .values(
            conversation_id=conversation.conversation_id,
            message_id=conversation.message_id,
        )
    )
    await session.commit()
    sender = _install_sender(monkeypatch, seed.source_account_id)
    callback_has_conversation = asyncio.Event()
    delivery_requests_conversation = asyncio.Event()
    release_callback = asyncio.Event()
    original_callback_lock = callback_module.acquire_conversation_delivery_xact_lock
    original_delivery_lock = (
        outbox_module.hold_conversation_delivery_lock_on_connection_in_transaction
    )

    async def controlled_callback_lock(session_arg, conversation_id):
        await original_callback_lock(session_arg, conversation_id)
        callback_has_conversation.set()
        await _wait_for_event(release_callback, "release callback conversation owner")

    @asynccontextmanager
    async def observed_delivery_lock(connection, conversation_id):
        # Before the real acquisition: current code already owns the account rows.
        # A corrected conversation-first order can also reach this barrier and pass.
        delivery_requests_conversation.set()
        async with original_delivery_lock(connection, conversation_id):
            yield

    monkeypatch.setattr(
        callback_module, "acquire_conversation_delivery_xact_lock", controlled_callback_lock
    )
    monkeypatch.setattr(
        outbox_module,
        "hold_conversation_delivery_lock_on_connection_in_transaction",
        observed_delivery_lock,
    )
    request = signed_card_request(
        seed.feishu_seed,
        conversation,
        operator_open_id=seed.operator_open_id,
        action="claim",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="same-conversation-deadlock-regression",
    )
    tasks: list[asyncio.Task] = []
    try:
        tasks.append(
            asyncio.create_task(
                callback_module.handle_feishu_card_action(
                    account_id=seed.bot_id,
                    tenant_id=seed.tenant_id,
                    provider_event_id=request.event_id,
                    request_digest=request.proof,
                    event=request.payload["event"],
                    feature_enabled=True,
                ),
                name="deadlock-regression-callback",
            )
        )
        await _wait_for_event(callback_has_conversation, "callback acquired conversation")
        tasks.append(
            asyncio.create_task(
                outbox_module.deliver_outbox(str(seed.c1_outbox_id)),
                name="deadlock-regression-outbox",
            )
        )
        await _wait_for_event(delivery_requests_conversation, "outbox requests conversation")
        release_callback.set()
        callback_result, delivery_result = await _finish_tasks(tasks)
    finally:
        await _cleanup_tasks(tasks, release_callback)

    assert callback_result["toast"]["type"] == "success"
    # Claim cancels pending bot work before releasing conversation serialization.
    assert delivery_result == "SKIPPED_NOT_CLAIMABLE"
    assert sender.calls == []
    async with get_session_factory()() as verify:
        work = await verify.get(models.HumanWorkItem, conversation.work_id)
        state = await verify.get(models.AutomationState, conversation.conversation_id)
        outbox = await verify.get(models.OutboxMessage, seed.c1_outbox_id)
        assert work is not None and work.status == "CLAIMED"
        assert state is not None and state.state == "HUMAN_ACTIVE"
        assert outbox is not None and outbox.status == "CANCELLED"
        assert outbox.attempt_count == 0


async def test_checkpoint_duplicate_delivery_completes_without_deadlock(
    session, monkeypatch
) -> None:
    seed = await _seed_shared_source(session)
    sender = _install_sender(monkeypatch, seed.source_account_id)
    role: ContextVar[str | None] = ContextVar("deadlock_delivery_role", default=None)
    checkpoint_committed = asyncio.Event()
    duplicate_requests_conversation = asyncio.Event()
    release_checkpoint = asyncio.Event()
    original_commit = AsyncSession.commit
    original_delivery_lock = (
        outbox_module.hold_conversation_delivery_lock_on_connection_in_transaction
    )

    async def controlled_commit(session_arg):
        checkpoint = role.get() == "first" and not checkpoint_committed.is_set() and any(
            isinstance(instance, models.OutboxMessage)
            and instance.id == seed.c1_outbox_id
            and instance.status == "SENDING"
            and instance.attempt_count == 1
            for instance in session_arg.identity_map.values()
        )
        await original_commit(session_arg)
        if checkpoint:
            checkpoint_committed.set()
            await _wait_for_event(release_checkpoint, "release durable checkpoint owner")

    @asynccontextmanager
    async def observed_delivery_lock(connection, conversation_id):
        if role.get() == "duplicate":
            duplicate_requests_conversation.set()
        async with original_delivery_lock(connection, conversation_id):
            yield

    monkeypatch.setattr(AsyncSession, "commit", controlled_commit)
    monkeypatch.setattr(
        outbox_module,
        "hold_conversation_delivery_lock_on_connection_in_transaction",
        observed_delivery_lock,
    )

    async def deliver_with_role(name: str):
        token = role.set(name)
        try:
            return await outbox_module.deliver_outbox(str(seed.c1_outbox_id))
        finally:
            role.reset(token)

    tasks: list[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(deliver_with_role("first")))
        await _wait_for_event(checkpoint_committed, "first delivery committed SENDING")
        tasks.append(asyncio.create_task(deliver_with_role("duplicate")))
        await _wait_for_event(
            duplicate_requests_conversation, "duplicate delivery requests conversation"
        )
        release_checkpoint.set()
        results = await _finish_tasks(tasks)
    finally:
        await _cleanup_tasks(tasks, release_checkpoint)

    assert results == ["SENT", "SKIPPED_NOT_CLAIMABLE"]
    assert len(sender.calls) == 1
    async with get_session_factory()() as verify:
        outbox = await verify.get(models.OutboxMessage, seed.c1_outbox_id)
        notifications = list(
            await verify.scalars(
                select(models.HandoffNotificationIntent).where(
                    models.HandoffNotificationIntent.conversation_id == seed.c1_conversation_id
                )
            )
        )
        assert outbox is not None and outbox.status == "SENT"
        assert outbox.attempt_count == 1
        assert notifications == []
