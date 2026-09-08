import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from social_reply.application.account_management import channel_management, jobs
from social_reply.application.account_management.auth import (
    authenticate,
    hash_password,
    issue_session,
    principal_from_session_id,
)
from social_reply.application.account_management.system_user_management import (
    SystemUserActor,
    set_system_user_status,
)
from social_reply.application.message_delivery.intents import OutboxActor, OutboxOrigin
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration

_PASSWORD = "channel-contract-password-123"
_BOOTSTRAP_PASSWORD = "test-admin-password"


async def _create_user(session, username: str, role: str = "USER"):
    user = models.AdminUser(
        username=username,
        password_hash=await hash_password(_PASSWORD),
        tenant_id="default",
        role=role,
        must_change_password=False,
        status="active",
    )
    session.add(user)
    await session.commit()
    result = await authenticate(username, _PASSWORD)
    assert result is not None
    principal, _token = result
    return user, principal


async def _create_bootstrap():
    _token, session_id = await issue_session()
    principal = await principal_from_session_id(session_id)
    assert principal is not None
    assert principal.is_superadmin
    return principal


def _actor(principal) -> channel_management.ChannelActor:
    assert principal.session_id is not None
    return channel_management.ChannelActor(
        actor=principal.actor,
        role="ADMIN" if principal.is_admin else "USER",
        user_id=principal.user_id,
        session_id=principal.session_id,
    )


async def _create_account(session, *, owner_user_id, shared: bool = False):
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        owner_user_id=owner_user_id,
        shared_with_support=shared,
        name="Channel contract account",
        external_account_id=f"telegram-{uuid.uuid4()}",
        public_id=f"tg-{uuid.uuid4()}",
        config={},
        capability={"dm": True},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add(account)
    await session.flush()
    return account


async def _add_work(
    session,
    account: models.PlatformAccount,
    *,
    assigned_user_id,
    assigned_session_id,
    assigned_actor: str,
    version: int = 1,
):
    contact = models.Contact(
        tenant_id="default",
        platform="telegram",
        platform_account_id=account.id,
        external_user_id=f"contact-{uuid.uuid4()}",
    )
    session.add(contact)
    await session.flush()
    conversation = models.Conversation(
        tenant_id="default",
        brand_id=account.brand_id,
        platform="telegram",
        platform_account_id=account.id,
        contact_id=contact.id,
        conversation_key=f"conversation-{uuid.uuid4()}",
    )
    session.add(conversation)
    await session.flush()
    work = models.HumanWorkItem(
        tenant_id="default",
        conversation_id=conversation.id,
        status="CLAIMED",
        reason_code="HUMAN_REQUEST",
        assigned_user_id=assigned_user_id,
        assigned_session_id=assigned_session_id,
        assigned_actor=assigned_actor,
        claimed_at=datetime.now(UTC),
        version=version,
    )
    state = models.AutomationState(
        conversation_id=conversation.id,
        state="HUMAN_ACTIVE",
        state_version=1,
        human_agent_id=assigned_actor,
    )
    session.add_all([work, state])
    await session.flush()
    return conversation, work, state


async def _add_card(
    session,
    work: models.HumanWorkItem,
    *,
    status: str = "PENDING",
    desired_state: str = "CLAIMED",
    desired_revision: int = 1,
    delivered_revision: int = 0,
    provider_message_id: str | None = None,
):
    intent = models.HandoffNotificationIntent(
        tenant_id="default",
        human_work_item_id=work.id,
        conversation_id=work.conversation_id,
        provider_uuid=uuid.uuid4(),
        provider_message_id=provider_message_id,
        status=status,
        desired_card_state=desired_state,
        desired_revision=desired_revision,
        delivered_revision=delivered_revision,
        action_nonce=uuid.uuid4(),
        attempt_count=0,
    )
    if status == "SENDING":
        intent.claim_token = uuid.uuid4()
        intent.claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        intent.sending_revision = desired_revision
    session.add(intent)
    await session.flush()
    return intent


