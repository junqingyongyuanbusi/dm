from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.company_permission_support import (
    FeishuConversationSeed,
    FeishuHandoffSeed,
    create_staff,
    signed_card_request,
)

from social_reply.application.handoff_notifications.callbacks import handle_feishu_card_action
from social_reply.application.handoff_notifications.service import (
    ensure_handoff_notification_intent,
    handoff_notification_route_lock_key,
    lock_handoff_notification_route,
)
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.advisory_locks import acquire_xact_lock
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration

_TENANT_ID = "default"
_BOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_SECOND_BOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_SOURCE_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")
_CONFIG_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_C1_CONVERSATION_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_C2_CONVERSATION_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
_C1_CONTACT_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
_C2_CONTACT_ID = uuid.UUID("30000000-0000-0000-0000-000000000002")
_C1_MESSAGE_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
_C2_MESSAGE_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
_C1_WORK_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
_C2_WORK_ID = uuid.UUID("50000000-0000-0000-0000-000000000002")
_C2_INTENT_ID = uuid.UUID("60000000-0000-0000-0000-000000000002")
_C2_PUBLIC_ID = uuid.UUID("70000000-0000-0000-0000-000000000002")
_C2_ACTION_NONCE = uuid.UUID("80000000-0000-0000-0000-000000000002")
_C1_OUTBOX_ID = uuid.UUID("90000000-0000-0000-0000-000000000001")

_APP_ID = "cli-route-lock-test"
_VERIFICATION_TOKEN = "route-lock-verification-token"
_ENCRYPT_KEY = "route-lock-encrypt-key"
_DESTINATION_CHAT_ID = "oc-route-lock-support"


@dataclass(frozen=True)
class _RouteRaceSeed:
    tenant_id: str
    bot_id: uuid.UUID
    second_bot_id: uuid.UUID
    source_account_id: uuid.UUID
    config_id: uuid.UUID
    c1_conversation_id: uuid.UUID
    c1_outbox_id: uuid.UUID
    c2_conversation: FeishuConversationSeed
    feishu_seed: FeishuHandoffSeed
    operator_open_id: str


class _OpenKillSwitch:
    async def is_disabled(self, _brand_id, _account_id, _tenant_id="default") -> bool:
        return False


class _ClosedKillSwitch:
    async def is_disabled(self, _brand_id, _account_id, _tenant_id="default") -> bool:
        return True


class _TelegramSender:
    async def send_text(self, *, target, text):
        return f"telegram-provider:{target['chat_id']}:{text}"


def _feishu_account(account_id: uuid.UUID, *, name: str) -> models.PlatformAccount:
    return models.PlatformAccount(
        id=account_id,
        tenant_id=_TENANT_ID,
        brand_id=f"{name}-brand",
        platform="feishu",
        name=name,
        external_account_id=f"{name}-{account_id}",
        public_id=f"{name}-{account_id}",
        credential_bundle={},
        webhook_secret_bundle={},
        config={"feishu_health_status": "READY"},
        capability={"dm": True, "mentions": True, "max_text_length": 4000},
        status="active",
    )


