from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.company_permission_support import (
    create_staff,
    seed_feishu_handoff,
    signed_card_request,
    signed_feishu_body,
)

from apps.api.main import create_app
from social_reply.application.account_management import human_workflow, staff_lifecycle
from social_reply.application.account_management.human_workflow import (
    send_human_reply,
    start_human_reception,
    transfer_human_work_item,
)
from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority
from social_reply.application.handoff_notifications import callbacks as callbacks_module
from social_reply.application.handoff_notifications.callbacks import (
    FeishuCardActionError,
    handle_feishu_card_action,
)
from social_reply.application.handoff_notifications.projection import render_current_handoff_card
from social_reply.application.handoff_notifications.service import (
    advance_handoff_notification_for_work,
)
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


def _client(*, feishu_enabled: bool = True) -> httpx.AsyncClient:
    settings = get_settings().model_copy(update={"feishu_enabled": feishu_enabled})
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://test",
        follow_redirects=False,
    )


async def _notification_public_id(account_id: uuid.UUID) -> str:
    async with get_session_factory()() as fresh:
        public_id = await fresh.scalar(
            select(models.PlatformAccount.public_id).where(models.PlatformAccount.id == account_id)
        )
        if not isinstance(public_id, str):
            raise AssertionError("Feishu notification public ID missing")
        return public_id


def _normal_feishu_payload(*, app_id: str, token: str, event_id: str) -> dict:
    now = str(int(time.time() * 1000))
    return {
        "schema": "2.0",
        "token": "top-level-token-is-not-used-as-proof",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": now,
            "token": token,
            "app_id": app_id,
            "tenant_key": "company-tenant-key",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou-company-customer"},
                "sender_type": "user",
            },
            "message": {
                "message_id": f"om-{event_id}",
                "chat_id": f"oc-{event_id}",
                "chat_type": "p2p",
                "message_type": "text",
                "create_time": now,
                "content": '{"text":"signed company callback"}',
            },
        },
    }