async def _add_manual_outbox(
    session,
    account: models.PlatformAccount,
    work: models.HumanWorkItem,
    *,
    initiator_user_id,
    initiator_session_id,
    status: str,
    version: int | None = None,
):
    outbox = models.OutboxMessage(
        tenant_id="default",
        conversation_id=work.conversation_id,
        platform_account_id=account.id,
        destination_type="direct",
        destination_id="recipient",
        message_type="text",
        payload={"text": "manual reply"},
        origin_kind=OutboxOrigin.MANUAL_REPLY.value,
        actor_kind=OutboxActor.ADMIN_HUMAN.value,
        actor_id="historical-actor-value",
        initiator_user_id=initiator_user_id,
        initiator_session_id=initiator_session_id,
        human_work_item_version=work.version if version is None else version,
        idempotency_key=f"channel-contract-{uuid.uuid4()}",
        status=status,
    )
    session.add(outbox)
    await session.flush()
    return outbox


async def _fresh_graph(account_id, work_id, intent_id, *outbox_ids):
    async with get_session_factory()() as session:
        account = await session.get(models.PlatformAccount, account_id)
        work = await session.get(models.HumanWorkItem, work_id)
        intent = await session.get(models.HandoffNotificationIntent, intent_id)
        outboxes = [
            await session.get(models.OutboxMessage, outbox_id)
            for outbox_id in outbox_ids
        ]
        state = await session.scalar(
            select(models.AutomationState).where(
                models.AutomationState.conversation_id == work.conversation_id
            )
        )
        return account, work, state, intent, outboxes


async def test_account_access_change_rolls_back_with_work_outbox_and_card_on_failure(
    session, migrated_db, monkeypatch
):
    old_user, old_principal = await _create_user(session, "channel-atomic-old")
    new_user, _new_principal = await _create_user(session, "channel-atomic-new")
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=old_user.id)
    conversation, work, _state = await _add_work(
        session,
        account,
        assigned_user_id=old_user.id,
        assigned_session_id=old_principal.session_id,
        assigned_actor=old_principal.actor,
    )
    outbox = await _add_manual_outbox(
        session,
        account,
        work,
        initiator_user_id=old_user.id,
        initiator_session_id=old_principal.session_id,
        status="PENDING",
    )
    intent = await _add_card(session, work)
    await session.commit()

    async def fail_card(*_args, **_kwargs):
        raise RuntimeError("injected card failure")

    monkeypatch.setattr(
        channel_management,
        "advance_handoff_notification_for_work",
        fail_card,
    )
    with pytest.raises(RuntimeError, match="injected card failure"):
        await channel_management.assign_channel_account_owner(
            tenant_id="default",
            account_id=account.id,
            actor=_actor(bootstrap),
            owner_user_id=new_user.id,
            expected_config_version=1,
        )

    account_after, work_after, state_after, intent_after, outboxes = await _fresh_graph(
        account.id, work.id, intent.id, outbox.id
    )
    assert account_after.owner_user_id == old_user.id
    assert account_after.config_version == 1
    assert work_after.status == "CLAIMED"
    assert work_after.assigned_user_id == old_user.id
    assert work_after.assigned_session_id == old_principal.session_id
    assert state_after.state == "HUMAN_ACTIVE"
    assert outboxes[0].status == "PENDING"
    assert intent_after.desired_card_state == "CLAIMED"
    assert intent_after.desired_revision == 1
    assert intent_after.status == "PENDING"
    assert conversation.id == work_after.conversation_id


