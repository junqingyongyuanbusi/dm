import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select
from tests.integration.company_permission_support import (
    grant_account_access,
    grant_legacy_shared_access,
)

from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
    principal_from_session_id,
)
from social_reply.application.account_management.human_workflow import (
    HumanWorkflowConflict,
    claim_human_work_item,
    send_human_reply,
    start_human_reception,
    transfer_human_work_item,
)
from social_reply.application.account_management.staff_lifecycle import revoke_staff_authority
from social_reply.application.message_delivery.intents import (
    OutboxActor,
    OutboxIdempotencyConflict,
    OutboxOrigin,
    create_or_get_outbox_intent,
)
from social_reply.application.message_delivery.outbox import (
    _validate_human_outbox_authority,
    deliver_outbox,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


async def _bootstrap_principal():
    _token, session_id = await issue_session()
    principal = await principal_from_session_id(session_id)
    assert principal is not None
    assert principal.is_superadmin
    return principal


async def _seed_conversation(
    session,
    *,
    tenant_id="tenant-a",
    shared_with_support=False,
    authorized_user_ids: tuple[uuid.UUID, ...] | None = None,
    state="BOT_ACTIVE",
    with_work=False,
):
    account_id, contact_id, conversation_id, message_id, work_id = (uuid.uuid4() for _ in range(5))
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="brand-a",
            platform="telegram",
            name="Contract account",
            external_account_id=str(account_id),
            public_id=f"contract-{account_id}",
            shared_with_support=shared_with_support,
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 1000},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.flush()
    if authorized_user_ids is not None:
        await grant_account_access(
            session, tenant_id=tenant_id, account_id=account_id, user_ids=authorized_user_ids
        )
    elif shared_with_support:
        await grant_legacy_shared_access(session, tenant_id=tenant_id, account_id=account_id)
    session.add(
        models.Contact(
            id=contact_id,
            tenant_id=tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=f"contact-{contact_id}",
        )
    )
    await session.flush()
    session.add(
        models.Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id="brand-a",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"telegram:{conversation_id}",
        )
    )
    await session.flush()
    session.add(
        models.AutomationState(
            conversation_id=conversation_id,
            state=state,
            state_version=1,
        )
    )
    session.add(
        models.Message(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text="Need a person",
            reply_target={"chat_id": "contact"},
        )
    )
    if with_work:
        session.add(
            models.HumanWorkItem(
                id=work_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                status="WAITING",
                reason_code="CONTRACT",
                version=1,
            )
        )
    await session.commit()
    return account_id, conversation_id, message_id, (work_id if with_work else None)