@pytest.mark.parametrize("encrypted", [False, True])
async def test_feishu_normal_events_require_both_encryption_and_signature(
    session, monkeypatch, encrypted
):
    staff = await create_staff(session, username=f"company-root-signature-{encrypted}")
    seed = await seed_feishu_handoff(session, staff=(staff,), work_status="WAITING")
    public_id = await _notification_public_id(seed.notification_account_id)
    payload = _normal_feishu_payload(
        app_id=seed.app_id,
        token=seed.verification_token,
        event_id=f"company-root-{encrypted}",
    )
    body, headers = signed_feishu_body(
        payload,
        encrypt_key=seed.encrypt_key,
        encrypted=encrypted,
    )

    async def ignore_dispatch(_raw_event_id):
        return None

    monkeypatch.setattr(
        "social_reply.connectors.feishu.router.dispatch_initial_raw_event",
        ignore_dispatch,
    )
    async with _client(feishu_enabled=True) as client:
        signed = await client.post(f"/webhooks/feishu/{public_id}", content=body, headers=headers)
        unsigned = await client.post(
            f"/webhooks/feishu/{public_id}",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert signed.status_code == (200 if encrypted else 401)
    if not encrypted:
        assert signed.json() == {"detail": "invalid_feishu_request"}
    assert unsigned.status_code == 401
    assert unsigned.json() == {"detail": "invalid_feishu_request"}


async def test_same_feishu_bot_callbacks_serialize_receipt_and_account_locks(session, monkeypatch):
    staff_a = await create_staff(session, username="company-feishu-concurrent-a")
    staff_b = await create_staff(session, username="company-feishu-concurrent-b")
    assert staff_a.principal.session_id != staff_b.principal.session_id
    seed = await seed_feishu_handoff(
        session,
        staff=(staff_a, staff_b),
        conversation_count=2,
        work_status="WAITING",
    )
    conversation_a, conversation_b = seed.conversations
    request_a = signed_card_request(
        seed,
        conversation_a,
        operator_open_id=seed.operator_open_ids[0],
        action="claim",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation_a.action_nonce,
        event_id="company-concurrent-a",
    )
    request_b = signed_card_request(
        seed,
        conversation_b,
        operator_open_id=seed.operator_open_ids[1],
        action="claim",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation_b.action_nonce,
        event_id="company-concurrent-b",
    )
    receipt_inserted_a = asyncio.Event()
    release_a = asyncio.Event()
    account_lock_attempted_b = asyncio.Event()
    account_lock_completed_b = asyncio.Event()
    callback_event: ContextVar[str | None] = ContextVar("company_callback_event", default=None)
    original_execute = AsyncSession.execute
    original_scalars = AsyncSession.scalars

    def is_account_lock(statement) -> bool:
        sql = str(statement).upper()
        return "PLATFORM_ACCOUNTS" in sql and "FOR UPDATE" in sql

    async def instrumented_execute(self, statement, *args, **kwargs):
        current_event = callback_event.get()
        account_lock = current_event == request_b.event_id and is_account_lock(statement)
        if account_lock:
            account_lock_attempted_b.set()
        result = await original_execute(self, statement, *args, **kwargs)
        if account_lock:
            account_lock_completed_b.set()
        sql = str(statement).lower()
        if (
            current_event == request_a.event_id
            and "feishu_card_action_receipts" in sql
            and sql.lstrip().startswith("insert")
        ):
            receipt_inserted_a.set()
            await release_a.wait()
        return result

    async def instrumented_scalars(self, statement, *args, **kwargs):
        account_lock = callback_event.get() == request_b.event_id and is_account_lock(statement)
        if account_lock:
            account_lock_attempted_b.set()
        result = await original_scalars(self, statement, *args, **kwargs)
        if account_lock:
            account_lock_completed_b.set()
        return result

    monkeypatch.setattr(AsyncSession, "execute", instrumented_execute)
    monkeypatch.setattr(AsyncSession, "scalars", instrumented_scalars)

    async def run_callback(request):
        token = callback_event.set(request.event_id)
        try:
            return await handle_feishu_card_action(
                account_id=seed.notification_account_id,
                tenant_id=seed.tenant_id,
                provider_event_id=request.event_id,
                request_digest=request.proof,
                event=request.payload["event"],
                feature_enabled=True,
            )
        finally:
            callback_event.reset(token)

    task_a = asyncio.create_task(run_callback(request_a))
    await asyncio.wait_for(receipt_inserted_a.wait(), timeout=5)
    task_b = asyncio.create_task(run_callback(request_b))
    await asyncio.wait_for(account_lock_attempted_b.wait(), timeout=5)
    assert not account_lock_completed_b.is_set()
    assert not task_b.done()

    async with get_session_factory()() as probe:
        assert (
            await probe.scalar(
                select(models.FeishuCardActionReceipt.id).where(
                    models.FeishuCardActionReceipt.provider_event_id == request_b.event_id
                )
            )
            is None
        )
    release_a.set()
    result_a, result_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
    assert account_lock_completed_b.is_set()
    assert task_b.done()
    assert result_a["toast"]["type"] == "success"
    assert result_b["toast"]["type"] == "success"

    async with get_session_factory()() as fresh:
        work_a = await fresh.get(models.HumanWorkItem, conversation_a.work_id)
        work_b = await fresh.get(models.HumanWorkItem, conversation_b.work_id)
        receipts = list(
            (
                await fresh.scalars(
                    select(models.FeishuCardActionReceipt).order_by(
                        models.FeishuCardActionReceipt.provider_event_id
                    )
                )
            ).all()
        )
        assert work_a is not None and work_b is not None
        assert work_a.status == work_b.status == "CLAIMED"
        assert work_a.assigned_user_id == staff_a.user_id
        assert work_b.assigned_user_id == staff_b.user_id
        assert {receipt.outcome for receipt in receipts} == {"SUCCEEDED"}
        assert len(receipts) == 2


async def test_feishu_callback_resolve_and_staff_revoke_converge_without_lost_authority(
    session,
    monkeypatch,
):
    staff = await create_staff(session, username="company-feishu-revoke-race")
    seed = await seed_feishu_handoff(session, staff=(staff,), work_status="CLAIMED")
    conversation = seed.conversations[0]
    request = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="company-resolve-revoke-race",
    )
    pending_outbox_id = uuid.uuid4()
    session.add(
        models.OutboxMessage(
            id=pending_outbox_id,
            tenant_id=seed.tenant_id,
            conversation_id=conversation.conversation_id,
            platform_account_id=conversation.customer_account_id,
            destination_type="telegram_dm",
            destination_id=f"chat-{conversation.conversation_id}",
            message_type="text",
            payload={
                "text": "pending before authority race",
                "visibility": "public",
                "target": {"chat_id": f"chat-{conversation.conversation_id}"},
            },
            reply_to_message_id=conversation.message_id,
            origin_kind="MANUAL_REPLY",
            actor_kind="ADMIN_HUMAN",
            actor_id=staff.principal.actor,
            initiator_user_id=staff.user_id,
            initiator_session_id=staff.principal.session_id,
            human_work_item_version=1,
            idempotency_key=f"company-revoke-race-{uuid.uuid4()}",
            status="PENDING",
        )
    )
    await session.commit()
    revoke_lock_acquired = asyncio.Event()
    release_revoke = asyncio.Event()
    callback_lock_attempted = asyncio.Event()
    callback_lock_completed = asyncio.Event()
    original_revoke_lock = staff_lifecycle.lock_user_authority
    original_callback_lock = callbacks_module.lock_user_authority

    async def paused_revoke_lock(lock_session, user_id):
        result = await original_revoke_lock(lock_session, user_id)
        if user_id == staff.user_id:
            revoke_lock_acquired.set()
            await release_revoke.wait()
        return result

    async def observed_callback_lock(lock_session, user_id):
        if user_id == staff.user_id:
            callback_lock_attempted.set()
        result = await original_callback_lock(lock_session, user_id)
        if user_id == staff.user_id:
            callback_lock_completed.set()
        return result

    monkeypatch.setattr(staff_lifecycle, "lock_user_authority", paused_revoke_lock)
    monkeypatch.setattr(callbacks_module, "lock_user_authority", observed_callback_lock)

    async def callback():
        return await handle_feishu_card_action(
            account_id=seed.notification_account_id,
            tenant_id=seed.tenant_id,
            provider_event_id=request.event_id,
            request_digest=request.proof,
            event=request.payload["event"],
            feature_enabled=True,
        )

    async def revoke():
        async with get_session_factory()() as revocation_session:
            result = await revoke_staff_authority(
                revocation_session,
                user_id=staff.user_id,
                reason="Concurrent Feishu authority revocation",
            )
            await revocation_session.commit()
            return result

    revoke_task = asyncio.create_task(revoke())
    await asyncio.wait_for(revoke_lock_acquired.wait(), timeout=5)
    callback_task = asyncio.create_task(callback())
    try:
        await asyncio.wait_for(callback_lock_attempted.wait(), timeout=5)
        assert not callback_lock_completed.is_set()
        assert not callback_task.done()
    finally:
        release_revoke.set()
    callback_result, revoke_result = await asyncio.wait_for(
        asyncio.gather(callback_task, revoke_task, return_exceptions=True), timeout=5
    )
    assert callback_lock_completed.is_set()
    if isinstance(revoke_result, BaseException):
        pytest.fail(f"staff revocation transaction failed: {revoke_result!r}")
    if isinstance(callback_result, BaseException):
        assert isinstance(callback_result, FeishuCardActionError)
    else:
        assert callback_result["toast"]["type"] == "error"
    assert not isinstance(callback_result, dict) or callback_result["toast"]["type"] != "success"

    async with get_session_factory()() as fresh:
        operator = await fresh.scalar(
            select(models.FeishuHandoffOperator).where(
                models.FeishuHandoffOperator.operator_open_id == seed.operator_open_ids[0]
            )
        )
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await fresh.get(models.HandoffNotificationIntent, conversation.intent_id)
        outbox = await fresh.get(models.OutboxMessage, pending_outbox_id)
        assert (
            operator is not None and work is not None and intent is not None and outbox is not None
        )
        assert operator.status == "DISABLED"
        assert work.status == "WAITING"
        assert intent.desired_card_state == "WAITING"
        assert outbox.status == "CANCELLED"
    follow_up = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="company-after-revoke",
    )
    try:
        follow_up_result = await handle_feishu_card_action(
            account_id=seed.notification_account_id,
            tenant_id=seed.tenant_id,
            provider_event_id=follow_up.event_id,
            request_digest=follow_up.proof,
            event=follow_up.payload["event"],
            feature_enabled=True,
        )
    except FeishuCardActionError:
        follow_up_result = None
    assert follow_up_result is None or follow_up_result["toast"]["type"] != "success"