@pytest.mark.parametrize("mutation", ["owner", "support"])
async def test_same_value_access_retry_releases_stale_claimed_work(
    session, migrated_db, mutation
):
    stale_user, stale_principal = await _create_user(session, f"channel-stale-{mutation}")
    keeper_user, keeper_principal = await _create_user(session, f"channel-keeper-{mutation}")
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=keeper_user.id, shared=False)
    _conversation, work, state = await _add_work(
        session,
        account,
        assigned_user_id=stale_user.id,
        assigned_session_id=stale_principal.session_id,
        assigned_actor=stale_principal.actor,
    )
    matching = await _add_manual_outbox(
        session,
        account,
        work,
        initiator_user_id=stale_user.id,
        initiator_session_id=stale_principal.session_id,
        status="PENDING",
    )
    unrelated = await _add_manual_outbox(
        session,
        account,
        work,
        initiator_user_id=keeper_user.id,
        initiator_session_id=keeper_principal.session_id,
        status="PENDING",
        version=99,
    )
    intent = await _add_card(session, work)
    old_nonce = intent.action_nonce
    if mutation == "owner":
        keeper_user.status = "disabled"
    await session.commit()

    if mutation == "owner":
        await channel_management.assign_channel_account_owner(
            tenant_id="default",
            account_id=account.id,
            actor=_actor(bootstrap),
            owner_user_id=keeper_user.id,
            expected_config_version=1,
        )
    else:
        await channel_management.set_channel_account_support_visibility(
            tenant_id="default",
            account_id=account.id,
            actor=_actor(bootstrap),
            shared=False,
            expected_config_version=1,
        )

    account_after, work_after, state_after, intent_after, outboxes = await _fresh_graph(
        account.id, work.id, intent.id, matching.id, unrelated.id
    )
    assert account_after.owner_user_id == keeper_user.id
    assert account_after.shared_with_support is False
    assert account_after.config_version == 1
    assert work_after.status == "WAITING"
    assert work_after.assigned_user_id is None
    assert work_after.assigned_session_id is None
    assert work_after.version == 2
    assert state_after.state == "HANDOFF_PENDING"
    assert outboxes[0].status == "CANCELLED"
    assert outboxes[0].last_error_code == "ACCOUNT_ACCESS_REVOKED"
    assert outboxes[1].status == "PENDING"
    assert intent_after.desired_card_state == "WAITING"
    assert intent_after.desired_revision == 2
    assert intent_after.action_nonce != old_nonce


async def test_shared_inbox_access_does_not_grant_channel_administration(session, migrated_db):
    owner, _owner_principal = await _create_user(session, "channel-shared-owner")
    _support, support_principal = await _create_user(session, "channel-shared-user")
    account = await _create_account(session, owner_user_id=owner.id, shared=True)
    await session.commit()
    assert support_principal.can_access_account(account)
    with pytest.raises(channel_management.ChannelPermissionError, match="tenant_admin_required"):
        await channel_management.rename_channel_account(
            tenant_id="default", account_id=account.id, actor=_actor(support_principal),
            name="Unauthorized rename", expected_config_version=1,
        )
    with pytest.raises(channel_management.ChannelPermissionError, match="tenant_admin_required"):
        await channel_management.set_channel_account_status(
            tenant_id="default", account_id=account.id, actor=_actor(support_principal),
            enabled=False, expected_status="active", expected_config_version=1,
        )
    await session.refresh(account)
    assert account.name == "Channel contract account"
    assert account.status == "active"
    assert account.config_version == 1
    bootstrap = await _create_bootstrap()
    await channel_management.set_channel_account_status(
        tenant_id="default", account_id=account.id, actor=_actor(bootstrap),
        enabled=False, expected_status="active", expected_config_version=1,
    )
    await session.refresh(account)
    assert account.status == "DISABLED"


async def test_reauthorize_alias_is_canonicalized_before_persisting_job(
    session, migrated_db, monkeypatch
):
    settings = channel_management.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(channel_management, "get_settings", lambda: settings)
    _admin, principal = await _create_user(
        session, "channel-canonical-admin", role="WORKSPACE_ADMIN"
    )
    account = models.PlatformAccount(
        tenant_id="default",
        brand_id="target-brand",
        platform="feishu",
        owner_user_id=None,
        shared_with_support=False,
        name="Feishu target",
        external_account_id="cli_12345678",
        public_id="feishu-target",
        config={
            "api_base_url": "https://open.feishu.cn/open-apis",
            "feishu_group_mode": "mentions_only",
        },
        capability={},
        automation_default="BOT_DRAFT_ONLY",
        status="active",
    )
    session.add(account)
    await session.commit()

    command = channel_management.build_provisioning_command(
        route_tenant_id="default",
        platform="feishu",
        actor=_actor(principal),
        operation="REAUTHORIZE_ACCOUNT",
        target_account_id=account.id,
        expected_config_version=1,
        target_brand_id=account.brand_id,
        target_external_account_id=account.external_account_id,
        values={
            "app_secret": "secret",
            "verification_token": "verification",
            "encrypt_key": "encrypt",
        },
    )
    assert command.operation == "REAUTHORIZE"

    async def ignore_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(channel_management, "dispatch_actor", ignore_dispatch)
    job_id = await channel_management.submit_channel_provisioning(command)

    async with get_session_factory()() as fresh:
        job = await fresh.get(models.ProvisioningJob, job_id)
    assert job.operation == "REAUTHORIZE"
    assert job.target_account_id == account.id