async def _seed_feishu_action(session, *, customer_shared: bool, employee_role: str):
    notification_account_id = uuid.uuid4()
    customer_account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    work_id = uuid.uuid4()
    config_id = uuid.uuid4()
    intent_id = uuid.uuid4()
    public_id = uuid.uuid4()
    nonce = uuid.uuid4()
    employee_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    open_id = f"ou-{uuid.uuid4().hex}"
    session.add_all(
        [
            models.PlatformAccount(
                id=notification_account_id,
                tenant_id="tenant-feishu",
                brand_id="brand-feishu",
                platform="feishu",
                name="Notification bot",
                external_account_id=f"cli-{uuid.uuid4().hex}",
                public_id=f"feishu-{uuid.uuid4().hex}",
                shared_with_support=False,
                config={"feishu_health_status": "READY"},
                capability={"dm": True, "max_text_length": 4000},
                status="active",
            ),
            models.PlatformAccount(
                id=customer_account_id,
                tenant_id="tenant-feishu",
                brand_id="brand-customer",
                platform="x",
                name="Customer account",
                external_account_id=f"telegram-{uuid.uuid4().hex}",
                public_id=f"telegram-{uuid.uuid4().hex}",
                shared_with_support=customer_shared,
                owner_user_id=owner_id if not customer_shared else None,
                config={"delivery_mode": "direct"},
                capability={"dm": True, "max_text_length": 1000},
                status="active",
            ),
            models.AdminUser(
                id=employee_id,
                username=f"employee-{uuid.uuid4().hex}",
                password_hash="test-only",
                tenant_id="tenant-feishu",
                role=employee_role,
                status="active",
                must_change_password=False,
            ),
            models.AdminUser(
                id=owner_id,
                username=f"owner-{uuid.uuid4().hex}",
                password_hash="test-only",
                tenant_id="tenant-feishu",
                role="USER",
                status="active",
                must_change_password=False,
            ),
        ]
    )
    await session.flush()
    if customer_shared:
        await grant_account_access(
            session,
            tenant_id="tenant-feishu",
            account_id=customer_account_id,
            user_ids=(employee_id,),
        )
    session.add_all(
        [
            models.Contact(
                id=contact_id,
                tenant_id="tenant-feishu",
                platform="x",
                platform_account_id=customer_account_id,
                external_user_id=f"customer-{uuid.uuid4().hex}",
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            models.Conversation(
                id=conversation_id,
                tenant_id="tenant-feishu",
                brand_id="brand-customer",
                platform="x",
                platform_account_id=customer_account_id,
                contact_id=contact_id,
                conversation_key=f"x:{conversation_id}",
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            models.AutomationState(
                conversation_id=conversation_id,
                state="HANDOFF_PENDING",
                state_version=2,
            ),
            models.Message(
                id=message_id,
                conversation_id=conversation_id,
                direction="inbound",
                sender_type="contact",
                text="Need human help",
                reply_target={"chat_id": "customer"},
            ),
            models.HumanWorkItem(
                id=work_id,
                tenant_id="tenant-feishu",
                conversation_id=conversation_id,
                status="WAITING",
                reason_code="CONTRACT",
                version=1,
            ),
            models.TenantFeishuHandoffConfig(
                id=config_id,
                tenant_id="tenant-feishu",
                feishu_platform_account_id=notification_account_id,
                destination_chat_id="oc-support",
                enabled=True,
                config_version=1,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            models.HandoffNotificationIntent(
                id=intent_id,
                public_id=public_id,
                tenant_id="tenant-feishu",
                human_work_item_id=work_id,
                conversation_id=conversation_id,
                notification_config_id=config_id,
                config_version=1,
                feishu_platform_account_id=notification_account_id,
                destination_chat_id="oc-support",
                provider_uuid=uuid.uuid4(),
                provider_message_id="om-card",
                status="SYNCED",
                desired_card_state="WAITING",
                desired_revision=1,
                delivered_revision=1,
                action_nonce=nonce,
                attempt_count=1,
            ),
            models.FeishuHandoffOperator(
                tenant_id="tenant-feishu",
                feishu_platform_account_id=notification_account_id,
                operator_open_id=open_id,
                display_name="Contract operator",
                admin_user_id=employee_id,
                can_claim=True,
                can_resolve=True,
                status="ACTIVE",
            ),
        ]
    )
    await session.commit()
    return {
        "notification_account_id": notification_account_id,
        "work_id": work_id,
        "intent_id": intent_id,
        "public_id": public_id,
        "nonce": nonce,
        "employee_id": employee_id,
        "open_id": open_id,
    }


async def test_start_reception_is_required_before_send_and_then_is_atomic(session, monkeypatch):
    _account_id, conversation_id, message_id, _work_id = await _seed_conversation(session)
    principal = await _bootstrap_principal()
    command = {
        "conversation_id": conversation_id,
        "reply_to_message_id": message_id,
        "text": "A verified human answer",
        "idempotency_key": str(uuid.uuid4()),
        "allowed_tenants": principal.allowed_tenants,
        "actor": principal.actor,
        "user_id": principal.user_id,
        "principal": principal,
    }

    with pytest.raises(HumanWorkflowConflict, match="human_reply_requires_claim"):
        await send_human_reply(**command)

    started_work_id = await start_human_reception(
        conversation_id=conversation_id,
        principal=principal,
    )

    async def skip_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "social_reply.application.account_management.human_workflow.dispatch_actor",
        skip_dispatch,
    )
    outbox_id = await send_human_reply(
        **command,
        work_item_id=started_work_id,
        expected_version=2,
    )

    session.expire_all()
    work = await session.get(models.HumanWorkItem, started_work_id)
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert work is not None
    assert (work.status, work.assigned_user_id, work.assigned_session_id) == (
        "CLAIMED",
        None,
        principal.session_id,
    )
    assert outbox is not None
    assert (
        outbox.initiator_user_id,
        outbox.initiator_session_id,
        outbox.human_work_item_version,
    ) == (None, principal.session_id, 2)

    sent = []

    class Sender:
        async def send_text(self, *, target, text):
            sent.append((target, text))
            return "provider-bootstrap"

    async def get_sender(_account_id):
        return Sender()

    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender",
        get_sender,
    )
    assert await deliver_outbox(str(outbox_id)) == "SENT"
    assert sent == [({"chat_id": "contact"}, "A verified human answer")]


async def test_two_concurrent_starters_create_one_claimed_work_item(session):
    _account_id, conversation_id, _message_id, _work_id = await _seed_conversation(session)
    first = await _bootstrap_principal()
    second = await _bootstrap_principal()

    results = await asyncio.gather(
        start_human_reception(conversation_id=conversation_id, principal=first),
        start_human_reception(conversation_id=conversation_id, principal=second),
        return_exceptions=True,
    )

    assert sum(isinstance(result, uuid.UUID) for result in results) == 1
    assert sum(isinstance(result, HumanWorkflowConflict) for result in results) == 1
    work = (
        await session.execute(
            select(models.HumanWorkItem).where(
                models.HumanWorkItem.conversation_id == conversation_id,
            )
        )
    ).scalar_one()
    state = await session.get(models.AutomationState, conversation_id)
    assert (work.status, work.assigned_user_id) == ("CLAIMED", None)
    assert work.assigned_session_id in {first.session_id, second.session_id}
    assert state is not None and state.state == "HUMAN_ACTIVE"


async def test_outbox_intent_identity_is_atomic_and_immutable(session):
    account_id, conversation_id, message_id, _work_id = await _seed_conversation(
        session,
        state="HUMAN_ACTIVE",
    )
    first = await _bootstrap_principal()
    second = await _bootstrap_principal()
    key = "same-human-intent-key"
    first_id = await create_or_get_outbox_intent(
        session,
        conversation_id=conversation_id,
        platform_account_id=account_id,
        reply_to_message_id=message_id,
        text="same text",
        origin_kind=OutboxOrigin.MANUAL_REPLY,
        actor_kind=OutboxActor.ADMIN_HUMAN,
        actor_id=first.actor,
        idempotency_key=key,
        initiator_user_id=None,
        initiator_session_id=first.session_id,
        human_work_item_version=7,
    )
    await session.commit()

    async with get_session_factory()() as other_session:
        with pytest.raises(OutboxIdempotencyConflict):
            await create_or_get_outbox_intent(
                other_session,
                conversation_id=conversation_id,
                platform_account_id=account_id,
                reply_to_message_id=message_id,
                text="same text",
                origin_kind=OutboxOrigin.MANUAL_REPLY,
                actor_kind=OutboxActor.ADMIN_HUMAN,
                actor_id=first.actor,
                idempotency_key=key,
                initiator_user_id=None,
                initiator_session_id=second.session_id,
                human_work_item_version=8,
            )
        await other_session.rollback()
    assert await session.get(models.OutboxMessage, first_id) is not None


async def test_approval_predecessor_requires_current_reviewer_identity(session):
    account_id, conversation_id, message_id, _work_id = await _seed_conversation(
        session,
        state="BOT_DRAFT_ONLY",
    )
    principal = await _bootstrap_principal()
    outbox = models.OutboxMessage(
        tenant_id="tenant-a",
        conversation_id=conversation_id,
        platform_account_id=account_id,
        destination_type="telegram_dm",
        destination_id="123",
        message_type="text",
        payload={"text": "approved text", "approval": "admin", "target": {"chat_id": "123"}},
        reply_to_message_id=message_id,
        origin_kind="DECISION",
        actor_kind="ADMIN_HUMAN",
        actor_id=principal.actor,
        initiator_user_id=None,
        initiator_session_id=principal.session_id,
        human_work_item_version=None,
        idempotency_key=f"approval-{uuid.uuid4()}",
        status="PENDING",
    )
    decision = models.ReplyDecision(
        tenant_id="tenant-a",
        conversation_id=conversation_id,
        message_id=message_id,
        action="draft",
        reply_text="approved text",
        final_reply_text="approved text",
        review_action="ACCEPTED",
        reviewed_by=principal.actor,
        reviewed_at=datetime.now(UTC),
        source="llm",
    )
    session.add_all([outbox, decision])
    await session.flush()
    decision.outbox_id = outbox.id
    await session.commit()

    assert await _validate_human_outbox_authority(session, outbox=outbox) is None
    await session.commit()
    decision.reviewed_by = "user:another-reviewer"
    await session.commit()
    assert (
        await _validate_human_outbox_authority(session, outbox=outbox)
        == "DRAFT_APPROVAL_PROVENANCE_INVALID"
    )
    await session.rollback()


async def test_legacy_human_predecessor_without_identity_never_calls_provider(session, monkeypatch):
    account_id, conversation_id, message_id, _work_id = await _seed_conversation(
        session,
        state="BOT_DRAFT_ONLY",
    )
    outbox_id = uuid.uuid4()
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="tenant-a",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="123",
            message_type="text",
            payload={
                "text": "old approved text",
                "approval": "admin",
                "target": {"chat_id": "123"},
            },
            reply_to_message_id=message_id,
            origin_kind="DECISION",
            actor_kind="ADMIN_HUMAN",
            actor_id="user:old-admin",
            initiator_user_id=None,
            initiator_session_id=None,
            human_work_item_version=None,
            idempotency_key=f"legacy-{outbox_id}",
            status="PENDING",
        )
    )
    await session.commit()
    provider_calls = []

    async def forbidden_provider(_account_id):
        provider_calls.append(_account_id)
        raise AssertionError("legacy human predecessor reached provider")

    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender",
        forbidden_provider,
    )
    assert await deliver_outbox(str(outbox_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox is not None and outbox.last_error_code == "HUMAN_INITIATOR_SESSION_MISSING"
    assert provider_calls == []


async def test_unrelated_staff_revoke_preserves_real_bootstrap_assignment(session):
    _account_id, _conversation_id, _message_id, work_id = await _seed_conversation(
        session,
        state="HANDOFF_PENDING",
        with_work=True,
    )
    bootstrap = await _bootstrap_principal()
    await claim_human_work_item(
        work_item_id=work_id,
        allowed_tenants=bootstrap.allowed_tenants,
        actor=bootstrap.actor,
        user_id=None,
        expected_version=1,
        principal=bootstrap,
    )
    unrelated_id = uuid.uuid4()
    session.add(
        models.AdminUser(
            id=unrelated_id,
            username=f"unrelated-{unrelated_id}",
            password_hash="test-only",
            tenant_id="tenant-a",
            role="USER",
            status="active",
            must_change_password=False,
        )
    )
    await session.commit()

    await revoke_staff_authority(session, user_id=unrelated_id, reason="unrelated revoke")
    await session.commit()
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    assert work is not None
    assert (
        work.status,
        work.assigned_user_id,
        work.assigned_session_id,
    ) == ("CLAIMED", None, bootstrap.session_id)


async def test_revoke_locks_and_cancels_outbox_only_conversations(session):
    account_id, conversation_id, message_id, _work_id = await _seed_conversation(session)
    user_id = uuid.uuid4()
    password = "outbox-revoke-contract-password"
    session.add(
        models.AdminUser(
            id=user_id,
            username="outbox-revoke-user",
            password_hash=await hash_password(password),
            tenant_id="tenant-a",
            role="USER",
            status="active",
            must_change_password=False,
        )
    )
    await session.commit()
    result = await authenticate("outbox-revoke-user", password)
    assert result is not None
    principal, _token = result
    outbox_id = uuid.uuid4()
    session.add(
        models.OutboxMessage(
            id=outbox_id,
            tenant_id="tenant-a",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="contact",
            message_type="text",
            payload={"text": "pending manual", "target": {"chat_id": "contact"}},
            reply_to_message_id=message_id,
            origin_kind="MANUAL_REPLY",
            actor_kind="ADMIN_HUMAN",
            actor_id=principal.actor,
            initiator_user_id=user_id,
            initiator_session_id=principal.session_id,
            human_work_item_version=1,
            idempotency_key=f"outbox-revoke-{outbox_id}",
            status="PENDING",
        )
    )
    await session.commit()

    await revoke_staff_authority(session, user_id=user_id, reason="outbox revoke")
    await session.commit()
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, outbox_id)
    assert outbox is not None
    assert (outbox.status, outbox.last_error_code) == ("CANCELLED", "STAFF_AUTHORITY_REVOKED")


async def _seed_transfer_pair(session, user_a_id: uuid.UUID, user_b_id: uuid.UUID):
    account_id = uuid.uuid4()
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id="tenant-a",
            brand_id="brand-a",
            platform="telegram",
            name="Shared transfer account",
            external_account_id=str(account_id),
            public_id=f"transfer-{account_id}",
            shared_with_support=True,
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 1000},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.flush()
    work_ids = []
    await grant_account_access(
        session, tenant_id="tenant-a", account_id=account_id, user_ids=(user_a_id, user_b_id)
    )
    for assigned_user_id in (user_a_id, user_b_id):
        contact_id, conversation_id, work_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        work_ids.append(work_id)
        session.add(
            models.Contact(
                id=contact_id,
                tenant_id="tenant-a",
                platform="telegram",
                platform_account_id=account_id,
                external_user_id=str(contact_id),
            )
        )
        await session.flush()
        session.add(
            models.Conversation(
                id=conversation_id,
                tenant_id="tenant-a",
                brand_id="brand-a",
                platform="telegram",
                platform_account_id=account_id,
                contact_id=contact_id,
                conversation_key=f"transfer:{conversation_id}",
            )
        )
        await session.flush()
        session.add(
            models.AutomationState(
                conversation_id=conversation_id,
                state="HUMAN_ACTIVE",
                state_version=2,
                human_agent_id=f"user:{assigned_user_id}",
            )
        )
        session.add(
            models.HumanWorkItem(
                id=work_id,
                tenant_id="tenant-a",
                conversation_id=conversation_id,
                status="CLAIMED",
                reason_code="TRANSFER_CONTRACT",
                assigned_user_id=assigned_user_id,
                assigned_actor=f"user:{assigned_user_id}",
                claimed_at=datetime.now(UTC),
                version=2,
            )
        )
    await session.commit()
    return work_ids