async def test_claimed_transfer_force_refreshes_projection_and_fences_old_card(
    session, monkeypatch
):
    staff_a = await create_staff(session, username="company-card-transfer-a")
    staff_b = await create_staff(session, username="company-card-transfer-b")
    seed = await seed_feishu_handoff(
        session,
        staff=(staff_a, staff_b),
        work_status="CLAIMED",
        assigned_user_ids=(staff_a.user_id,),
    )
    conversation = seed.conversations[0]
    await transfer_human_work_item(
        work_item_id=conversation.work_id,
        allowed_tenants=staff_a.principal.allowed_tenants,
        actor=staff_a.principal.actor,
        user_id=staff_a.user_id,
        target_user_id=staff_b.user_id,
        expected_version=1,
        principal=staff_a.principal,
    )

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await fresh.get(models.HandoffNotificationIntent, conversation.intent_id)
        current_conversation = await fresh.get(models.Conversation, conversation.conversation_id)
        state = await fresh.get(models.AutomationState, conversation.conversation_id)
        assert work is not None and intent is not None
        assert current_conversation is not None and state is not None
        assert work.assigned_user_id == staff_b.user_id
        assert work.version == 2
        assert intent.desired_card_state == "CLAIMED"
        assert intent.desired_revision == 2
        assert intent.status == "PENDING"
        assert intent.action_nonce != conversation.action_nonce
        new_nonce = intent.action_nonce
        card = await render_current_handoff_card(
            fresh,
            intent=intent,
            conversation=current_conversation,
            work=work,
            state=state,
        )

    card_content = card["body"]["elements"][0]["content"]
    assert staff_b.username in card_content
    resolve_button = next(element for element in card["body"]["elements"] if element.get("value"))
    assert resolve_button["value"]["expected_work_version"] == 2
    assert resolve_button["value"]["expected_card_revision"] == 2
    assert resolve_button["value"]["action_nonce"] == str(new_nonce)

    old_request = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="company-old-card-after-transfer",
    )
    old_result = await handle_feishu_card_action(
        account_id=seed.notification_account_id,
        tenant_id=seed.tenant_id,
        provider_event_id=old_request.event_id,
        request_digest=old_request.proof,
        event=old_request.payload["event"],
        feature_enabled=True,
    )
    assert old_result["toast"] == {
        "type": "error",
        "content": "工单当前无法执行该操作",
    }
    stale_owner_request = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[1],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="company-current-owner-stale-card-after-transfer",
    )
    stale_owner_result = await handle_feishu_card_action(
        account_id=seed.notification_account_id,
        tenant_id=seed.tenant_id,
        provider_event_id=stale_owner_request.event_id,
        request_digest=stale_owner_request.proof,
        event=stale_owner_request.payload["event"],
        feature_enabled=True,
    )
    # Transfer invalidates the proof nonce before work-version conflict handling.
    assert stale_owner_result["toast"] == old_result["toast"]
    async with get_session_factory()() as unchanged:
        current_work = await unchanged.get(models.HumanWorkItem, conversation.work_id)
        current_intent = await unchanged.get(
            models.HandoffNotificationIntent, conversation.intent_id
        )
        assert current_work.assigned_user_id == staff_b.user_id
        assert current_work.status == "CLAIMED"
        assert current_work.version == 2
        assert current_intent.desired_revision == 2
        assert current_intent.action_nonce == new_nonce
    sent: list[str] = []

    class FakeSender:
        async def send_text(self, *, target: dict, text: str) -> str:
            sent.append(text)
            return "provider-after-transfer"

    async def no_dispatch(*_args, **_kwargs):
        return None

    async def fake_get_sender(account_id):
        assert account_id == conversation.customer_account_id
        return FakeSender()

    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)
    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_get_sender)
    outbox_id = await send_human_reply(
        conversation_id=conversation.conversation_id,
        reply_to_message_id=conversation.message_id,
        text="B replies after the transfer",
        idempotency_key=str(uuid.uuid4()),
        allowed_tenants=staff_b.principal.allowed_tenants,
        actor=staff_b.principal.actor,
        user_id=staff_b.user_id,
        work_item_id=conversation.work_id,
        expected_version=2,
        principal=staff_b.principal,
    )
    assert await outbox_module.deliver_outbox(str(outbox_id)) == "SENT"
    assert sent == ["B replies after the transfer"]
    async with get_session_factory()() as delivered:
        delivered_outbox = await delivered.get(models.OutboxMessage, outbox_id)
        assert delivered_outbox is not None
        assert delivered_outbox.status == "SENT"
        assert delivered_outbox.human_work_item_version == 2

    new_request = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[1],
        action="resolve",
        expected_work_version=2,
        expected_card_revision=2,
        action_nonce=new_nonce,
        event_id="company-new-card-after-transfer",
    )
    new_result = await handle_feishu_card_action(
        account_id=seed.notification_account_id,
        tenant_id=seed.tenant_id,
        provider_event_id=new_request.event_id,
        request_digest=new_request.proof,
        event=new_request.payload["event"],
        feature_enabled=True,
    )
    assert new_result["toast"]["type"] == "success"


