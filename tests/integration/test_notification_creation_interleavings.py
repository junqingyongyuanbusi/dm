from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text, update
from tests.integration.company_permission_support import create_staff
from tests.integration.test_feishu_handoff_sender import _settings
from tests.integration.test_handoff_notification_deadlock_regression import (
    _cleanup_tasks,
    _finish_tasks,
    _install_sender,
    _wait_for_event,
)
from tests.integration.test_handoff_notification_route_locking import _seed_shared_source

from social_reply.application.account_management import feishu_handoff_service as config_module
from social_reply.application.handoff_notifications import sender as sender_module
from social_reply.application.handoff_notifications import service as notification_module
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.reply_decision import business_prompt_retirement, persist
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.reply.decision import ReplyAction, ReplyDecision
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def _wait_for_database_blocker(*, waiting_pid: int, blocking_pid: int) -> None:
    # Poll evidence, not elapsed time: only a real PostgreSQL wait edge releases
    # the writer barrier. This observer uses its own connection and no row locks.
    async with asyncio.timeout(10):
        async with get_session_factory()() as observer:
            while True:
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(CAST(:waiting_pid AS integer))"),
                    {"waiting_pid": waiting_pid},
                )
                if blocking_pid in blockers:
                    return
                await asyncio.sleep(0.01)


async def test_sender_refresh_serializes_with_config_update_and_claims_fresh_route(
    session, monkeypatch
) -> None:
    seed = await _seed_shared_source(session)
    administrator = await create_staff(
        session, tenant_id=seed.tenant_id, role="WORKSPACE_ADMIN"
    )
    await session.execute(
        update(models.HandoffNotificationIntent)
        .where(models.HandoffNotificationIntent.id == seed.c2_conversation.intent_id)
        .values(
            status="PENDING",
            provider_message_id=None,
            config_version=0,
            delivered_revision=0,
            attempt_count=0,
        )
    )
    await session.commit()
    monkeypatch.setattr(sender_module, "get_settings", _settings)
    writer_has_route = asyncio.Event()
    reader_requests_route = asyncio.Event()
    reader_has_route = asyncio.Event()
    release_writer = asyncio.Event()
    backend_pids: dict[str, int] = {}
    original_config_lock = config_module._lock_config
    original_shared_lock = notification_module.acquire_shared_xact_lock

    async def controlled_config_lock(session_arg, tenant_id):
        await original_config_lock(session_arg, tenant_id)
        backend_pids["writer"] = await session_arg.scalar(text("SELECT pg_backend_pid()"))
        writer_has_route.set()
        await _wait_for_event(release_writer, "release config route writer")

    async def observed_shared_lock(session_arg, key):
        backend_pids["reader"] = await session_arg.scalar(text("SELECT pg_backend_pid()"))
        reader_requests_route.set()
        await original_shared_lock(session_arg, key)
        reader_has_route.set()

    monkeypatch.setattr(config_module, "_lock_config", controlled_config_lock)
    monkeypatch.setattr(
        notification_module, "acquire_shared_xact_lock", observed_shared_lock
    )
    tasks: list[asyncio.Task] = []
    try:
        tasks.append(
            asyncio.create_task(
                config_module.save_feishu_handoff_config(
                    tenant_id=seed.tenant_id,
                    actor=administrator.principal.actor,
                    principal=administrator.principal,
                    account_id=seed.second_bot_id,
                    destination_chat_id="oc-interleaving-new-route",
                    enabled=True,
                )
            )
        )
        await _wait_for_event(writer_has_route, "config writer acquired exclusive route")
        tasks.append(
            asyncio.create_task(
                sender_module._claim_notification(seed.c2_conversation.intent_id)
            )
        )
        await _wait_for_event(reader_requests_route, "sender requests route refresh")
        await _wait_for_database_blocker(
            waiting_pid=backend_pids["reader"], blocking_pid=backend_pids["writer"]
        )
        assert not reader_has_route.is_set()
        assert not tasks[1].done()
        release_writer.set()
        config_id, claimed = await _finish_tasks(tasks)
    finally:
        await _cleanup_tasks(tasks, release_writer)

    assert config_id == seed.config_id
    assert reader_has_route.is_set()
    assert isinstance(claimed, sender_module.ClaimedNotification)
    assert claimed.feishu_platform_account_id == seed.second_bot_id
    assert claimed.destination_chat_id == "oc-interleaving-new-route"
    async with get_session_factory()() as verify:
        config = await verify.get(models.TenantFeishuHandoffConfig, seed.config_id)
        intent = await verify.get(
            models.HandoffNotificationIntent, seed.c2_conversation.intent_id
        )
        assert config is not None and config.config_version == 2
        assert intent is not None and intent.config_version == config.config_version
        assert intent.status == "SENDING"
        assert intent.feishu_platform_account_id == config.feishu_platform_account_id
        assert intent.destination_chat_id == config.destination_chat_id
        assert intent.claim_token == claimed.claim_token
        assert intent.attempt_count == 1