async def _seed_shared_source(session) -> _RouteRaceSeed:
    staff = await create_staff(
        session,
        username=f"route-lock-staff-{uuid.uuid4().hex}",
        tenant_id=_TENANT_ID,
        role="USER",
    )
    operator_open_id = f"ou-route-lock-{uuid.uuid4().hex}"
    seed_rows = [
        _feishu_account(_BOT_ID, name="route-lock-bot"),
        _feishu_account(_SECOND_BOT_ID, name="route-lock-bot-second"),
        models.PlatformAccount(
            id=_SOURCE_ACCOUNT_ID,
            tenant_id=_TENANT_ID,
            brand_id="route-lock-source-brand",
            platform="telegram",
            name="route-lock-source",
            external_account_id="route-lock-source-external",
            public_id="route-lock-source-public",
            shared_with_support=True,
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4000},
            status="active",
        ),
        models.TenantFeishuHandoffConfig(
            id=_CONFIG_ID,
            tenant_id=_TENANT_ID,
            feishu_platform_account_id=_BOT_ID,
            destination_chat_id=_DESTINATION_CHAT_ID,
            enabled=True,
            config_version=1,
        ),
        models.Contact(
            id=_C1_CONTACT_ID,
            tenant_id=_TENANT_ID,
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            external_user_id="route-lock-customer-1",
        ),
        models.Contact(
            id=_C2_CONTACT_ID,
            tenant_id=_TENANT_ID,
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            external_user_id="route-lock-customer-2",
        ),
        models.Conversation(
            id=_C1_CONVERSATION_ID,
            tenant_id=_TENANT_ID,
            brand_id="route-lock-source-brand",
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            contact_id=_C1_CONTACT_ID,
            conversation_key="route-lock-conversation-1",
            decision_generation=1,
        ),
        models.Conversation(
            id=_C2_CONVERSATION_ID,
            tenant_id=_TENANT_ID,
            brand_id="route-lock-source-brand",
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            contact_id=_C2_CONTACT_ID,
            conversation_key="route-lock-conversation-2",
            decision_generation=1,
        ),
        models.AutomationState(
            conversation_id=_C1_CONVERSATION_ID,
            state="BOT_ACTIVE",
            state_version=1,
        ),
        models.AutomationState(
            conversation_id=_C2_CONVERSATION_ID,
            state="HANDOFF_PENDING",
            state_version=1,
        ),
        models.Message(
            id=_C1_MESSAGE_ID,
            conversation_id=_C1_CONVERSATION_ID,
            direction="inbound",
            sender_type="contact",
            text="route-lock customer one",
            reply_target={"chat_id": "route-lock-customer-1"},
            decision_generation=1,
        ),
        models.Message(
            id=_C2_MESSAGE_ID,
            conversation_id=_C2_CONVERSATION_ID,
            direction="inbound",
            sender_type="contact",
            text="route-lock customer two",
            reply_target={"chat_id": "route-lock-customer-2"},
            decision_generation=1,
        ),
        models.HumanWorkItem(
            id=_C2_WORK_ID,
            tenant_id=_TENANT_ID,
            conversation_id=_C2_CONVERSATION_ID,
            status="WAITING",
            reason_code="ROUTE_LOCK_TEST",
            version=1,
        ),
        models.HandoffNotificationIntent(
            id=_C2_INTENT_ID,
            public_id=_C2_PUBLIC_ID,
            tenant_id=_TENANT_ID,
            human_work_item_id=_C2_WORK_ID,
            conversation_id=_C2_CONVERSATION_ID,
            notification_config_id=_CONFIG_ID,
            config_version=1,
            feishu_platform_account_id=_BOT_ID,
            destination_chat_id=_DESTINATION_CHAT_ID,
            provider_uuid=uuid.UUID("a0000000-0000-0000-0000-000000000002"),
            provider_message_id="om-route-lock-c2",
            status="SYNCED",
            desired_card_state="WAITING",
            desired_revision=1,
            delivered_revision=1,
            action_nonce=_C2_ACTION_NONCE,
            attempt_count=1,
        ),
        models.FeishuHandoffOperator(
            tenant_id=_TENANT_ID,
            feishu_platform_account_id=_BOT_ID,
            operator_open_id=operator_open_id,
            display_name="Route lock operator",
            admin_user_id=staff.user_id,
            can_claim=True,
            can_resolve=True,
            status="ACTIVE",
        ),
        models.OutboxMessage(
            id=_C1_OUTBOX_ID,
            tenant_id=_TENANT_ID,
            conversation_id=_C1_CONVERSATION_ID,
            platform_account_id=_SOURCE_ACCOUNT_ID,
            destination_type="telegram_dm",
            destination_id="route-lock-customer-1",
            message_type="text",
            payload={
                "text": "route-lock reply",
                "target": {"chat_id": "route-lock-customer-1"},
            },
            reply_to_message_id=_C1_MESSAGE_ID,
            origin_kind="DECISION",
            actor_kind="BOT",
            actor_id="bot:route-lock-test",
            idempotency_key="route-lock-outbox-c1",
            status="PENDING",
        ),
        models.ReplyDecision(
            tenant_id=_TENANT_ID,
            conversation_id=_C1_CONVERSATION_ID,
            message_id=_C1_MESSAGE_ID,
            action="auto_reply",
            reply_text="route-lock reply",
            source="rule",
            decision_generation=1,
            outbox_id=_C1_OUTBOX_ID,
        ),
    ]
    for model_class in (
        models.PlatformAccount,
        models.TenantFeishuHandoffConfig,
        models.Contact,
        models.Conversation,
        models.AutomationState,
        models.Message,
        models.HumanWorkItem,
        models.HandoffNotificationIntent,
        models.FeishuHandoffOperator,
        models.OutboxMessage,
        models.ReplyDecision,
    ):
        session.add_all(row for row in seed_rows if isinstance(row, model_class))
        await session.flush()
    await session.commit()
    c2_conversation = FeishuConversationSeed(
        _SOURCE_ACCOUNT_ID,
        _C2_CONVERSATION_ID,
        _C2_MESSAGE_ID,
        _C2_WORK_ID,
        _C2_INTENT_ID,
        _C2_PUBLIC_ID,
        _C2_ACTION_NONCE,
        "om-route-lock-c2",
    )
    feishu_seed = FeishuHandoffSeed(
        _TENANT_ID,
        _BOT_ID,
        _CONFIG_ID,
        _APP_ID,
        _VERIFICATION_TOKEN,
        _ENCRYPT_KEY,
        (c2_conversation,),
        (operator_open_id,),
    )
    return _RouteRaceSeed(
        tenant_id=_TENANT_ID,
        bot_id=_BOT_ID,
        second_bot_id=_SECOND_BOT_ID,
        source_account_id=_SOURCE_ACCOUNT_ID,
        config_id=_CONFIG_ID,
        c1_conversation_id=_C1_CONVERSATION_ID,
        c1_outbox_id=_C1_OUTBOX_ID,
        c2_conversation=c2_conversation,
        feishu_seed=feishu_seed,
        operator_open_id=operator_open_id,
    )