async def test_admin_takeover_force_refreshes_existing_claimed_notification(session):
    staff_a = await create_staff(session, username="company-takeover-assigned")
    manager = await create_staff(
        session,
        username="company-takeover-manager",
        role="WORKSPACE_ADMIN",
    )
    seed = await seed_feishu_handoff(
        session,
        staff=(staff_a, manager),
        work_status="CLAIMED",
        assigned_user_ids=(staff_a.user_id,),
    )
    conversation = seed.conversations[0]
    old_nonce = conversation.action_nonce
    await start_human_reception(
        conversation_id=conversation.conversation_id,
        principal=manager.principal,
    )

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await fresh.get(models.HandoffNotificationIntent, conversation.intent_id)
        current_conversation = await fresh.get(models.Conversation, conversation.conversation_id)
        state = await fresh.get(models.AutomationState, conversation.conversation_id)
        assert work is not None and intent is not None
        assert current_conversation is not None and state is not None
        assert work.assigned_user_id == manager.user_id
        assert work.version == 2
        assert intent.desired_card_state == "CLAIMED"
        assert intent.desired_revision == 2
        assert intent.action_nonce != old_nonce
        assert intent.status == "PENDING"
        new_nonce = intent.action_nonce
        card = await render_current_handoff_card(
            fresh,
            intent=intent,
            conversation=current_conversation,
            work=work,
            state=state,
        )

    assert manager.username in card["body"]["elements"][0]["content"]
    resolve_button = next(element for element in card["body"]["elements"] if element.get("value"))
    assert resolve_button["value"]["expected_work_version"] == 2
    assert resolve_button["value"]["expected_card_revision"] == 2
    assert resolve_button["value"]["action_nonce"] == str(new_nonce)

    old_request = signed_card_request(
        seed,
        conversation,
        operator_open_id=seed.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=old_nonce,
        event_id="company-old-card-after-takeover",
    )
    old_result = await handle_feishu_card_action(
        account_id=seed.notification_account_id,
        tenant_id=seed.tenant_id,
        provider_event_id=old_request.event_id,
        request_digest=old_request.proof,
        event=old_request.payload["event"],
        feature_enabled=True,
    )
    assert old_result["toast"] == {
        "type": "error",
        "content": "工单当前无法执行该操作",
    }
    async with get_session_factory()() as unchanged:
        current_work = await unchanged.get(models.HumanWorkItem, conversation.work_id)
        current_intent = await unchanged.get(
            models.HandoffNotificationIntent, conversation.intent_id
        )
        assert current_work.assigned_user_id == manager.user_id
        assert current_work.status == "CLAIMED"
        assert current_work.version == 2
        assert current_intent.action_nonce == new_nonce