@pytest.mark.parametrize("creation_path", ["decision", "rollback"])
async def test_notification_creation_and_same_conversation_outbox_complete_without_deadlock(
    session, monkeypatch, creation_path
) -> None:
    seed = await _seed_shared_source(session)
    sender = _install_sender(monkeypatch, seed.source_account_id)
    if creation_path == "rollback":
        settings = get_settings().model_copy(update={"reply_business_prompt_enabled": False})
        monkeypatch.setattr(business_prompt_retirement, "get_settings", lambda: settings)
        # The retirement API recognizes hash-only provenance, without needing a
        # prompt-version fixture or changing the route/account topology.
        await session.execute(
            update(models.ReplyDecision)
            .where(models.ReplyDecision.outbox_id == seed.c1_outbox_id)
            .values(reply_business_prompt_content_hash="a" * 64)
        )
        await session.commit()

    creator_module = persist if creation_path == "decision" else business_prompt_retirement
    creator_ready = asyncio.Event()
    delivery_requests_conversation = asyncio.Event()
    release_creator = asyncio.Event()
    original_ensure = creator_module.ensure_handoff_notification_intent
    original_delivery_lock = (
        outbox_module.hold_conversation_delivery_lock_on_connection_in_transaction
    )

    async def controlled_ensure(session_arg, *, work):
        assert work.conversation_id == seed.c1_conversation_id
        assert await session_arg.scalar(
            select(models.HandoffNotificationIntent.id).where(
                models.HandoffNotificationIntent.human_work_item_id == work.id
            )
        ) is None
        # Both real creators hold C here. Leave the actual INSERT and its foreign
        # key locking intact: the old Outbox order held accounts while waiting for C.
        creator_ready.set()
        await _wait_for_event(release_creator, "release notification creator")
        return await original_ensure(session_arg, work=work)

    @asynccontextmanager
    async def observed_delivery_lock(connection, conversation_id):
        assert conversation_id == seed.c1_conversation_id
        delivery_requests_conversation.set()
        async with original_delivery_lock(connection, conversation_id):
            yield

    monkeypatch.setattr(creator_module, "ensure_handoff_notification_intent", controlled_ensure)
    monkeypatch.setattr(
        outbox_module,
        "hold_conversation_delivery_lock_on_connection_in_transaction",
        observed_delivery_lock,
    )

    async def create_notification():
        async with get_session_factory()() as creator:
            if creation_path == "rollback":
                report = await business_prompt_retirement.retire_business_prompt_work_for_rollback(
                    creator
                )
                assert report.conversations == 1
                assert report.outboxes_cancelled == 1
            else:
                conversation = await creator.get(models.Conversation, seed.c1_conversation_id)
                assert conversation is not None
                snapshot = DecisionSnapshot(
                    text="Please transfer me to a person",
                    platform=conversation.platform,
                    tenant_id=seed.tenant_id,
                    brand_id=conversation.brand_id,
                    account_id=str(seed.source_account_id),
                    conversation_key=conversation.conversation_key,
                    automation_state="BOT_ACTIVE",
                    state_version=1,
                )
                # A conversation-level HANDOFF is supported without a message ID;
                # avoid hitting the existing auto-reply decision's idempotency guard.
                await persist.persist_decision(
                    creator,
                    snapshot,
                    seed.c1_conversation_id,
                    None,
                    seed.source_account_id,
                    ReplyDecision(
                        action=ReplyAction.HANDOFF,
                        source="rule",
                        reason_codes=("INTERLEAVING_HANDOFF",),
                    ),
                    "interleaving-test",
                )
            await creator.commit()

    tasks: list[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(create_notification()))
        await _wait_for_event(creator_ready, "creator holds conversation before new intent")
        tasks.append(
            asyncio.create_task(outbox_module.deliver_outbox(str(seed.c1_outbox_id)))
        )
        await _wait_for_event(delivery_requests_conversation, "outbox requests conversation")
        release_creator.set()
        _, delivery_result = await _finish_tasks(tasks)
    finally:
        await _cleanup_tasks(tasks, release_creator)

    expected_result = "SKIPPED_NOT_CLAIMABLE" if creation_path == "rollback" else "CANCELLED"
    assert delivery_result == expected_result
    assert sender.calls == []
    async with get_session_factory()() as verify:
        intent = (
            await verify.scalars(
                select(models.HandoffNotificationIntent).where(
                    models.HandoffNotificationIntent.conversation_id == seed.c1_conversation_id
                )
            )
        ).one()
        work = await verify.get(models.HumanWorkItem, intent.human_work_item_id)
        state = await verify.get(models.AutomationState, seed.c1_conversation_id)
        outbox = await verify.get(models.OutboxMessage, seed.c1_outbox_id)
        assert intent.status == "PENDING"
        assert intent.feishu_platform_account_id == seed.bot_id
        assert intent.notification_config_id == seed.config_id
        assert work is not None and work.status == "WAITING"
        assert state is not None and state.state == "HANDOFF_PENDING"
        assert outbox is not None and outbox.status == "CANCELLED"
        if creation_path == "rollback":
            assert outbox.last_error_code == "REPLY_BUSINESS_PROMPT_ROLLBACK"