async def test_public_failure_and_signed_callback_share_uuid_account_order(
    session,
    monkeypatch,
) -> None:
    seed = await _seed_shared_source(session)
    monkeypatch.setattr(outbox_module, "make_killswitch_checker", lambda: _ClosedKillSwitch())

    role: ContextVar[str | None] = ContextVar("route_lock_role", default=None)
    route_lock_acquired = asyncio.Event()
    outbox_accounts_locked = asyncio.Event()
    callback_account_lock_attempted = asyncio.Event()
    callback_account_lock_completed = asyncio.Event()
    release_outbox = asyncio.Event()
    lock_sequence: list[str] = []
    outbox_pause_used = False
    original_execute = AsyncSession.execute

    def is_route_shared_lock(statement) -> bool:
        return "PG_ADVISORY_XACT_LOCK_SHARED" in str(statement).upper()

    def is_account_lock(statement) -> bool:
        sql = str(statement).upper()
        return "PLATFORM_ACCOUNTS" in sql and "FOR UPDATE" in sql

    async def observed_execute(self, statement, *args, **kwargs):
        nonlocal outbox_pause_used
        current_role = role.get()
        route_shared_lock = is_route_shared_lock(statement)
        account_lock = is_account_lock(statement)
        if account_lock and current_role == "callback":
            callback_account_lock_attempted.set()
        result = await original_execute(self, statement, *args, **kwargs)
        if route_shared_lock and current_role == "outbox":
            lock_sequence.append("outbox_route")
            route_lock_acquired.set()
        if account_lock and current_role == "outbox" and not outbox_pause_used:
            outbox_pause_used = True
            lock_sequence.append("outbox_accounts")
            outbox_accounts_locked.set()
            await release_outbox.wait()
        if account_lock and current_role == "callback":
            callback_account_lock_completed.set()
        return result

    monkeypatch.setattr(AsyncSession, "execute", observed_execute)

    async def run_outbox():
        token = role.set("outbox")
        try:
            return await deliver_outbox(str(seed.c1_outbox_id))
        finally:
            role.reset(token)

    callback = signed_card_request(
        seed.feishu_seed,
        seed.c2_conversation,
        operator_open_id=seed.operator_open_id,
        action="claim",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=_C2_ACTION_NONCE,
        event_id="route-lock-signed-callback",
    )

    async def run_callback():
        token = role.set("callback")
        try:
            return await handle_feishu_card_action(
                account_id=seed.bot_id,
                tenant_id=seed.tenant_id,
                provider_event_id=callback.event_id,
                request_digest=callback.proof,
                event=callback.payload["event"],
                feature_enabled=True,
            )
        finally:
            role.reset(token)

    outbox_task = asyncio.create_task(run_outbox())
    await asyncio.wait_for(route_lock_acquired.wait(), timeout=5)
    await asyncio.wait_for(outbox_accounts_locked.wait(), timeout=5)
    assert lock_sequence[:2] == ["outbox_route", "outbox_accounts"]
    callback_task = asyncio.create_task(run_callback())
    await asyncio.wait_for(callback_account_lock_attempted.wait(), timeout=5)
    assert not callback_account_lock_completed.is_set()
    assert not callback_task.done()

    release_outbox.set()
    outbox_result, callback_result = await asyncio.wait_for(
        asyncio.gather(outbox_task, callback_task),
        timeout=5,
    )
    assert outbox_result == "CANCELLED"
    assert callback_result["toast"]["type"] == "success"

    async with get_session_factory()() as verify:
        intents = list(
            await verify.scalars(
                select(models.HandoffNotificationIntent).order_by(
                    models.HandoffNotificationIntent.conversation_id
                )
            )
        )
        created_intent = await verify.scalar(
            select(models.HandoffNotificationIntent).where(
                models.HandoffNotificationIntent.conversation_id == seed.c1_conversation_id
            )
        )
        created_work = await verify.scalar(
            select(models.HumanWorkItem).where(
                models.HumanWorkItem.conversation_id == seed.c1_conversation_id
            )
        )
    assert len(intents) == 2
    assert created_intent is not None
    assert created_intent.feishu_platform_account_id == seed.bot_id
    assert created_intent.notification_config_id == seed.config_id
    assert created_work is not None


