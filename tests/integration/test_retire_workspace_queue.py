import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from apps.cli import retire_workspace_queue
from apps.cli.retire_workspace_queue import (
    EXPECTED_SCHEMA,
    RETIRED,
    UNKNOWN,
    CutoverRequest,
    run_cutover,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

BEFORE = datetime(2020, 1, 2, tzinfo=UTC)
OLD = BEFORE - timedelta(days=1)


@pytest.fixture
async def cutover_session(session):
    # Only the isolated _test DB from tests/conftest.py; runtime CLI never stamps schema.
    await session.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)"
    ))
    await session.execute(text("DELETE FROM alembic_version"))
    await session.execute(text(
        "INSERT INTO alembic_version (version_num) VALUES (:version)"
    ), {"version": EXPECTED_SCHEMA})
    await session.commit()
    return session


def request_for(*admin_ids, apply=True):
    return CutoverRequest(
        tenant="default", cutover_id=uuid.uuid4(), before=BEFORE,
        restore_admin_ids=admin_ids, apply=apply, confirm_processes_stopped=apply,
    )


async def seed_conversation(session, tenant="default"):
    account = models.PlatformAccount(
        tenant_id=tenant, brand_id="brand", platform="telegram", name="preserve-account",
        config={"preserve": True}, credential_bundle={"encrypted": "unchanged"},
    )
    session.add(account)
    await session.flush()
    contact = models.Contact(
        tenant_id=tenant, platform="telegram", platform_account_id=account.id,
        external_user_id=str(uuid.uuid4()), display_name="preserve-contact",
    )
    session.add(contact)
    await session.flush()
    conversation = models.Conversation(
        tenant_id=tenant, brand_id="brand", platform="telegram", platform_account_id=account.id,
        contact_id=contact.id, conversation_key=str(uuid.uuid4()), created_at=OLD,
    )
    session.add(conversation)
    await session.flush()
    return account, conversation


def make_outbox(account, conversation, *, status="PENDING", created_at=OLD):
    return models.OutboxMessage(
        tenant_id=account.tenant_id, conversation_id=conversation.id,
        platform_account_id=account.id, destination_type="telegram_reply", destination_id="chat",
        message_type="text", payload={"text": "preserve-history"},
        idempotency_key=str(uuid.uuid4()),
        status=status, created_at=created_at, last_error_code="XCHAT_DISABLED",
        locked_at=OLD if status == "SENDING" else None,
    )


async def test_preview_is_database_read_only_and_leaves_business_and_audit_unchanged(
    cutover_session,
):
    session = cutover_session
    account, conversation = await seed_conversation(session)
    outbox = make_outbox(account, conversation)
    session.add(outbox)
    await session.commit()
    report = await run_cutover(session, request_for(apply=False))
    assert report["status"] == "preview"
    assert report["counts"]["outbox_cancelled"] == 1
    await session.refresh(outbox)
    assert outbox.status == "PENDING"
    assert outbox.payload == {"text": "preserve-history"}
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_apply_preserves_new_other_tenant_and_unknown_deliveries(cutover_session):
    session = cutover_session
    account, conversation = await seed_conversation(session)
    other_account, other_conversation = await seed_conversation(session, tenant="tenant-b")
    old = make_outbox(account, conversation)
    new = make_outbox(account, conversation, created_at=BEFORE)
    foreign = make_outbox(other_account, other_conversation)
    sending = make_outbox(account, conversation, status="SENDING")
    review = make_outbox(account, conversation, status="NEEDS_REVIEW")
    session.add_all([old, new, foreign, sending, review])
    await session.commit()
    request = request_for()
    report = await run_cutover(session, request)
    repeated = await run_cutover(session, request)
    assert repeated["status"] == "already_applied"
    assert repeated["counts"] == report["counts"]
    assert report["counts"]["outbox_unknown"] == 2
    for outbox in [old, new, foreign, sending, review]:
        await session.refresh(outbox)
    assert old.status == "CANCELLED"
    assert new.status == foreign.status == "PENDING"
    assert sending.status == review.status == "NEEDS_REVIEW"
    assert sending.last_error_code == review.last_error_code == UNKNOWN
    assert sending.next_attempt_at is None
    assert sending.platform_message_id is None
    await session.refresh(account)
    assert account.credential_bundle == {"encrypted": "unchanged"}
    assert account.config == {"preserve": True}
    assert await session.scalar(select(func.count()).select_from(models.Contact)) == 2
    await session.commit()
    with pytest.raises(ValueError, match="cutover_id_parameter_conflict"):
        await run_cutover(session, replace(request, before=OLD))