async def test_owner_change_keeps_workspace_admin_and_real_bootstrap_assignments(
    session, migrated_db
):
    old_user, old_principal = await _create_user(session, "channel-retain-old")
    new_user, _new_principal = await _create_user(session, "channel-retain-new")
    workspace_admin, workspace_principal = await _create_user(
        session, "channel-retain-admin", role="WORKSPACE_ADMIN"
    )
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=old_user.id)
    _conversation, old_work, _old_state = await _add_work(
        session,
        account,
        assigned_user_id=old_user.id,
        assigned_session_id=old_principal.session_id,
        assigned_actor=old_principal.actor,
    )
    _conversation, admin_work, _admin_state = await _add_work(
        session,
        account,
        assigned_user_id=workspace_admin.id,
        assigned_session_id=workspace_principal.session_id,
        assigned_actor=workspace_principal.actor,
    )
    _conversation, bootstrap_work, _bootstrap_state = await _add_work(
        session,
        account,
        assigned_user_id=None,
        assigned_session_id=bootstrap.session_id,
        assigned_actor="untrusted-actor-text",
    )
    await session.commit()

    await channel_management.assign_channel_account_owner(
        tenant_id="default",
        account_id=account.id,
        actor=_actor(bootstrap),
        owner_user_id=new_user.id,
        expected_config_version=1,
    )

    async with get_session_factory()() as fresh:
        old_after = await fresh.get(models.HumanWorkItem, old_work.id)
        admin_after = await fresh.get(models.HumanWorkItem, admin_work.id)
        bootstrap_after = await fresh.get(models.HumanWorkItem, bootstrap_work.id)
    assert old_after.status == "WAITING"
    assert admin_after.status == "CLAIMED"
    assert admin_after.assigned_user_id == workspace_admin.id
    assert admin_after.assigned_session_id == workspace_principal.session_id
    assert bootstrap_after.status == "CLAIMED"
    assert bootstrap_after.assigned_user_id is None
    assert bootstrap_after.assigned_session_id == bootstrap.session_id


async def test_reauthorization_grant_change_never_releases_inbox_work(session, migrated_db):
    owner, _owner_principal = await _create_user(session, "channel-grant-owner")
    grantee, grantee_principal = await _create_user(session, "channel-grant-user")
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=owner.id)
    _conversation, work, _state = await _add_work(
        session,
        account,
        assigned_user_id=grantee.id,
        assigned_session_id=grantee_principal.session_id,
        assigned_actor=grantee_principal.actor,
    )
    grant = models.AccountReauthorizationGrant(
        tenant_id="default",
        platform_account_id=account.id,
        user_id=grantee.id,
        active=True,
    )
    session.add(grant)
    await session.commit()

    await channel_management.set_channel_reauthorization_grant(
        tenant_id="default",
        account_id=account.id,
        actor=_actor(bootstrap),
        user_id=grantee.id,
        enabled=False,
        expected_config_version=1,
    )

    async with get_session_factory()() as fresh:
        work_after = await fresh.get(models.HumanWorkItem, work.id)
        account_after = await fresh.get(models.PlatformAccount, account.id)
        grant_after = await fresh.get(models.AccountReauthorizationGrant, grant.id)
    assert work_after.status == "CLAIMED"
    assert work_after.assigned_user_id == grantee.id
    assert account_after.config_version == 2
    assert grant_after.active is False