async def test_each_public_preflight_reads_fresh_route_after_checkpoint(session, monkeypatch):
    seed = await _seed_shared_source(session)
    monkeypatch.setattr(outbox_module, "make_killswitch_checker", lambda: _OpenKillSwitch())

    async def get_sender(account_id):
        assert account_id == seed.source_account_id
        return _TelegramSender()

    monkeypatch.setattr(outbox_module, "get_platform_sender", get_sender)
    original_route_lock = outbox_module.lock_handoff_notification_route
    first_route_loaded = asyncio.Event()
    config_changed = asyncio.Event()
    route_account_ids: list[uuid.UUID | None] = []
    route_call_count = 0

    async def observed_route_lock(session_arg, *, tenant_id: str):
        nonlocal route_call_count
        route_call_count += 1
        call_number = route_call_count
        if call_number == 3:
            await config_changed.wait()
        route_values = await original_route_lock(session_arg, tenant_id=tenant_id)
        route_account_ids.append(route_values["feishu_platform_account_id"])
        if call_number == 1:
            first_route_loaded.set()
        return route_values

    monkeypatch.setattr(outbox_module, "lock_handoff_notification_route", observed_route_lock)

    async def change_route_after_checkpoint():
        await first_route_loaded.wait()
        async with get_session_factory()() as writer:
            await acquire_xact_lock(
                writer,
                handoff_notification_route_lock_key(seed.tenant_id),
            )
            await writer.execute(
                update(models.TenantFeishuHandoffConfig)
                .where(models.TenantFeishuHandoffConfig.id == seed.config_id)
                .values(
                    feishu_platform_account_id=seed.second_bot_id,
                    destination_chat_id="oc-route-lock-support-v2",
                    config_version=2,
                )
            )
            await writer.commit()
        config_changed.set()

    delivery_task = asyncio.create_task(deliver_outbox(str(seed.c1_outbox_id)))
    await asyncio.wait_for(first_route_loaded.wait(), timeout=5)
    change_task = asyncio.create_task(change_route_after_checkpoint())
    result = await asyncio.wait_for(delivery_task, timeout=5)
    await asyncio.wait_for(change_task, timeout=5)

    assert result == "SENT"
    assert route_call_count == 3
    assert route_account_ids == [seed.bot_id, seed.bot_id, seed.second_bot_id]
    async with get_session_factory()() as verify:
        config = await verify.get(models.TenantFeishuHandoffConfig, seed.config_id)
    assert config is not None
    assert config.feishu_platform_account_id == seed.second_bot_id
    assert config.config_version == 2


