import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from tests.integration.company_permission_support import (
    create_staff,
    login_client,
    seed_conversation,
)

from apps.api.main import create_app
from social_reply.application.account_management import human_workflow
from social_reply.application.account_management.channel_management import (
    ChannelActor,
    set_channel_account_status,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["legacy_reply", "override", "transfer"])
async def test_disabled_account_rejects_writes_before_assignment_or_reply_cancellation(
    session, migrated_db, monkeypatch, operation
):
    staff = await create_staff(session)
    manager = await create_staff(session, role="WORKSPACE_ADMIN")
    conversation = await seed_conversation(session, shared_with_support=True)
    monkeypatch.setattr(human_workflow, "dispatch_actor", AsyncMock())
    work_id = await human_workflow.start_human_reception(
        conversation_id=conversation.conversation_id,
        principal=staff.principal,
    )
    await human_workflow.send_human_reply(
        conversation_id=conversation.conversation_id,
        reply_to_message_id=conversation.message_id,
        text="Queued employee reply",
        idempotency_key=str(uuid.uuid4()),
        allowed_tenants=staff.principal.allowed_tenants,
        actor=staff.principal.actor,
        user_id=staff.user_id,
        principal=staff.principal,
    )
    await set_channel_account_status(
        tenant_id="default",
        account_id=conversation.account_id,
        actor=ChannelActor(
            actor=manager.principal.actor,
            role="ADMIN",
            user_id=manager.user_id,
            session_id=manager.principal.session_id,
        ),
        enabled=False,
        expected_status="active",
        expected_config_version=1,
    )
    before = await _snapshot(work_id, conversation.conversation_id)
    arguments = dict(
        allowed_tenants=manager.principal.allowed_tenants,
        actor=manager.principal.actor,
        user_id=manager.user_id,
        principal=manager.principal,
    )
    if operation == "override":
        with pytest.raises(
            human_workflow.HumanWorkflowConflict, match="human_account_access_denied"
        ):
            await human_workflow.send_human_reply(
                conversation_id=conversation.conversation_id,
                reply_to_message_id=conversation.message_id,
                text="Must not replace queued reply",
                idempotency_key=str(uuid.uuid4()),
                allow_override=True,
                **arguments,
            )
    elif operation == "transfer":
        with pytest.raises(
            human_workflow.HumanWorkflowConflict, match="human_account_access_denied"
        ):
            await human_workflow.transfer_human_work_item(
                work_item_id=work_id,
                target_user_id=manager.user_id,
                expected_version=before[0][0],
                **arguments,
            )
    else:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            csrf = await login_client(client, username=manager.username, password=manager.password)
            response = await client.post(
                f"/admin/conversations/{conversation.conversation_id}/reply",
                data={
                    "csrf_token": csrf,
                    "text": "Must not create a new intent",
                    "reply_to_message_id": str(conversation.message_id),
                    "idempotency_key": str(uuid.uuid4()),
                    "work_item_id": str(work_id),
                    "version": str(before[0][0]),
                },
            )
            assert response.status_code == 409
            assert response.json() == {"detail": "human_account_access_denied"}
    assert await _snapshot(work_id, conversation.conversation_id) == before


async def _snapshot(work_id, conversation_id):
    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, work_id)
        state = await fresh.get(models.AutomationState, conversation_id)
        replies = list(
            await fresh.execute(
                select(
                    models.OutboxMessage.id,
                    models.OutboxMessage.status,
                    models.OutboxMessage.human_work_item_version,
                    models.OutboxMessage.initiator_user_id,
                )
                .where(models.OutboxMessage.conversation_id == conversation_id)
                .order_by(models.OutboxMessage.id)
            )
        )
        return (
            (work.version, work.status, work.assigned_user_id, work.assigned_session_id),
            (state.state, state.state_version, state.human_agent_id),
            tuple(tuple(row) for row in replies),
        )