async def test_sending_card_and_manual_delivery_keep_external_send_evidence(session, migrated_db):
    old_user, old_principal = await _create_user(session, "channel-sending-old")
    new_user, _new_principal = await _create_user(session, "channel-sending-new")
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=old_user.id)
    _conversation, work, _state = await _add_work(
        session,
        account,
        assigned_user_id=old_user.id,
        assigned_session_id=old_principal.session_id,
        assigned_actor=old_principal.actor,
    )
    intent = await _add_card(
        session,
        work,
        status="SENDING",
        desired_state="CLAIMED",
        desired_revision=3,
        delivered_revision=2,
        provider_message_id="card-provider-message",
    )
    intent.next_attempt_at = datetime.now(UTC) + timedelta(minutes=2)
    outbox = await _add_manual_outbox(
        session,
        account,
        work,
        initiator_user_id=old_user.id,
        initiator_session_id=old_principal.session_id,
        status="SENDING",
    )
    outbox.locked_by = "delivery-worker"
    outbox.locked_at = datetime.now(UTC)
    outbox.platform_message_id = "reply-provider-message"
    await session.commit()

    old_card_nonce = intent.action_nonce
    old_card_claim = intent.claim_token
    old_card_expiry = intent.claim_expires_at
    old_card_sending_revision = intent.sending_revision
    old_card_next_attempt = intent.next_attempt_at
    old_outbox_lock = outbox.locked_at

    await channel_management.assign_channel_account_owner(
        tenant_id="default",
        account_id=account.id,
        actor=_actor(bootstrap),
        owner_user_id=new_user.id,
        expected_config_version=1,
    )

    account_after, work_after, _state_after, intent_after, outboxes = await _fresh_graph(
        account.id, work.id, intent.id, outbox.id
    )
    assert account_after.owner_user_id == new_user.id
    assert work_after.status == "WAITING"
    assert intent_after.status == "SENDING"
    assert intent_after.provider_message_id == "card-provider-message"
    assert intent_after.claim_token == old_card_claim
    assert intent_after.claim_expires_at == old_card_expiry
    assert intent_after.sending_revision == old_card_sending_revision
    assert intent_after.next_attempt_at == old_card_next_attempt
    assert intent_after.desired_card_state == "WAITING"
    assert intent_after.desired_revision == 4
    assert intent_after.delivered_revision == 2
    assert intent_after.action_nonce != old_card_nonce
    assert outboxes[0].status == "SENDING"
    assert outboxes[0].platform_message_id == "reply-provider-message"
    assert outboxes[0].locked_by == "delivery-worker"
    assert outboxes[0].locked_at == old_outbox_lock


async def test_access_change_restarts_full_lock_plan_after_assignee_scope_change(
    session, migrated_db, monkeypatch
):
    old_owner, old_principal = await _create_user(session, "channel-retry-snapshot-old")
    new_owner, _new_principal = await _create_user(session, "channel-retry-snapshot-new")
    replacement, replacement_principal = await _create_user(
        session, "channel-retry-snapshot-assignee", role="WORKSPACE_ADMIN"
    )
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=old_owner.id)
    _conversation, work, _state = await _add_work(
        session,
        account,
        assigned_user_id=old_owner.id,
        assigned_session_id=old_principal.session_id,
        assigned_actor=old_principal.actor,
    )
    await session.commit()

    original_snapshot = channel_management._read_account_access_snapshot
    injected = False

    async def snapshot_with_assignee_change(*args, **kwargs):
        nonlocal injected
        snapshot = await original_snapshot(*args, **kwargs)
        if not injected:
            injected = True
            async with get_session_factory()() as concurrent:
                concurrent_work = await concurrent.get(models.HumanWorkItem, work.id)
                concurrent_work.assigned_user_id = replacement.id
                concurrent_work.assigned_session_id = replacement_principal.session_id
                concurrent_work.assigned_actor = replacement_principal.actor
                await concurrent.commit()
        return snapshot

    monkeypatch.setattr(
        channel_management,
        "_read_account_access_snapshot",
        snapshot_with_assignee_change,
    )
    await channel_management.assign_channel_account_owner(
        tenant_id="default",
        account_id=account.id,
        actor=_actor(bootstrap),
        owner_user_id=new_owner.id,
        expected_config_version=1,
    )

    assert injected is True
    async with get_session_factory()() as fresh:
        account_after = await fresh.get(models.PlatformAccount, account.id)
        work_after = await fresh.get(models.HumanWorkItem, work.id)
    assert account_after.owner_user_id == new_owner.id
    assert work_after.status == "CLAIMED"
    assert work_after.assigned_user_id == replacement.id
    assert work_after.assigned_session_id == replacement_principal.session_id


