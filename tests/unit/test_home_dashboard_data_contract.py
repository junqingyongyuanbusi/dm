"""Fast dashboard capability, input, and immutable-result contracts."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.home_dashboard_data import (
    HomeAttentionItem,
    HomeChannelStat,
    HomeDashboardData,
    HomeTrendDay,
    load_home_dashboard,
)


def _principal(role="MANAGER", tenants=frozenset({"default"})):
    return Principal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="dashboard",
        actor="user:dashboard",
        allowed_tenants=tenants,
        tenant_id="default",
        role=role,
    )


@pytest.mark.parametrize("role", ["AGENT", "USER", "OPERATOR", "VIEWER", "UNKNOWN"])
async def test_home_capability_denied_before_database_access(role):
    session = AsyncMock()
    with pytest.raises(HTTPException) as raised:
        await load_home_dashboard(session, _principal(role), "default", datetime.now(UTC))
    assert raised.value.status_code == 403
    session.execute.assert_not_awaited()


async def test_wrong_tenant_denied_before_database_access():
    session = AsyncMock()
    with pytest.raises(HTTPException) as raised:
        await load_home_dashboard(session, _principal(), "other", datetime.now(UTC))
    assert raised.value.status_code == 403
    session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "now",
    [datetime(2026, 9, 8), datetime(2026, 9, 8, tzinfo=timezone(timedelta(hours=8)))],
)
async def test_now_must_be_aware_utc(now):
    session = AsyncMock()
    with pytest.raises(ValueError, match="UTC"):
        await load_home_dashboard(session, _principal(), "default", now)
    session.execute.assert_not_awaited()


def test_dashboard_contract_is_immutable_and_tuple_based():
    day = HomeTrendDay(day=date(2026, 9, 8), received=4, ai=2)
    channel = HomeChannelStat(platform="telegram", accounts=1, received=4)
    attention = HomeAttentionItem(title="Contact", description="Waiting", href="/", kind="human")
    result = HomeDashboardData(
        pending_count=1,
        ai_count=2,
        account_count=1,
        enabled_account_count=1,
        resolved_count=3,
        trend=(day,),
        channels=(channel,),
        attention=(attention,),
        published_knowledge_count=0,
        draft_knowledge_count=0,
        pending_draft_count=0,
    )
    for item, attribute in (
        (result, "pending_count"),
        (day, "received"),
        (channel, "received"),
        (attention, "title"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(item, attribute, "changed")
