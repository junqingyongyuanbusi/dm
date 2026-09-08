from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select, update
from tests.integration.company_permission_support import (
    create_staff,
    login_client,
    seed_conversation,
    seed_feishu_handoff,
    signed_card_request,
)

from apps.api.main import create_app
from social_reply.application.account_management import human_workflow
from social_reply.application.account_management.auth import hash_password
from social_reply.application.account_management.human_workflow import send_human_reply
from social_reply.application.handoff_notifications.callbacks import handle_feishu_card_action
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _load_work(conversation_id: uuid.UUID) -> tuple[uuid.UUID, int, str, uuid.UUID | None]:
    async with get_session_factory()() as fresh:
        work = await fresh.scalar(
            select(models.HumanWorkItem).where(
                models.HumanWorkItem.conversation_id == conversation_id,
                models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
            )
        )
        if work is None:
            raise AssertionError("open work item not found")
        return work.id, work.version, work.status, work.assigned_user_id


async def _load_outbox_id(conversation_id: uuid.UUID) -> uuid.UUID:
    async with get_session_factory()() as fresh:
        outbox_id = await fresh.scalar(
            select(models.OutboxMessage.id)
            .where(models.OutboxMessage.conversation_id == conversation_id)
            .order_by(models.OutboxMessage.created_at.desc())
            .limit(1)
        )
        if outbox_id is None:
            raise AssertionError("human reply outbox not found")
        return outbox_id


async def _graph_snapshot(
    conversation_id: uuid.UUID,
) -> tuple[object, ...]:
    async with get_session_factory()() as fresh:
        work = await fresh.scalar(
            select(models.HumanWorkItem).where(
                models.HumanWorkItem.conversation_id == conversation_id,
                models.HumanWorkItem.status.in_(("WAITING", "CLAIMED")),
            )
        )
        state = await fresh.get(models.AutomationState, conversation_id)
        outboxes = list(
            (
                await fresh.scalars(
                    select(models.OutboxMessage)
                    .where(models.OutboxMessage.conversation_id == conversation_id)
                    .order_by(models.OutboxMessage.created_at, models.OutboxMessage.id)
                )
            ).all()
        )
        if work is None or state is None:
            raise AssertionError("conversation graph is incomplete")
        bot_outboxes = tuple(
            (row.id, row.status, row.actor_kind, row.origin_kind)
            for row in outboxes
            if row.actor_kind == "BOT"
        )
        human_outboxes = tuple(
            (row.id, row.status, row.actor_kind, row.origin_kind)
            for row in outboxes
            if row.actor_kind == "ADMIN_HUMAN"
        )
        return (
            work.id,
            work.status,
            work.version,
            work.assigned_user_id,
            work.assigned_actor,
            state.state,
            state.state_version,
            state.human_agent_id,
            bot_outboxes,
            human_outboxes,
        )