async def test_access_transfer_and_staff_disable_complete_without_lock_cycle(
    session, migrated_db
):
    old_owner, _old_principal = await _create_user(session, "channel-concurrent-old")
    new_owner, _new_principal = await _create_user(session, "channel-concurrent-new")
    target_admin, target_principal = await _create_user(
        session, "channel-concurrent-target", role="WORKSPACE_ADMIN"
    )
    _survivor, _survivor_principal = await _create_user(
        session, "channel-concurrent-survivor", role="WORKSPACE_ADMIN"
    )
    bootstrap = await _create_bootstrap()
    account = await _create_account(session, owner_user_id=old_owner.id)
    _conversation, work, _state = await _add_work(
        session,
        account,
        assigned_user_id=target_admin.id,
        assigned_session_id=target_principal.session_id,
        assigned_actor=target_principal.actor,
    )
    await session.commit()

    transfer = channel_management.assign_channel_account_owner(
        tenant_id="default",
        account_id=account.id,
        actor=_actor(bootstrap),
        owner_user_id=new_owner.id,
        expected_config_version=1,
    )
    disable = set_system_user_status(
        user_id=target_admin.id,
        user_status="disabled",
        bootstrap_password=_BOOTSTRAP_PASSWORD,
        actor=SystemUserActor(
            actor=bootstrap.actor,
            session_id=bootstrap.session_id,
            user_id=None,
        ),
    )
    results = await asyncio.wait_for(
        asyncio.gather(transfer, disable, return_exceptions=True),
        timeout=10,
    )
    assert results[1] is None
    assert results[0] is None or isinstance(
        results[0], channel_management.ChannelConflictError
    )

    async with get_session_factory()() as fresh:
        account_after = await fresh.get(models.PlatformAccount, account.id)
        work_after = await fresh.get(models.HumanWorkItem, work.id)
        target_after = await fresh.get(models.AdminUser, target_admin.id)
    assert account_after.owner_user_id in {old_owner.id, new_owner.id}
    assert target_after.status == "disabled"
    assert work_after.status == "WAITING"

