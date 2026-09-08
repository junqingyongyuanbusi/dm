from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.home_overview import (
    HomeBusinessActivity,
    HomeChannelAlert,
    HomeOverview,
    load_home_overview,
)
from social_reply.shared.config import Settings

_RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _principal(*, superadmin: bool = False) -> Principal:
    return Principal(
        session_id=UUID(int=100),
        user_id=None if superadmin else UUID(int=101),
        username="operator",
        actor="user:operator",
        allowed_tenants=frozenset({"tenant-a"}),
        tenant_id="tenant-a",
        role="SUPERADMIN" if superadmin else "USER",
    )


def _settings(**gates: bool) -> Settings:
    return Settings(
        _env_file=None,
        testing=True,
        **{
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "feishu_enabled": True,
            **gates,
        },
    )


def _session(*batches: tuple[dict, ...]) -> AsyncMock:
    results = []
    for batch in batches:
        result = MagicMock()
        result.mappings.return_value.all.return_value = batch
        results.append(result)
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = results
    return session


def _sql(statement) -> str:
    return " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        ).split()
    )


@pytest.mark.parametrize("superadmin", [False, True])
async def test_overview_scopes_and_bounds_only_explicit_business_projections(superadmin):
    principal = _principal(superadmin=superadmin)
    session = _session(*([()] * (4 if superadmin else 2)))

    overview = await load_home_overview(session, principal, "tenant-a", settings=_settings())

    assert overview == HomeOverview(alerts=(), activities=())
    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == (4 if superadmin else 2)
    for statement in statements:
        query = _sql(statement)
        assert "platform_accounts.tenant_id = 'tenant-a'" in query
        assert ("platform_accounts.owner_user_id =" in query) == (not superadmin)
        if not superadmin:
            assert str(principal.user_id) in query
        assert "audit_logs" not in query
        for forbidden in ("payload", "credential", "error_message", "body", "actor_id"):
            assert forbidden not in query

    alert_statement, *activity_statements = statements
    assert tuple(alert_statement.selected_columns.keys()) == (
        "account_id", "account_name", "platform", "health_status", "checked_at"
    )
    alert_query = _sql(alert_statement)
    for field in (
        "meta_health_status", "meta_health_checked_at",
        "feishu_health_status", "feishu_health_checked_at",
    ):
        assert field in alert_query
    assert "platform_accounts.status = 'active'" in alert_query
    assert alert_query.endswith(
        "ORDER BY platform_accounts.platform, platform_accounts.name, platform_accounts.id LIMIT 3"
    )
    assert "READY" not in alert_query and "DISABLED" not in alert_query

    for statement in activity_statements:
        query = _sql(statement)
        assert tuple(statement.selected_columns.keys()) == (
            "event_id", "conversation_id", "kind", "account_name", "occurred_at"
        )
        assert "conversations.tenant_id = 'tenant-a'" in query
        assert "conversations.platform_account_id = platform_accounts.id" in query
        assert query.endswith("LIMIT 5")
        assert query.index("WHERE") < query.index("ORDER BY") < query.index("LIMIT")
    handoff_query = _sql(activity_statements[0])
    assert "human_work_items.tenant_id = 'tenant-a'" in handoff_query
    assert "human_work_items.status" not in handoff_query
    assert "human_work_items.created_at AS occurred_at" in handoff_query
    assert "ORDER BY human_work_items.created_at DESC, human_work_items.id DESC" in handoff_query

    if superadmin:
        sent_query, failed_query = map(_sql, activity_statements[1:])
        for query in (sent_query, failed_query):
            assert "outbox_messages.tenant_id = 'tenant-a'" in query
            assert "outbox_messages.platform_account_id = platform_accounts.id" in query
            assert "outbox_messages.created_at" not in query
        assert "outbox_messages.status = 'SENT'" in sent_query
        assert "outbox_messages.sent_at IS NOT NULL" in sent_query
        assert "outbox_messages.sent_at AS occurred_at" in sent_query
        assert "outbox_messages.actor_kind = 'BOT'" in sent_query
        for origin in ("DECISION", "DRAFT_APPROVAL", "MANUAL_REPLY"):
            assert origin in sent_query
        assert "SYSTEM_NOTICE" not in sent_query
        assert "ORDER BY outbox_messages.sent_at DESC, outbox_messages.id DESC" in sent_query
        assert "delivery_attempts.created_at AS occurred_at" in failed_query
        assert "delivery_attempts.outcome IN ('FAILED', 'NEEDS_REVIEW')" in failed_query
        assert "outbox_messages.status IN ('FAILED', 'NEEDS_REVIEW')" in failed_query
        assert (
            "ORDER BY delivery_attempts.created_at DESC, delivery_attempts.id DESC" in failed_query
        )


