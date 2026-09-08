import uuid

import httpx
import pytest
from sqlalchemy import select
from tests.integration.company_permission_support import (
    create_staff,
    login_client,
    seed_conversation,
)

from apps.api.main import create_app
from social_reply.application.account_management.channel_management import (
    ChannelActor,
    set_channel_account_status,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("role", ["USER", "WORKSPACE_ADMIN"])
@pytest.mark.parametrize("route", ["saas", "legacy"])
async def test_existing_waiting_work_cannot_be_claimed_after_account_disable(
    session, migrated_db, role, route
):
    staff = await create_staff(session, role=role)
    manager = await create_staff(session, role="WORKSPACE_ADMIN")
    conversation = await seed_conversation(session, work_status="WAITING", shared_with_support=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        csrf = await login_client(client, username=staff.username, password=staff.password)
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
        before = await _snapshot(conversation)
        prefix = "/app/t/default" if route == "saas" else "/admin"
        version_field = "expected_version" if route == "saas" else "version"
        response = await client.post(
            f"{prefix}/work-items/{conversation.work_id}/claim",
            data={"csrf_token": csrf, version_field: str(before[0][1])},
        )
        if route == "legacy" and role == "USER":
            assert response.status_code == 403
            assert response.json() == {"detail": "admin_required"}
        else:
            assert response.status_code == 409
            assert response.json() == {"detail": "human_account_access_denied"}
        detail = await client.get(f"/app/t/default/conversations/{conversation.conversation_id}")
        assert detail.status_code == 200
    assert await _snapshot(conversation) == before


async def _snapshot(conversation):
    async with get_session_factory()() as fresh:
        work = await fresh.get(models.HumanWorkItem, conversation.work_id)
        state = await fresh.get(models.AutomationState, conversation.conversation_id)
        notifications = list(
            await fresh.scalars(
                select(models.HandoffNotificationIntent.id).where(
                    models.HandoffNotificationIntent.conversation_id
                    == conversation.conversation_id,
                )
            )
        )
        assert work is not None and state is not None
        return (
            (
                work.status,
                work.version,
                work.assigned_user_id,
                work.assigned_session_id,
                work.claimed_at,
            ),
            (state.state, state.state_version, state.human_agent_id),
            tuple(sorted(notifications, key=uuid.UUID.__str__)),
        )