async def test_shared_saas_reception_transfers_from_a_to_b_and_fences_old_owner(
    session, monkeypatch
):
    staff_a = await create_staff(session, username="company-reception-a")
    staff_b = await create_staff(session, username="company-reception-b")
    foreign_id = uuid.uuid4()
    session.add(
        models.AdminUser(
            id=foreign_id,
            username="company-reception-foreign",
            password_hash=await hash_password("foreign-company-password-123"),
            tenant_id="tenant-other",
            role="USER",
            status="active",
            must_change_password=False,
        )
    )
    await session.commit()
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="BOT_ACTIVE",
    )

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)
    async with _client() as client_a, _client() as client_b:
        csrf_a = await login_client(
            client_a, username=staff_a.username, password=staff_a.password
        )
        csrf_b = await login_client(
            client_b, username=staff_b.username, password=staff_b.password
        )
        detail_path = f"/app/t/default/conversations/{conversation.conversation_id}"
        before = await client_a.get(detail_path)
        assert before.status_code == 200
        assert f"/conversations/{conversation.conversation_id}/start-reception" in before.text

        started = await client_a.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf_a},
        )
        assert started.status_code == 303
        work_id, work_version, _status, _assignee = await _load_work(
            conversation.conversation_id
        )
        assert work_version == 2

        owned_page = await client_a.get(detail_path)
        assert owned_page.status_code == 200
        assert f"/work-items/{work_id}/transfer" in owned_page.text
        assert f"/work-items/{work_id}/resolve" in owned_page.text
        assert str(staff_b.user_id) in owned_page.text

        foreign_transfer = await client_a.post(
            f"/app/t/default/work-items/{work_id}/transfer",
            data={
                "csrf_token": csrf_a,
                "target_user_id": str(foreign_id),
                "expected_version": str(work_version),
            },
        )
        assert foreign_transfer.status_code == 409
        assert foreign_transfer.json() == {"detail": "human_transfer_target_not_accessible"}

        transferred = await client_a.post(
            f"/app/t/default/work-items/{work_id}/transfer",
            data={
                "csrf_token": csrf_a,
                "target_user_id": str(staff_b.user_id),
                "expected_version": str(work_version),
            },
        )
        assert transferred.status_code == 303

        former_owner_page = await client_a.get(detail_path)
        assert former_owner_page.status_code == 200
        assert f"/work-items/{work_id}/transfer" not in former_owner_page.text
        assert f"/work-items/{work_id}/resolve" not in former_owner_page.text
        assert 'name="text"' not in former_owner_page.text

        former_owner_reply = await client_a.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/reply",
            data={
                "csrf_token": csrf_a,
                "reply_to_message_id": str(conversation.message_id),
                "idempotency_key": str(uuid.uuid4()),
                "work_item_id": str(work_id),
                "expected_version": str(work_version + 1),
                "text": "A must no longer reply",
            },
        )
        assert former_owner_reply.status_code == 409
        assert former_owner_reply.json() == {
            "detail": "human_work_item_assigned_to_another_user"
        }

        new_owner_page = await client_b.get(detail_path)
        assert new_owner_page.status_code == 200
        assert f"/work-items/{work_id}/resolve" in new_owner_page.text
        assert f"/conversations/{conversation.conversation_id}/reply" in new_owner_page.text

        new_owner_reply = await client_b.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/reply",
            data={
                "csrf_token": csrf_b,
                "reply_to_message_id": str(conversation.message_id),
                "idempotency_key": str(uuid.uuid4()),
                "work_item_id": str(work_id),
                "expected_version": str(work_version + 1),
                "text": "B is now replying",
            },
        )
        assert new_owner_reply.status_code == 303

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, work_id)
        reply = await fresh.scalar(
            select(models.OutboxMessage)
            .where(models.OutboxMessage.conversation_id == conversation.conversation_id)
            .order_by(models.OutboxMessage.created_at.desc())
            .limit(1)
        )
        assert work is not None and reply is not None
        assert work.assigned_user_id == staff_b.user_id
        assert work.version == work_version + 1
        assert reply.initiator_user_id == staff_b.user_id
        assert reply.actor_kind == "ADMIN_HUMAN"
        assert reply.origin_kind == "MANUAL_REPLY"
        assert reply.status == "PENDING"