async def test_sending_projection_keeps_lease_when_force_refresh_raises_desired_revision(
    session,
):
    staff = await create_staff(session, username="company-card-sending")
    seed = await seed_feishu_handoff(session, staff=(staff,), work_status="CLAIMED")
    conversation = seed.conversations[0]
    claim_token = uuid.uuid4()
    claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    async with get_session_factory()() as mutate:
        intent = await mutate.get(models.HandoffNotificationIntent, conversation.intent_id)
        assert intent is not None
        old_revision = intent.desired_revision
        old_nonce = intent.action_nonce
        intent.status = "SENDING"
        intent.claim_token = claim_token
        intent.claim_expires_at = claim_expires_at
        intent.sending_revision = old_revision
        await mutate.commit()

    async with get_session_factory()() as refresh:
        work = await refresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await refresh.get(models.HandoffNotificationIntent, conversation.intent_id)
        assert work is not None and intent is not None
        updated = await advance_handoff_notification_for_work(
            refresh,
            work=work,
            force_refresh=True,
        )
        await refresh.commit()
        assert updated is not None
        assert updated.desired_card_state == "CLAIMED"
        assert updated.desired_revision == old_revision + 1
        assert updated.action_nonce != old_nonce
        assert updated.status == "SENDING"
        assert updated.claim_token == claim_token
        assert updated.claim_expires_at == claim_expires_at
        assert updated.sending_revision == old_revision