async def test_fixed_uuid_a_greater_than_b_mutual_transfer_does_not_deadlock(session):
    user_a_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    user_b_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    password = "transfer-contract-password"
    session.add_all(
        [
            models.AdminUser(
                id=user_a_id,
                username="transfer-a",
                password_hash=await hash_password(password),
                tenant_id="tenant-a",
                role="USER",
                status="active",
                must_change_password=False,
            ),
            models.AdminUser(
                id=user_b_id,
                username="transfer-b",
                password_hash=await hash_password(password),
                tenant_id="tenant-a",
                role="USER",
                status="active",
                must_change_password=False,
            ),
        ]
    )
    work_a_id, work_b_id = await _seed_transfer_pair(session, user_a_id, user_b_id)
    auth_a = await authenticate("transfer-a", password)
    auth_b = await authenticate("transfer-b", password)
    assert auth_a is not None and auth_b is not None
    principal_a, _token_a = auth_a
    principal_b, _token_b = auth_b

    await asyncio.wait_for(
        asyncio.gather(
            transfer_human_work_item(
                work_item_id=work_a_id,
                allowed_tenants=principal_a.allowed_tenants,
                actor=principal_a.actor,
                user_id=principal_a.user_id,
                target_user_id=user_b_id,
                expected_version=2,
                principal=principal_a,
            ),
            transfer_human_work_item(
                work_item_id=work_b_id,
                allowed_tenants=principal_b.allowed_tenants,
                actor=principal_b.actor,
                user_id=principal_b.user_id,
                target_user_id=user_a_id,
                expected_version=2,
                principal=principal_b,
            ),
        ),
        timeout=5,
    )
    session.expire_all()
    work_a = await session.get(models.HumanWorkItem, work_a_id)
    work_b = await session.get(models.HumanWorkItem, work_b_id)
    assert work_a is not None and work_b is not None
    assert (work_a.assigned_user_id, work_b.assigned_user_id) == (user_b_id, user_a_id)