async def test_route_guard_serializes_missing_creation_and_route_change(session):
    session.add_all(
        [
            _feishu_account(_BOT_ID, name="route-guard-bot"),
            _feishu_account(_SECOND_BOT_ID, name="route-guard-bot-second"),
        ]
    )
    await session.commit()
    seed_rows = [
        models.PlatformAccount(
            id=_SOURCE_ACCOUNT_ID,
            tenant_id=_TENANT_ID,
            brand_id="route-lock-source-brand",
            platform="telegram",
            name="route-lock-source",
            external_account_id="route-lock-source-external",
            public_id="route-lock-source-public",
            shared_with_support=True,
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4000},
            status="active",
        ),
        models.Contact(
            id=_C1_CONTACT_ID,
            tenant_id=_TENANT_ID,
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            external_user_id="route-lock-customer-1",
        ),
        models.Conversation(
            id=_C1_CONVERSATION_ID,
            tenant_id=_TENANT_ID,
            brand_id="route-lock-source-brand",
            platform="telegram",
            platform_account_id=_SOURCE_ACCOUNT_ID,
            contact_id=_C1_CONTACT_ID,
            conversation_key="route-lock-conversation-1",
        ),
        models.HumanWorkItem(
            id=_C1_WORK_ID,
            tenant_id=_TENANT_ID,
            conversation_id=_C1_CONVERSATION_ID,
            status="WAITING",
            reason_code="ROUTE_LOCK_TEST",
            version=1,
        ),
    ]
    for row in seed_rows:
        session.add(row)
        await session.flush()
    await session.commit()
    lock_key = handoff_notification_route_lock_key(_TENANT_ID)

    create_ready = asyncio.Event()
    release_create = asyncio.Event()

    async def create_route():
        async with get_session_factory()() as writer:
            await acquire_xact_lock(writer, lock_key)
            writer.add(
                models.TenantFeishuHandoffConfig(
                    id=_CONFIG_ID,
                    tenant_id=_TENANT_ID,
                    feishu_platform_account_id=_BOT_ID,
                    destination_chat_id=_DESTINATION_CHAT_ID,
                    enabled=True,
                    config_version=1,
                )
            )
            await writer.flush()
            create_ready.set()
            await release_create.wait()
            await writer.commit()

    async def ensure_route():
        async with get_session_factory()() as reader:
            route_values = await lock_handoff_notification_route(
                reader,
                tenant_id=_TENANT_ID,
            )
            work = await reader.get(models.HumanWorkItem, _C1_WORK_ID)
            assert work is not None
            intent = await ensure_handoff_notification_intent(reader, work=work)
            await reader.commit()
            return route_values, intent

    async def read_route():
        async with get_session_factory()() as reader:
            route_values = await lock_handoff_notification_route(
                reader,
                tenant_id=_TENANT_ID,
            )
            await reader.commit()
            return route_values

    create_task = asyncio.create_task(create_route())
    await asyncio.wait_for(create_ready.wait(), timeout=5)
    ensure_task = asyncio.create_task(ensure_route())
    await asyncio.sleep(0.05)
    assert not ensure_task.done()
    release_create.set()
    created_route, created_intent = await asyncio.wait_for(ensure_task, timeout=5)
    await asyncio.wait_for(create_task, timeout=5)
    assert created_route["status"] == "PENDING"
    assert created_route["feishu_platform_account_id"] == _BOT_ID
    assert created_route["config_version"] == 1
    assert created_intent.feishu_platform_account_id == _BOT_ID
    assert created_intent.notification_config_id == _CONFIG_ID

    change_ready = asyncio.Event()
    release_change = asyncio.Event()

    async def change_route():
        async with get_session_factory()() as writer:
            await acquire_xact_lock(writer, lock_key)
            await writer.execute(
                update(models.TenantFeishuHandoffConfig)
                .where(models.TenantFeishuHandoffConfig.id == _CONFIG_ID)
                .values(
                    feishu_platform_account_id=_SECOND_BOT_ID,
                    destination_chat_id="oc-route-lock-support-v2",
                    config_version=2,
                )
            )
            change_ready.set()
            await release_change.wait()
            await writer.commit()

    change_task = asyncio.create_task(change_route())
    await asyncio.wait_for(change_ready.wait(), timeout=5)
    changed_read_task = asyncio.create_task(read_route())
    await asyncio.sleep(0.05)
    assert not changed_read_task.done()
    release_change.set()
    changed_route = await asyncio.wait_for(changed_read_task, timeout=5)
    await asyncio.wait_for(change_task, timeout=5)
    assert changed_route["status"] == "PENDING"
    assert changed_route["feishu_platform_account_id"] == _SECOND_BOT_ID
    assert changed_route["destination_chat_id"] == "oc-route-lock-support-v2"
    assert changed_route["config_version"] == 2


