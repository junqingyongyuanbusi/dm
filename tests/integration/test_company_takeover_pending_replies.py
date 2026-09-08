import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.integration.company_permission_support import create_staff, seed_conversation

from social_reply.application.account_management import human_workflow
from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["transfer", "start", "override"])
@pytest.mark.parametrize("old_status", ["PENDING", "FAILED", "SENDING", "NEEDS_REVIEW"])
async def test_assignment_changes_dispose_only_unstarted_prior_replies(
    session, migrated_db, monkeypatch, operation, old_status
):
    staff = await create_staff(session)
    manager = await create_staff(session, role="WORKSPACE_ADMIN")
    conversation = await seed_conversation(session, shared_with_support=True)
    monkeypatch.setattr(human_workflow, "dispatch_actor", AsyncMock())
    work_id = await human_workflow.start_human_reception(
        conversation_id=conversation.conversation_id,
        principal=staff.principal,
    )
    old_reply_id = await _reply(conversation, staff)
    work = await session.get(models.HumanWorkItem, work_id)
    old_reply = await session.get(models.OutboxMessage, old_reply_id)
    assert work.assigned_user_id == staff.user_id
    assert work.assigned_session_id is None
    assert old_reply.initiator_session_id == staff.principal.session_id
    assert old_reply.human_work_item_version == work.version
    old_version = work.version
    # Inject only delivery state after a real intent; never invent assignment identity.
    old_reply.status = old_status
    if old_status in {"SENDING", "NEEDS_REVIEW"}:
        old_reply.attempt_count = 1
    await session.commit()

    if operation == "transfer":
        await human_workflow.transfer_human_work_item(
            work_item_id=work_id,
            allowed_tenants=staff.principal.allowed_tenants,
            actor=staff.principal.actor,
            user_id=staff.user_id,
            target_user_id=manager.user_id,
            expected_version=old_version,
            principal=staff.principal,
        )
    elif operation == "start":
        await human_workflow.start_human_reception(
            conversation_id=conversation.conversation_id,
            principal=manager.principal,
        )
    else:
        new_reply_id = await _reply(conversation, manager, override=True)
    if operation != "override":
        new_reply_id = await _reply(conversation, manager)
    await session.refresh(work)
    await session.refresh(old_reply)
    assert work.assigned_user_id == manager.user_id
    assert work.version == old_version + 1
    assert old_reply.status == ("CANCELLED" if old_status in {"PENDING", "FAILED"} else old_status)
    audits = list(
        await session.scalars(
            select(models.AuditLog).where(
                models.AuditLog.action == "CANCEL_SUPERSEDED_HUMAN_REPLY",
                models.AuditLog.subject_id == str(old_reply_id),
            )
        )
    )
    assert len(audits) == (1 if old_status in {"PENDING", "FAILED"} else 0)
    if audits:
        assert audits[0].detail["previous_user_id"] == str(staff.user_id)
        assert audits[0].detail["previous_work_version"] == old_version
    await session.commit()

    sends = []

    class Sender:
        async def send_text(self, *, target, text):
            sends.append(text)
            return f"message-{uuid.uuid4()}"

    monkeypatch.setattr(outbox_module, "get_platform_sender", AsyncMock(return_value=Sender()))
    assert await outbox_module.deliver_outbox(str(new_reply_id)) == "SENT"
    assert len(sends) == 1
    arguments = dict(
        work_item_id=work_id,
        allowed_tenants=manager.principal.allowed_tenants,
        actor=manager.principal.actor,
        user_id=manager.user_id,
        principal=manager.principal,
        expected_version=old_version + 1,
        allow_override=False,
    )
    if old_status in {"SENDING", "NEEDS_REVIEW"}:
        with pytest.raises(
            human_workflow.HumanWorkflowConflict, match="human_reply_delivery_pending"
        ):
            await human_workflow.resolve_human_work_item(**arguments)
        await session.refresh(work)
        assert work.status == "CLAIMED"
        assert work.version == old_version + 1
    else:
        await human_workflow.resolve_human_work_item(**arguments)
        await session.refresh(work)
        assert work.status == "RESOLVED"


async def _reply(conversation, identity, *, override=False):
    return await human_workflow.send_human_reply(
        conversation_id=conversation.conversation_id,
        reply_to_message_id=conversation.message_id,
        text="Replacement response" if override else "Customer response",
        idempotency_key=str(uuid.uuid4()),
        allowed_tenants=identity.principal.allowed_tenants,
        actor=identity.principal.actor,
        user_id=identity.user_id,
        principal=identity.principal,
        allow_override=override,
    )
