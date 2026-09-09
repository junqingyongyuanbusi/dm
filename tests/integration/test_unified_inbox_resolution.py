"""Run serially: the shared session fixture rebuilds the independent test schema."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.unified_inbox import build_conversation_query
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration
BASE_TIME = datetime(2026, 9, 8, 12, tzinfo=UTC)


async def _seed_case(
    session,
    *,
    state,
    resolutions,
    inbound_times,
    active_status=None,
    occurred_offset=0,
    tenant_id="default",
    work_status="RESOLVED",
):
    account_id, contact_id, conversation_id = (uuid.uuid4() for _ in range(3))
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="inbox",
            platform="telegram",
            name="Resolution account",
            automation_default="BOT_ACTIVE",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id=tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=str(contact_id),
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id="inbox",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=str(conversation_id),
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id,
            state=state,
        )
    )
    for minute in resolutions:
        await session.execute(
            insert(models.HumanWorkItem).values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                status=work_status,
                reason_code="TEST",
                resolved_at=BASE_TIME + timedelta(minutes=minute) if minute is not None else None,
            )
        )
    if active_status:
        await session.execute(
            insert(models.HumanWorkItem).values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                status=active_status,
                reason_code="TEST",
                assigned_actor="user:reader" if active_status == "CLAIMED" else None,
                claimed_at=BASE_TIME if active_status == "CLAIMED" else None,
            )
        )
    for minute in inbound_times:
        await session.execute(
            insert(models.Message).values(
                conversation_id=conversation_id,
                direction="inbound",
                sender_type="contact",
                text="Inbound",
                created_at=BASE_TIME + timedelta(minutes=minute),
                occurred_at=BASE_TIME + timedelta(minutes=minute + occurred_offset),
            )
        )
    # A later outbound must not reopen completed human handling.
    await session.execute(
        insert(models.Message).values(
            conversation_id=conversation_id,
            direction="outbound",
            sender_type="bot",
            text="Outbound",
            created_at=BASE_TIME + timedelta(days=1),
        )
    )
    return conversation_id


@pytest.mark.parametrize(
    "state,resolutions,inbound_times,active_status,occurred_offset,expected",
    [
        ("BOT_ACTIVE", [2], [1], None, 0, True),
        ("BOT_DRAFT_ONLY", [2], [2], None, 0, True),
        ("BOT_ACTIVE", [2], [1, 3], None, 0, False),
        ("BOT_ACTIVE", [2, 4], [3, 1], None, 0, True),
        ("BOT_ACTIVE", [2], [1], "WAITING", 0, False),
        ("BOT_ACTIVE", [2], [1], "CLAIMED", 0, False),
        ("BOT_ACTIVE", [], [1], None, 0, False),
        ("BOT_ACTIVE", [None], [1], None, 0, False),
        ("BOT_ACTIVE", [2], [], None, 0, True),
        ("BOT_ACTIVE", [2], [3], None, -100, False),
        ("BOT_ACTIVE", [2], [1], None, 100, True),
        ("CLOSED", [], [3], None, 0, True),
    ],
)
async def test_resolution_boundary_query(
    session,
    state,
    resolutions,
    inbound_times,
    active_status,
    occurred_offset,
    expected,
):
    conversation_id = await _seed_case(
        session,
        state=state,
        resolutions=resolutions,
        inbound_times=inbound_times,
        active_status=active_status,
        occurred_offset=occurred_offset,
    )
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="reader",
        actor="user:reader",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role="WORKSPACE_ADMIN",
    )
    statement = build_conversation_query(principal, "default", queue="resolved")
    rows = (await session.execute(statement)).all()
    assert [row[0].id for row in rows] == ([conversation_id] if expected else [])
    if rows:
        assert rows[0][4] == state
    count = await session.scalar(
        select(func.count()).select_from(
            statement.order_by(None).limit(None).offset(None).subquery(),
        )
    )
    assert count == int(expected)


async def test_resolution_excludes_cancelled_work_other_tenant_and_ungranted_account(session):
    await _seed_case(
        session, state="BOT_ACTIVE", resolutions=[2], inbound_times=[1], work_status="CANCELLED"
    )
    await _seed_case(
        session, state="BOT_ACTIVE", resolutions=[2], inbound_times=[1], tenant_id="other"
    )
    visible_id = await _seed_case(session, state="BOT_ACTIVE", resolutions=[2], inbound_times=[1])
    admin = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="reader",
        actor="user:reader",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role="WORKSPACE_ADMIN",
    )
    rows = (
        await session.execute(build_conversation_query(admin, "default", queue="resolved"))
    ).all()
    assert [row[0].id for row in rows] == [visible_id]
    viewer = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="viewer",
        actor="user:viewer",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role="VIEWER",
    )
    assert not (
        await session.execute(
            build_conversation_query(
                viewer,
                "default",
                queue="resolved",
                selected_id=visible_id,
            )
        )
    ).all()