async def test_workspace_admin_own_session_can_start_and_transfer_through_admin_console(
    session,
):
    manager = await create_staff(
        session,
        username="company-reception-manager",
        role="WORKSPACE_ADMIN",
    )
    target = await create_staff(session, username="company-reception-manager-target")
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="BOT_ACTIVE",
    )
    assert manager.principal.is_workspace_admin

    async with _client() as client:
        csrf = await login_client(client, username=manager.username, password=manager.password)
        admin_detail = await client.get(
            f"/admin/conversations/{conversation.conversation_id}"
        )
        assert admin_detail.status_code == 303
        assert admin_detail.headers["location"] == (
            f"/app/t/default/conversations/{conversation.conversation_id}"
        )
        page = await client.get(f"/app/t/default/conversations/{conversation.conversation_id}")
        assert page.status_code == 200
        assert f"/conversations/{conversation.conversation_id}/start-reception" in page.text

        started = await client.post(
            f"/admin/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf},
        )
        assert started.status_code == 303
        assert started.headers["location"] == (
            f"/admin/conversations/{conversation.conversation_id}"
        )
        work_id, work_version, _status, assignee = await _load_work(
            conversation.conversation_id
        )
        assert work_version == 2
        assert assignee == manager.user_id

        page = await client.get(f"/app/t/default/conversations/{conversation.conversation_id}")
        assert page.status_code == 200
        assert f"/work-items/{work_id}/transfer" in page.text
        assert f"/work-items/{work_id}/resolve" in page.text
        admin_inbox = await client.get("/admin/inbox?queue=human")
        assert admin_inbox.status_code == 303
        assert admin_inbox.headers["location"] == "/app/t/default/inbox?queue=human"

        response = await client.post(
            f"/admin/work-items/{work_id}/transfer",
            data={
                "csrf_token": csrf,
                "target_user_id": str(target.user_id),
                "version": str(work_version),
            },
        )
        assert response.status_code == 303

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, work_id)
        assert work is not None
        assert work.assigned_user_id == target.user_id
        assert work.version == work_version + 1


@pytest.mark.parametrize(
    "outbox_status,stored_status",
    [
        ("PENDING", "PENDING"),
        ("FAILED", "FAILED"),
        ("SENDING", "SENDING"),
        ("NEEDS_REVIEW", "NEEDS_REVIEW"),
        ("UNKNOWN", "UNKNOWN_HUMAN_STATUS"),
    ],
)
async def test_saas_resolve_rejects_every_unfinished_human_reply_status(
    session, monkeypatch, outbox_status, stored_status
):
    staff = await create_staff(session, username=f"company-pending-{outbox_status.lower()}")
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="BOT_ACTIVE",
    )
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def paused_dispatch(*_args, **_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()

    monkeypatch.setattr(human_workflow, "dispatch_actor", paused_dispatch)
    reply_task: asyncio.Task[httpx.Response] | None = None
    async with _client() as client:
        csrf = await login_client(client, username=staff.username, password=staff.password)
        started = await client.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf},
        )
        assert started.status_code == 303
        work_id, work_version, _status, _assignee = await _load_work(
            conversation.conversation_id
        )
        reply_task = asyncio.create_task(
            client.post(
                f"/app/t/default/conversations/{conversation.conversation_id}/reply",
                data={
                    "csrf_token": csrf,
                    "reply_to_message_id": str(conversation.message_id),
                    "idempotency_key": str(uuid.uuid4()),
                    "work_item_id": str(work_id),
                    "expected_version": str(work_version),
                    "text": "Reply whose delivery is still unresolved",
                },
            )
        )
        await asyncio.wait_for(dispatch_started.wait(), timeout=5)
        outbox_id = await _load_outbox_id(conversation.conversation_id)
        async with get_session_factory()() as mutate:
            await mutate.execute(
                update(models.OutboxMessage)
                .where(models.OutboxMessage.id == outbox_id)
                .values(status=stored_status)
            )
            await mutate.commit()

        try:
            pending_page = await client.get(
                f"/app/t/default/conversations/{conversation.conversation_id}"
            )
            assert pending_page.status_code == 200
            assert f"/work-items/{work_id}/resolve" not in pending_page.text
            assert 'class="saas-alert"' in pending_page.text

            before = await _graph_snapshot(conversation.conversation_id)
            response = await client.post(
                f"/app/t/default/work-items/{work_id}/resolve",
                data={
                    "csrf_token": csrf,
                    "expected_version": str(work_version),
                },
            )
            assert response.status_code == 409
            assert response.json() == {"detail": "human_reply_delivery_pending"}
            after = await _graph_snapshot(conversation.conversation_id)
            assert after == before
        finally:
            release_dispatch.set()
            assert reply_task is not None
            reply = await asyncio.wait_for(reply_task, timeout=5)
            assert reply.status_code == 303