async def test_fixed_uuid_transfer_and_revoke_share_staff_order_without_deadlock(session):
    user_a_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    user_b_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    password = "transfer-revoke-contract-password"
    session.add_all(
        [
            models.AdminUser(
                id=user_a_id,
                username="transfer-revoke-a",
                password_hash=await hash_password(password),
                tenant_id="tenant-a",
                role="USER",
                status="active",
                must_change_password=False,
            ),
            models.AdminUser(
                id=user_b_id,
                username="transfer-revoke-b",
                password_hash=await hash_password(password),
                tenant_id="tenant-a",
                role="USER",
                status="active",
                must_change_password=False,
            ),
        ]
    )
    work_a_id, _work_b_id = await _seed_transfer_pair(session, user_a_id, user_b_id)
    auth_a = await authenticate("transfer-revoke-a", password)
    assert auth_a is not None
    principal_a, _token_a = auth_a

    async def revoke_b():
        async with get_session_factory()() as revocation_session:
            await revoke_staff_authority(
                revocation_session,
                user_id=user_b_id,
                reason="fixed-order revoke",
            )
            await revocation_session.commit()

    await asyncio.wait_for(
        asyncio.gather(
            transfer_human_work_item(
                work_item_id=work_a_id,
                allowed_tenants=principal_a.allowed_tenants,
                actor=principal_a.actor,
                user_id=principal_a.user_id,
                target_user_id=user_b_id,
                expected_version=2,
                principal=principal_a,
            ),
            revoke_b(),
        ),
        timeout=5,
    )
    session.expire_all()
    work_a = await session.get(models.HumanWorkItem, work_a_id)
    assert work_a is not None
    assert work_a.assigned_user_id in {None, user_b_id}