async def test_overview_requires_tenant_before_any_query():
    session = _session()
    with pytest.raises(HTTPException) as denied:
        await load_home_overview(session, _principal(), "tenant-b", settings=_settings())
    assert denied.value.status_code == 403
    session.execute.assert_not_awaited()


async def test_database_identity_cannot_enable_superadmin_delivery_sources():
    principal = replace(_principal(), role="SUPERADMIN")
    session = _session((), ())

    await load_home_overview(session, principal, "tenant-a", settings=_settings())

    assert session.execute.await_count == 2
    assert all(
        "outbox_messages" not in _sql(call.args[0])
        and "platform_accounts.owner_user_id =" in _sql(call.args[0])
        for call in session.execute.await_args_list
    )


@pytest.mark.parametrize("enabled_platform", ["facebook", "instagram", "feishu", None])
async def test_channel_gates_filter_before_limit_and_skip_disabled_health_sources(enabled_platform):
    settings = _settings(
        facebook_messenger_enabled=enabled_platform == "facebook",
        instagram_messaging_enabled=enabled_platform == "instagram",
        feishu_enabled=enabled_platform == "feishu",
        email_enabled=True,
    )
    session = _session(*([()] * (1 if enabled_platform is None else 2)))

    await load_home_overview(session, _principal(), "tenant-a", settings=settings)

    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == (1 if enabled_platform is None else 2)
    if enabled_platform is None:
        assert "human_work_items" in _sql(statements[0])
    else:
        where_clause = _sql(statements[0]).split(" WHERE ", 1)[1]
        for platform in ("facebook", "instagram", "feishu", "email"):
            assert (f"platform_accounts.platform = '{platform}'" in where_clause) == (
                platform == enabled_platform
            )


@pytest.mark.parametrize(
    ("recorded_value", "expected"),
    [
        ("2026-09-06T20:00:00+08:00", _RECORDED_AT),
        ("2026-09-06T12:00:00Z", _RECORDED_AT),
        (None, None),
        ("not-a-timestamp", None),
        ("2026-09-06T12:00:00", None),
        ({"unexpected": "object"}, None),
    ],
)
async def test_alert_timestamp_is_recorded_evidence_not_a_live_probe(recorded_value, expected):
    session = _session(
        ({
            "account_id": UUID(int=1), "account_name": "Support", "platform": "facebook",
            "health_status": "ERROR", "checked_at": recorded_value,
        },),
        (),
    )

    overview = await load_home_overview(session, _principal(), "tenant-a", settings=_settings())

    assert overview.alerts == (
        HomeChannelAlert(UUID(int=1), "Support", "facebook", "ERROR", expected),
    )
    with pytest.raises(FrozenInstanceError):
        overview.alerts[0].health_status = "READY"
    with pytest.raises(FrozenInstanceError):
        overview.activities = ()


async def test_activity_sources_merge_into_stable_top_five_using_evidence_time():
    def activity(event_number: int, kind: str, minutes: int) -> dict:
        return {
            "event_id": UUID(int=event_number), "conversation_id": UUID(int=200),
            "kind": kind, "account_name": "Support",
            "occurred_at": _RECORDED_AT + timedelta(minutes=minutes),
        }

    session = _session(
        (),
        (activity(3, "handoff", 4), activity(1, "handoff", 1)),
        (activity(6, "auto_sent", 5), activity(4, "draft_sent", 4),
         activity(2, "manual_sent", 2)),
        (activity(7, "delivery_review", 6), activity(5, "delivery_failed", 4)),
    )

    overview = await load_home_overview(
        session, _principal(superadmin=True), "tenant-a", settings=_settings()
    )

    assert tuple(event.event_id.int for event in overview.activities) == (7, 6, 5, 4, 3)
    assert overview.activities[0] == HomeBusinessActivity(**activity(7, "delivery_review", 6))
    with pytest.raises(FrozenInstanceError):
        overview.activities[0].kind = "handoff"