async def test_retry_cross_workspace_admins_use_one_sorted_authority_order(
    session, migrated_db, monkeypatch
):
    admin_a, principal_a = await _create_user(
        session, "channel-retry-cross-a", role="WORKSPACE_ADMIN"
    )
    admin_b, principal_b = await _create_user(
        session, "channel-retry-cross-b", role="WORKSPACE_ADMIN"
    )

    def make_job(owner, principal, marker):
        return models.ProvisioningJob(
            tenant_id="default",
            brand_id="default",
            platform="telegram",
            operation="CONNECT_ACCOUNT",
            actor=principal.actor,
            owner_user_id=owner.id,
            initiator_user_id=owner.id,
            initiator_session_id=principal.session_id,
            authority_kind="STAFF_SESSION",
            authority_version=1,
            idempotency_key=f"channel-retry-{marker}-{uuid.uuid4()}",
            request={"automation_default": "BOT_DRAFT_ONLY"},
            staging_secret={},
            status="FAILED",
            current_step="FAILED",
            result={},
        )

    job_a = make_job(admin_a, principal_a, "a")
    job_b = make_job(admin_b, principal_b, "b")
    session.add_all([job_a, job_b])
    await session.commit()
    original_lock = jobs._lock_staff_and_sessions
    all_callers_ready = asyncio.Event()
    arrived = 0

    async def synchronized_lock(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            all_callers_ready.set()
        await all_callers_ready.wait()
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", synchronized_lock)

    async def ignore_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(channel_management, "dispatch_actor", ignore_dispatch)
    await asyncio.wait_for(
        asyncio.gather(
            channel_management.retry_channel_job(
                tenant_id="default", job_id=job_a.id, principal=principal_b
            ),
            channel_management.retry_channel_job(
                tenant_id="default", job_id=job_b.id, principal=principal_a
            ),
        ),
        timeout=10,
    )

    async with get_session_factory()() as fresh:
        retried_a = await fresh.get(models.ProvisioningJob, job_a.id)
        retried_b = await fresh.get(models.ProvisioningJob, job_b.id)
    assert retried_a.status == "PENDING"
    assert retried_b.status == "PENDING"


async def test_retry_rejects_revoked_caller_without_mutating_job(
    session, migrated_db, monkeypatch
):
    caller, caller_principal = await _create_user(
        session, "channel-retry-revoked-caller", role="WORKSPACE_ADMIN"
    )
    initiator, initiator_principal = await _create_user(
        session, "channel-retry-revoked-caller-initiator"
    )
    bootstrap = await _create_bootstrap()
    job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        operation="CONNECT_ACCOUNT",
        actor=initiator_principal.actor,
        owner_user_id=initiator.id,
        initiator_user_id=initiator.id,
        initiator_session_id=initiator_principal.session_id,
        authority_kind="STAFF_SESSION",
        authority_version=1,
        idempotency_key=f"channel-retry-revoked-caller-{uuid.uuid4()}",
        request={"automation_default": "BOT_DRAFT_ONLY"},
        staging_secret={},
        status="FAILED",
        current_step="FAILED",
        result={},
    )
    session.add(job)
    await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_lock = jobs._lock_staff_and_sessions

    async def pause_before_lock(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", pause_before_lock)
    retry_task = asyncio.create_task(
        channel_management.retry_channel_job(
            tenant_id="default", job_id=job.id, principal=caller_principal
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    await set_system_user_status(
        user_id=caller.id,
        user_status="disabled",
        bootstrap_password=_BOOTSTRAP_PASSWORD,
        emergency_reason="Revoke compromised sole workspace admin during retry",
        actor=SystemUserActor(
            actor=bootstrap.actor,
            session_id=bootstrap.session_id,
            user_id=None,
        ),
    )
    release.set()
    with pytest.raises(channel_management.ChannelPermissionError, match="admin_session_invalid"):
        await asyncio.wait_for(retry_task, timeout=10)

    async with get_session_factory()() as fresh:
        unchanged = await fresh.get(models.ProvisioningJob, job.id)
    assert unchanged.status == "FAILED"
    assert unchanged.current_step == "FAILED"


async def test_retry_rejects_revoked_original_without_mutating_job(
    session, migrated_db, monkeypatch
):
    caller, caller_principal = await _create_user(
        session, "channel-retry-revoked-original-caller", role="WORKSPACE_ADMIN"
    )
    initiator, initiator_principal = await _create_user(
        session, "channel-retry-revoked-original-initiator"
    )
    bootstrap = await _create_bootstrap()
    job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform="telegram",
        operation="CONNECT_ACCOUNT",
        actor=initiator_principal.actor,
        owner_user_id=initiator.id,
        initiator_user_id=initiator.id,
        initiator_session_id=initiator_principal.session_id,
        authority_kind="STAFF_SESSION",
        authority_version=1,
        idempotency_key=f"channel-retry-revoked-original-{uuid.uuid4()}",
        request={"automation_default": "BOT_DRAFT_ONLY"},
        staging_secret={},
        status="FAILED",
        current_step="FAILED",
        result={},
    )
    session.add(job)
    await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_lock = jobs._lock_staff_and_sessions

    async def pause_before_lock(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(jobs, "_lock_staff_and_sessions", pause_before_lock)
    retry_task = asyncio.create_task(
        channel_management.retry_channel_job(
            tenant_id="default", job_id=job.id, principal=caller_principal
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    await set_system_user_status(
        user_id=initiator.id,
        user_status="disabled",
        bootstrap_password=_BOOTSTRAP_PASSWORD,
        actor=SystemUserActor(
            actor=bootstrap.actor,
            session_id=bootstrap.session_id,
            user_id=None,
        ),
    )
    job_snapshot_query = select(models.ProvisioningJob.__table__).where(
        models.ProvisioningJob.id == job.id
    )
    async with get_session_factory()() as fresh:
        revoked_snapshot = (await fresh.execute(job_snapshot_query)).mappings().one()
    assert revoked_snapshot["status"] == "CANCELLED"
    assert revoked_snapshot["current_step"] == "REVOKED"
    assert revoked_snapshot["last_error_code"] == "STAFF_AUTHORITY_REVOKED"
    release.set()
    with pytest.raises(
        channel_management.ChannelPermissionError, match="initiator_session_invalid"
    ):
        await asyncio.wait_for(retry_task, timeout=10)

    async with get_session_factory()() as fresh:
        unchanged = (await fresh.execute(job_snapshot_query)).mappings().one()
    assert unchanged == revoked_snapshot


@pytest.mark.parametrize("platform", ["whatsapp", "feishu"])
async def test_named_workspace_admin_can_retry_platform_job(
    session, migrated_db, monkeypatch, platform
):
    settings = jobs.get_settings().model_copy(update={f"{platform}_enabled": True})
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    admin, principal = await _create_user(
        session, f"channel-retry-{platform}", role="WORKSPACE_ADMIN"
    )
    job = models.ProvisioningJob(
        tenant_id="default",
        brand_id="default",
        platform=platform,
        operation="CONNECT_ACCOUNT",
        actor=principal.actor,
        owner_user_id=admin.id,
        initiator_user_id=admin.id,
        initiator_session_id=principal.session_id,
        authority_kind="STAFF_SESSION",
        authority_version=1,
        idempotency_key=f"channel-retry-{platform}-{uuid.uuid4()}",
        request={"automation_default": "BOT_ACTIVE"},
        staging_secret={},
        status="FAILED",
        current_step="FAILED",
        result={},
        last_error_code="PLATFORM_UNAVAILABLE",
    )
    session.add(job)
    await session.commit()

    processed = []

    async def fake_process(job_id: str) -> str:
        processed.append(job_id)
        return "PENDING"

    monkeypatch.setattr(channel_management, "process_provisioning_job", fake_process)
    await channel_management.retry_channel_job(
        tenant_id="default",
        job_id=job.id,
        principal=principal,
    )

    async with get_session_factory()() as fresh:
        job_after = await fresh.get(models.ProvisioningJob, job.id)
    assert job_after.status == "PENDING"
    assert job_after.current_step == "QUEUED"
    assert processed == [str(job.id)]


async def test_submit_rejects_forged_admin_actor_before_job_creation(
    session, migrated_db, monkeypatch
):
    settings = channel_management.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(channel_management, "get_settings", lambda: settings)
    _user, principal = await _create_user(session, "channel-submit-user")
    forged_actor = channel_management.ChannelActor(
        actor=principal.actor,
        role="ADMIN",
        user_id=principal.user_id,
        session_id=principal.session_id,
    )
    command = channel_management.build_provisioning_command(
        route_tenant_id="default",
        platform="feishu",
        actor=forged_actor,
        values={
            "app_id": "cli_12345678",
            "app_secret": "secret",
            "verification_token": "verification",
            "encrypt_key": "encrypt",
        },
    )
    submitted = False

    async def fake_submit(**_kwargs):
        nonlocal submitted
        submitted = True
        return uuid.uuid4()

    monkeypatch.setattr(channel_management, "submit_provisioning_job", fake_submit)
    with pytest.raises(
        channel_management.ChannelPermissionError,
        match="provisioning_session_invalid",
    ):
        await channel_management.submit_channel_provisioning(command)
    assert submitted is False