def _signed_callback(seeded, event, event_id):
    import hashlib
    import json
    import time

    from social_reply.application.handoff_notifications.callbacks import callback_request_digest

    token, key, app_id = "verification-token", "encrypt-key", "cli-test"
    body = json.dumps({
        "header": {"event_type": "card.action.trigger", "event_id": event_id,
                   "token": token, "app_id": app_id},
        "event": event,
    }).encode()
    timestamp, nonce = str(int(time.time())), "test-callback-nonce"
    signature = hashlib.sha256((timestamp + nonce + key).encode() + body).hexdigest()
    return callback_request_digest(
        body, account_id=seeded["notification_account_id"], tenant_id="tenant-feishu",
        app_id=app_id, verification_token=token, encrypt_key=key,
        timestamp=timestamp, nonce=nonce, signature=signature,
    )


async def test_feishu_operator_uses_customer_account_scope_without_disabling_operator(session):
    from social_reply.application.handoff_notifications.callbacks import (
        handle_feishu_card_action,
    )

    seeded = await _seed_feishu_action(
        session,
        customer_shared=True,
        employee_role="USER",
    )
    event = {
        "operator": {"open_id": seeded["open_id"]},
        "open_message_id": "om-card",
        "action": {
            "value": {
                "contract_version": 1,
                "notification_id": str(seeded["public_id"]),
                "action": "claim",
                "expected_work_version": 1,
                "expected_card_revision": 1,
                "action_nonce": str(seeded["nonce"]),
            }
        },
    }
    response = await handle_feishu_card_action(
        account_id=seeded["notification_account_id"],
        tenant_id="tenant-feishu",
        provider_event_id="evt-customer-shared",
        request_digest=_signed_callback(seeded, event, "evt-customer-shared"),
        event=event,
        feature_enabled=True,
    )

    assert response["toast"]["type"] == "success"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, seeded["work_id"])
    operator = await session.scalar(
        select(models.FeishuHandoffOperator).where(
            models.FeishuHandoffOperator.operator_open_id == seeded["open_id"]
        )
    )
    assert work is not None and operator is not None
    assert work.assigned_user_id == seeded["employee_id"]
    assert operator.status == "ACTIVE"