@pytest.mark.parametrize(
    "tenant,role,status",
    [("tenant-b", "USER", "active"), ("default", "MANAGER", "active"),
     ("default", "USER", "disabled")],
)
async def test_invalid_admin_rejects_entire_cutover(cutover_session, tenant, role, status):
    session = cutover_session
    account, conversation = await seed_conversation(session)
    outbox = make_outbox(account, conversation)
    admin = models.AdminUser(
        username=str(uuid.uuid4()), password_hash="never-change", tenant_id=tenant,
        role=role, status=status,
    )
    session.add_all([outbox, admin])
    await session.commit()
    with pytest.raises(ValueError, match="restore_admin_scope_or_role_invalid"):
        await run_cutover(session, request_for(admin.id))
    await session.refresh(outbox)
    await session.refresh(admin)
    assert outbox.status == "PENDING"
    assert admin.role == role
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_admin_restore_is_explicit_idempotent_and_does_not_change_password(cutover_session):
    session = cutover_session
    admins = [models.AdminUser(
        username=str(uuid.uuid4()), password_hash="never-change", tenant_id="default",
        role=role, status="active",
    ) for role in ("USER", "AGENT", "WORKSPACE_ADMIN", "USER")]
    session.add_all(admins)
    await session.commit()
    request = request_for(*(admin.id for admin in admins[:3]))
    report = await run_cutover(session, request)
    assert report["counts"]["admins_restored"] == 2
    repeated = await run_cutover(session, replace(
        request, restore_admin_ids=tuple(reversed(request.restore_admin_ids)),
    ))
    assert repeated["status"] == "already_applied"
    for admin in admins:
        await session.refresh(admin)
        assert admin.password_hash == "never-change"
    assert [admin.role for admin in admins] == [
        "WORKSPACE_ADMIN", "WORKSPACE_ADMIN", "WORKSPACE_ADMIN", "USER",
    ]
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 3


async def test_wrong_schema_and_missing_admin_fail_closed(cutover_session):
    session = cutover_session
    with pytest.raises(ValueError, match="restore_admin_scope_or_role_invalid"):
        await run_cutover(session, request_for(uuid.uuid4()))
    await session.execute(text("UPDATE alembic_version SET version_num = 'previous'"))
    await session.commit()
    with pytest.raises(ValueError, match="exact_schema_head_required"):
        await run_cutover(session, request_for())
    await session.execute(text("UPDATE alembic_version SET version_num = :version"), {
        "version": EXPECTED_SCHEMA,
    })
    await session.commit()


async def test_retires_work_and_invalidates_claims_but_preserves_raw_evidence(cutover_session):
    session = cutover_session
    account, conversation = await seed_conversation(session)
    message = models.Message(
        conversation_id=conversation.id, direction="inbound",
        sender_type="contact", text="preserve-message", decision_generation=0,
    )
    raw = models.RawEvent(
        tenant_id="default", platform_account_id=account.id, source="telegram",
        payload={"evidence": True}, context={"keep": True}, processing_status="INITIAL_DISPATCHING",
        received_at=OLD, processing_claim_token=uuid.uuid4(),
        processing_claim_expires_at=BEFORE,
    )
    work = models.HumanWorkItem(
        tenant_id="default", conversation_id=conversation.id, status="CLAIMED", reason_code="old",
        assigned_actor="agent:old", claimed_at=OLD, created_at=OLD, version=7,
    )
    state = models.AutomationState(
        conversation_id=conversation.id, state="HUMAN_ACTIVE", human_agent_id="old",
        last_human_message_at=OLD, resume_policy="AUTO", state_version=8, updated_at=OLD,
    )
    session.add_all([message, raw, work, state])
    await session.flush()
    decision = models.DecisionJob(
        raw_event_id=raw.id, conversation_id=conversation.id, message_id=message.id,
        account_id=account.id, snapshot={"keep": True}, status="PROCESSING", created_at=OLD,
        claim_token=uuid.uuid4(), locked_at=OLD,
    )
    draft = models.ReplyDecision(
        tenant_id="default", conversation_id=conversation.id, message_id=message.id,
        action="draft", source="rule", reply_text="preserve-draft", created_at=OLD,
        review_action="PENDING", decision_generation=0,
    )
    notification = models.HandoffNotificationIntent(
        tenant_id="default", human_work_item_id=work.id, conversation_id=conversation.id,
        status="SENDING", provider_message_id="already-created", created_at=OLD,
        claim_token=uuid.uuid4(), claim_expires_at=BEFORE, sending_revision=1,
    )
    provisioning = models.ProvisioningJob(
        tenant_id="default", brand_id="brand", platform="telegram", actor="test",
        idempotency_key=str(uuid.uuid4()), request={"preserve": True}, status="PROCESSING",
        created_at=OLD, attempt_count=3, locked_at=OLD, locked_by="old-worker",
        staging_secret={"encrypted": "preserve"},
    )
    session.add_all([decision, draft, notification, provisioning])
    await session.commit()
    nonce = notification.action_nonce
    report = await run_cutover(session, request_for())
    assert report["counts"]["raw_retired"] == 1
    for row in [raw, work, state, decision, draft, notification, provisioning, message]:
        await session.refresh(row)
    assert raw.processing_status == RETIRED
    assert raw.processed_at is None
    assert raw.processing_claim_token is None
    assert raw.payload == {"evidence": True}
    assert raw.context == {"keep": True}
    assert work.status == "CANCELLED" and work.version == 8
    assert state.state == "BOT_DRAFT_ONLY" and state.state_version == 9
    assert state.human_agent_id is state.last_human_message_at is None
    assert state.resume_policy == "MANUAL"
    assert decision.status == "SUPERSEDED" and decision.claim_token is None
    assert draft.review_action == "REJECTED" and draft.reply_text == "preserve-draft"
    assert notification.status == "NEEDS_REVIEW" and notification.last_error_code == UNKNOWN
    assert notification.claim_token is notification.sending_revision is None
    assert notification.action_nonce != nonce
    assert notification.provider_message_id == "already-created"
    assert provisioning.status == "CANCELLED" and provisioning.attempt_count == 4
    assert provisioning.locked_at is provisioning.locked_by is None
    assert provisioning.staging_secret == {"encrypted": "preserve"}
    assert message.text == "preserve-message"