async def test_blocked_route_stops_delivery_and_persists_failure_facts(session, monkeypatch):
    seed = await _seed_shared_source(session)
    await session.execute(
        update(models.TenantFeishuHandoffConfig)
        .where(models.TenantFeishuHandoffConfig.id == seed.config_id)
        .values(enabled=False, config_version=2)
    )
    await session.commit()
    monkeypatch.setattr(outbox_module, "make_killswitch_checker", lambda: _OpenKillSwitch())
    send_calls: list[object] = []

    async def unexpected_sender(_account_id):
        send_calls.append(_account_id)
        raise AssertionError("blocked notification route must not send")

    monkeypatch.setattr(outbox_module, "get_platform_sender", unexpected_sender)
    result = await deliver_outbox(str(seed.c1_outbox_id))

    assert result == "CANCELLED"
    assert send_calls == []
    async with get_session_factory()() as verify:
        intent = await verify.scalar(
            select(models.HandoffNotificationIntent).where(
                models.HandoffNotificationIntent.conversation_id == seed.c1_conversation_id
            )
        )
    assert intent is not None
    assert intent.status == "BLOCKED_CONFIG"
    assert intent.last_error_code == "FEISHU_HANDOFF_ROUTE_DISABLED"


async def test_locked_notification_bot_is_revalidated_before_public_send(session, monkeypatch):
    seed = await _seed_shared_source(session)
    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == seed.bot_id)
        .values(status="DISABLED")
    )
    await session.commit()

    async def stale_route(_session, *, tenant_id: str):
        assert tenant_id == seed.tenant_id
        return {
            "notification_config_id": seed.config_id,
            "config_version": 1,
            "feishu_platform_account_id": seed.bot_id,
            "destination_chat_id": _DESTINATION_CHAT_ID,
            "status": "PENDING",
            "last_error_code": None,
        }

    monkeypatch.setattr(outbox_module, "lock_handoff_notification_route", stale_route)
    outbox = await session.get(models.OutboxMessage, seed.c1_outbox_id)
    assert outbox is not None
    result = await outbox_module._public_bot_send_preflight(
        session,
        outbox=outbox,
        payload_text="route-lock reply",
    )

    assert result == "PUBLIC_SEND_ROUTE_INVALID"