async def test_feishu_customer_scope_rejection_does_not_disable_operator(session):
    from social_reply.application.handoff_notifications.callbacks import (
        handle_feishu_card_action,
    )

    seeded = await _seed_feishu_action(
        session,
        customer_shared=False,
        employee_role="USER",
    )
    event = {
        "operator": {"open_id": seeded["open_id"]},
        "open_message_id": "om-card",
        "action": {
            "value": {
                "contract_version": 1,
                "notification_id": str(seeded["public_id"]),
                "action": "claim",
                "expected_work_version": 1,
                "expected_card_revision": 1,
                "action_nonce": str(seeded["nonce"]),
            }
        },
    }
    response = await handle_feishu_card_action(
        account_id=seeded["notification_account_id"],
        tenant_id="tenant-feishu",
        provider_event_id="evt-customer-denied",
        request_digest=_signed_callback(seeded, event, "evt-customer-denied"),
        event=event,
        feature_enabled=True,
    )

    assert response["toast"]["type"] == "error"
    session.expire_all()
    work = await session.get(models.HumanWorkItem, seeded["work_id"])
    operator = await session.scalar(
        select(models.FeishuHandoffOperator).where(
            models.FeishuHandoffOperator.operator_open_id == seeded["open_id"]
        )
    )
    assert work is not None and operator is not None
    assert work.status == "WAITING"
    assert operator.status == "ACTIVE"