async def test_unattributable_raw_is_reported_and_blocks_apply(cutover_session):
    session = cutover_session
    raw = models.RawEvent(source="telegram", payload={}, received_at=OLD)
    session.add(raw)
    await session.commit()
    request = request_for(apply=False)
    report = await run_cutover(session, request)
    assert report["counts"]["unscoped_raw_blockers"] == 1
    with pytest.raises(ValueError, match="unscoped_raw_requires_manual_review"):
        await run_cutover(session, replace(
            request, apply=True, confirm_processes_stopped=True,
        ))
    await session.refresh(raw)
    assert raw.processing_status == "PENDING"
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_retired_raw_cannot_be_claimed_by_current_recovery_paths(cutover_session):
    from social_reply.application.event_ingestion.raw_recovery import (
        claim_initial_raw_event,
        dispatch_initial_raw_event,
    )
    from social_reply.application.event_ingestion.xchat_webhook import _claim

    session = cutover_session
    account, _conversation = await seed_conversation(session)
    token = uuid.uuid4()
    raw = models.RawEvent(
        tenant_id="default", platform_account_id=account.id, source="x", payload={},
        received_at=OLD, processing_status="XCHAT_DECRYPTION_PENDING",
        processing_claim_token=token,
    )
    session.add(raw)
    await session.commit()
    await run_cutover(session, request_for())
    assert await claim_initial_raw_event(raw.id, token, expected_kind="direct") is None
    assert await dispatch_initial_raw_event(raw.id) is False
    assert await _claim(raw.id, account.id, "default") is None
    await session.refresh(raw)
    assert raw.processing_status == RETIRED
    assert raw.processing_attempt_count == 1


async def test_late_failure_rolls_back_business_changes_and_audit(cutover_session, monkeypatch):
    session = cutover_session
    account, conversation = await seed_conversation(session)
    outbox = make_outbox(account, conversation)
    session.add(outbox)
    await session.commit()
    perform = retire_workspace_queue._perform_cutover

    async def fail_after_updates(transaction_session, request):
        await perform(transaction_session, request)
        await transaction_session.flush()
        raise RuntimeError("injected pre-commit failure")

    monkeypatch.setattr(retire_workspace_queue, "_perform_cutover", fail_after_updates)
    with pytest.raises(RuntimeError, match="injected pre-commit failure"):
        await run_cutover(session, request_for())
    await session.refresh(outbox)
    assert outbox.status == "PENDING"
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_overlapping_cutover_fails_without_writing(cutover_session):
    session = cutover_session
    async with get_session_factory()() as blocker, blocker.begin():
        await blocker.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('workspace_queue_cutover'))"
        ))
        with pytest.raises(ValueError, match="cutover_already_running"):
            await run_cutover(session, request_for())
    assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