async def test_delivered_human_reply_can_resolve_only_after_real_outbox_send(
    session, monkeypatch
):
    staff = await create_staff(session, username="company-delivery-success")
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="BOT_ACTIVE",
    )
    sent: list[tuple[dict, str]] = []
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    class FakeSender:
        async def send_text(self, *, target: dict, text: str) -> str:
            sent.append((target, text))
            return "provider-company-message"

    async def fake_get_sender(account_id):
        assert account_id == conversation.account_id
        return FakeSender()

    async def paused_dispatch(*_args, **_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()

    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_get_sender)
    monkeypatch.setattr(human_workflow, "dispatch_actor", paused_dispatch)
    async with _client() as client:
        csrf = await login_client(client, username=staff.username, password=staff.password)
        started = await client.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf},
        )
        assert started.status_code == 303
        work_id, work_version, _status, _assignee = await _load_work(
            conversation.conversation_id
        )
        reply_task = asyncio.create_task(
            client.post(
                f"/app/t/default/conversations/{conversation.conversation_id}/reply",
                data={
                    "csrf_token": csrf,
                    "reply_to_message_id": str(conversation.message_id),
                    "idempotency_key": str(uuid.uuid4()),
                    "work_item_id": str(work_id),
                    "expected_version": str(work_version),
                    "text": "A delivered reply",
                },
            )
        )
        await asyncio.wait_for(dispatch_started.wait(), timeout=5)
        outbox_id = await _load_outbox_id(conversation.conversation_id)
        release_dispatch.set()
        reply = await asyncio.wait_for(reply_task, timeout=5)
        assert reply.status_code == 303
        delivery_result = await outbox_module.deliver_outbox(str(outbox_id))
        assert delivery_result == "SENT"
        assert len(sent) == 1
        async with get_session_factory()() as fresh:
            outbox = await fresh.scalar(
                select(models.OutboxMessage).where(
                    models.OutboxMessage.conversation_id == conversation.conversation_id
                )
            )
            work = await fresh.get(models.HumanWorkItem, work_id)
            assert outbox is not None and work is not None
            assert outbox.status == "SENT"
            assert outbox.platform_message_id == "provider-company-message"
            assert outbox.human_work_item_version == work_version
            assert work.version == work_version

        resolved = await client.post(
            f"/app/t/default/work-items/{work_id}/resolve",
            data={"csrf_token": csrf, "expected_version": str(work_version)},
        )
        assert resolved.status_code == 303

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, work_id)
        state = await fresh.get(models.AutomationState, conversation.conversation_id)
        outbox = await fresh.scalar(
            select(models.OutboxMessage).where(
                models.OutboxMessage.conversation_id == conversation.conversation_id
            )
        )
        assert work is not None and state is not None and outbox is not None
        assert work.status == "RESOLVED"
        assert work.version == work_version + 1
        assert state.state == "BOT_ACTIVE"
        assert outbox.status == "SENT"