async def test_unverified_feishu_callback_digest_cannot_mint_action_proof():
    from social_reply.application.handoff_notifications.callbacks import (
        FeishuCardActionError,
        VerifiedFeishuCallback,
        handle_feishu_card_action,
    )

    event = {
        "operator": {"open_id": "ou_untrusted"},
        "action": {
            "value": {
                "contract_version": 1,
                "notification_id": str(uuid.uuid4()),
                "action": "claim",
                "expected_work_version": 1,
                "expected_card_revision": 1,
                "action_nonce": str(uuid.uuid4()),
            }
        },
        "open_message_id": "om_untrusted",
    }
    assert not VerifiedFeishuCallback(digest="a" * 64).is_verified
    with pytest.raises(FeishuCardActionError, match="verification_required"):
        await handle_feishu_card_action(
            account_id=uuid.uuid4(),
            tenant_id="tenant-a",
            provider_event_id="evt_untrusted",
            request_digest="a" * 64,
            event=event,
            feature_enabled=True,
        )


async def test_bootstrap_transfer_records_named_user_and_clears_session_source(session):
    account_id, _conversation_id, _message_id, work_id = await _seed_conversation(
        session,
        shared_with_support=True,
        state="HANDOFF_PENDING",
        with_work=True,
    )
    bootstrap = await _bootstrap_principal()
    await claim_human_work_item(
        work_item_id=work_id,
        allowed_tenants=bootstrap.allowed_tenants,
        actor=bootstrap.actor,
        user_id=None,
        expected_version=1,
        principal=bootstrap,
    )
    target_id = uuid.uuid4()
    session.add(
        models.AdminUser(
            id=target_id,
            username=f"named-target-{target_id}",
            password_hash="test-only",
            tenant_id="tenant-a",
            role="USER",
            status="active",
            must_change_password=False,
        )
    )
    await session.flush()
    await grant_account_access(
        session, tenant_id="tenant-a", account_id=account_id, user_ids=(target_id,)
    )
    await session.commit()

    await transfer_human_work_item(
        work_item_id=work_id,
        allowed_tenants=bootstrap.allowed_tenants,
        actor=bootstrap.actor,
        user_id=None,
        target_user_id=target_id,
        expected_version=2,
        principal=bootstrap,
    )
    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_id)
    assert work is not None
    assert work.assigned_user_id == target_id
    assert work.assigned_session_id is None