@pytest.mark.parametrize(
    "outbox_status,stored_status",
    [
        ("PENDING", "PENDING"),
        ("FAILED", "FAILED"),
        ("SENDING", "SENDING"),
        ("NEEDS_REVIEW", "NEEDS_REVIEW"),
        ("UNKNOWN", "UNKNOWN_HUMAN_STATUS"),
    ],
)
async def test_feishu_resolve_uses_same_pending_delivery_guard(
    session, monkeypatch, outbox_status, stored_status
):
    staff = await create_staff(session, username=f"company-feishu-pending-{outbox_status.lower()}")
    feishu = await seed_feishu_handoff(session, staff=(staff,), work_status="CLAIMED")
    conversation = feishu.conversations[0]
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def paused_dispatch(*_args, **_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()

    monkeypatch.setattr(human_workflow, "dispatch_actor", paused_dispatch)
    reply_task = asyncio.create_task(
        send_human_reply(
            conversation_id=conversation.conversation_id,
            reply_to_message_id=conversation.message_id,
            text="Reply pending before Feishu resolve",
            idempotency_key=str(uuid.uuid4()),
            allowed_tenants=staff.principal.allowed_tenants,
            actor=staff.principal.actor,
            user_id=staff.user_id,
            work_item_id=conversation.work_id,
            expected_version=1,
            principal=staff.principal,
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=5)
    outbox_id = await _load_outbox_id(conversation.conversation_id)
    async with get_session_factory()() as mutate:
        await mutate.execute(
            update(models.OutboxMessage)
            .where(models.OutboxMessage.id == outbox_id)
            .values(status=stored_status)
        )
        await mutate.commit()
    before = await _graph_snapshot(conversation.conversation_id)
    request = signed_card_request(
        feishu,
        conversation,
        operator_open_id=feishu.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
    )
    try:
        response = await handle_feishu_card_action(
            account_id=feishu.notification_account_id,
            tenant_id=feishu.tenant_id,
            provider_event_id=request.event_id,
            request_digest=request.proof,
            event=request.payload["event"],
            feature_enabled=True,
        )
        assert response["toast"]["type"] == "warning"
        assert "投递" in response["toast"]["content"]
        assert await _graph_snapshot(conversation.conversation_id) == before
        async with get_session_factory()() as fresh:
            receipt = await fresh.scalar(
                select(models.FeishuCardActionReceipt).where(
                    models.FeishuCardActionReceipt.provider_event_id == request.event_id
                )
            )
            outbox = await fresh.get(models.OutboxMessage, outbox_id)
            assert receipt is not None and outbox is not None
            assert receipt.outcome == "CONFLICT"
            assert outbox.status == stored_status
    finally:
        release_dispatch.set()
        assert await asyncio.wait_for(reply_task, timeout=5) == outbox_id


@pytest.mark.parametrize("role", ["USER", "WORKSPACE_ADMIN"])
async def test_disabled_account_history_is_readable_but_cannot_start_reception(session, role):
    staff = await create_staff(session, username=f"company-disabled-{role.lower()}", role=role)
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        account_status="DISABLED",
        state="BOT_ACTIVE",
    )

    async with _client() as client:
        csrf = await login_client(client, username=staff.username, password=staff.password)
        detail = await client.get(f"/app/t/default/conversations/{conversation.conversation_id}")
        start = await client.post(
            f"/app/t/default/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf},
        )

    assert detail.status_code == 200
    assert start.status_code == 409
    assert start.json() == {"detail": "human_account_access_denied"}


async def test_send_override_refreshes_only_when_work_version_changes(session, monkeypatch):
    manager = await create_staff(
        session,
        username="company-override-manager",
        role="WORKSPACE_ADMIN",
    )
    assigned = await create_staff(session, username="company-override-assigned")
    conversation = await seed_conversation(
        session,
        tenant_id="default",
        shared_with_support=True,
        state="HUMAN_ACTIVE",
        work_status="CLAIMED",
        assigned_user_id=assigned.user_id,
        assigned_actor=assigned.principal.actor,
        work_version=1,
    )
    intent_id = uuid.uuid4()
    old_nonce = uuid.uuid4()
    session.add(
        models.HandoffNotificationIntent(
            id=intent_id,
            public_id=uuid.uuid4(),
            tenant_id="default",
            human_work_item_id=conversation.work_id,
            conversation_id=conversation.conversation_id,
            provider_uuid=uuid.uuid4(),
            provider_message_id="om-company-override",
            status="SYNCED",
            desired_card_state="CLAIMED",
            desired_revision=1,
            delivered_revision=1,
            action_nonce=old_nonce,
        )
    )
    await session.commit()

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(human_workflow, "dispatch_actor", no_dispatch)
    first_outbox = await send_human_reply(
        conversation_id=conversation.conversation_id,
        reply_to_message_id=conversation.message_id,
        text="Workspace admin override",
        idempotency_key=str(uuid.uuid4()),
        allowed_tenants=manager.principal.allowed_tenants,
        actor=manager.principal.actor,
        user_id=manager.user_id,
        allow_override=True,
        work_item_id=conversation.work_id,
        expected_version=1,
        principal=manager.principal,
    )
    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await fresh.get(models.HandoffNotificationIntent, intent_id)
        assert work is not None and intent is not None
        assert first_outbox != intent_id
        assert work.assigned_user_id == manager.user_id
        assert work.version == 2
        assert intent.desired_revision == 2
        assert intent.action_nonce != old_nonce
        assert intent.status == "PENDING"
        refreshed_nonce = intent.action_nonce

    second_outbox = await send_human_reply(
        conversation_id=conversation.conversation_id,
        reply_to_message_id=conversation.message_id,
        text="Workspace admin override again",
        idempotency_key=str(uuid.uuid4()),
        allowed_tenants=manager.principal.allowed_tenants,
        actor=manager.principal.actor,
        user_id=manager.user_id,
        allow_override=True,
        work_item_id=conversation.work_id,
        expected_version=2,
        principal=manager.principal,
    )
    assert second_outbox != first_outbox
    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        intent = await fresh.get(models.HandoffNotificationIntent, intent_id)
        assert work is not None and intent is not None
        assert work.version == 2
        assert intent.desired_revision == 2
        assert intent.action_nonce == refreshed_nonce


async def test_feishu_resolve_succeeds_after_real_deliver_outbox_sent(session, monkeypatch):
    staff = await create_staff(session, username="company-feishu-delivery-success")
    feishu = await seed_feishu_handoff(session, staff=(staff,), work_status="CLAIMED")
    conversation = feishu.conversations[0]
    sent: list[tuple[dict, str]] = []
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    class FakeSender:
        async def send_text(self, *, target: dict, text: str) -> str:
            sent.append((target, text))
            return "provider-feishu-company-message"

    async def fake_get_sender(account_id):
        assert account_id == conversation.customer_account_id
        return FakeSender()

    async def paused_dispatch(*_args, **_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()

    monkeypatch.setattr(outbox_module, "get_platform_sender", fake_get_sender)
    monkeypatch.setattr(human_workflow, "dispatch_actor", paused_dispatch)
    reply_task = asyncio.create_task(
        send_human_reply(
            conversation_id=conversation.conversation_id,
            reply_to_message_id=conversation.message_id,
            text="Feishu customer reply delivered by the worker",
            idempotency_key=str(uuid.uuid4()),
            allowed_tenants=staff.principal.allowed_tenants,
            actor=staff.principal.actor,
            user_id=staff.user_id,
            work_item_id=conversation.work_id,
            expected_version=1,
            principal=staff.principal,
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=5)
    outbox_id = await _load_outbox_id(conversation.conversation_id)
    async with get_session_factory()() as pending_check:
        pending_outbox = await pending_check.get(models.OutboxMessage, outbox_id)
        assert pending_outbox is not None
        assert pending_outbox.status == "PENDING"
    release_dispatch.set()
    assert await asyncio.wait_for(reply_task, timeout=5) == outbox_id
    delivery_result = await outbox_module.deliver_outbox(str(outbox_id))
    assert delivery_result == "SENT"
    assert sent


    async with get_session_factory()() as fresh:
        outbox = await fresh.get(models.OutboxMessage, outbox_id)
        assert outbox is not None
        assert outbox.status == "SENT"
        assert outbox.platform_message_id == "provider-feishu-company-message"

    request = signed_card_request(
        feishu,
        conversation,
        operator_open_id=feishu.operator_open_ids[0],
        action="resolve",
        expected_work_version=1,
        expected_card_revision=1,
        action_nonce=conversation.action_nonce,
        event_id="company-feishu-resolve-after-sent",
    )
    resolved = await handle_feishu_card_action(
        account_id=feishu.notification_account_id,
        tenant_id=feishu.tenant_id,
        provider_event_id=request.event_id,
        request_digest=request.proof,
        event=request.payload["event"],
        feature_enabled=True,
    )
    assert resolved["toast"]["type"] == "success"

    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        state = await fresh.get(models.AutomationState, conversation.conversation_id)
        outbox = await fresh.get(models.OutboxMessage, outbox_id)
        assert work is not None and state is not None and outbox is not None
        assert work.status == "RESOLVED"
        assert state.state == "BOT_ACTIVE"
        assert outbox.status == "SENT"
