import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from social_reply.application.account_management import saas_console
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.home_overview import (
    HomeBusinessActivity,
    HomeChannelAlert,
    HomeOverview,
)
from social_reply.application.account_management.templating import render_template, trusted_html


def _summary(*, human=0, drafts=0, delivery=0):
    return saas_console.InboxSummary(human, drafts, delivery, None, None, None)


def test_empty_home_template_leaves_content_blank():
    rendered = render_template(
        "tenant/home.html",
        next_action_html="",
        queue_summary_html="",
        message_count=0,
        channel_alerts=(),
        business_activities=(),
    )

    assert '<div class="saas-home-workspace">' in rendered
    assert "<section" not in rendered
    assert "saas-today-card" not in rendered
    assert "placeholder" not in rendered


def test_populated_home_only_renders_supplied_data_and_escapes_labels():
    rendered = render_template(
        "tenant/home.html",
        next_action_html=trusted_html('<section data-next-work="true">Review</section>'),
        queue_summary_html="",
        attention_title="Tasks",
        channel_alerts=(),
        message_count=7,
        today_title="<overview>",
        today_description="UTC; inbound and outbound",
        message_count_label="<messages>",
        business_activity_title="<activity>",
        business_activity_description="Latest business records",
        view_conversation_label="View conversation",
        business_activities=(
            {
                "title": "<real-event>",
                "account_name": "<account>",
                "href": "/app/t/default/conversations/123",
                "occurred_at": "2026-09-07T00:00:00+00:00",
                "time_label": "09-07 00:00 UTC",
            },
        ),
    )

    assert 'data-next-work="true"' in rendered
    assert "/app/t/default/conversations/123" in rendered
    assert "&lt;real-event&gt;" in rendered
    assert "&lt;account&gt;" in rendered
    assert ">7</span>" in rendered
    assert "&lt;messages&gt;" in rendered
    assert "<messages>" not in rendered
    assert "saas-home-agents" not in rendered
    assert "saas-today-ratio" not in rendered
    assert "placeholder" not in rendered


def test_empty_queue_and_activity_do_not_generate_demo_content():
    assert saas_console._home_queue_summary_grid("default", _summary()) == ""
    assert saas_console._home_activity_views("default", ()) == ()
    assert saas_console._home_next_action("default", _summary()) == ""


def test_next_action_prioritizes_delivery_risk_and_omits_unknown_waiting_time():
    rendered = saas_console._home_next_action("default", _summary(human=2, drafts=3, delivery=1))

    assert "/inbox?queue=delivery" in rendered
    assert "/inbox?queue=drafts" not in rendered
    assert "saas-nextup-desc" in rendered


def test_delivery_queue_does_not_present_task_creation_time_as_failure_wait():
    summary = saas_console.InboxSummary(0, 0, 4, None, None, datetime(2020, 1, 1, tzinfo=UTC))
    queue_html = saas_console._home_queue_summary_grid("default", summary)
    next_action = saas_console._home_next_action("default", summary)

    assert "saas-metric-sub" not in queue_html
    assert "\u7b49\u5f85" not in next_action


@pytest.mark.parametrize("queue", ["human", "drafts", "delivery"])
def test_queue_cards_only_include_nonempty_queues_without_invented_wait_or_health(queue):
    rendered = saas_console._home_queue_summary_grid("default", _summary(**{queue: 4}))

    assert rendered.count("saas-home-metric-card") == 1
    assert f"/app/t/default/inbox?queue={queue}" in rendered
    assert ">4</span>" in rendered
    assert "14m" not in rendered
    assert "SLA" not in rendered
    assert "429" not in rendered


def test_alert_is_an_observation_with_a_safe_channel_link_and_no_fabricated_check_time():
    account_id = uuid.uuid4()
    alert = HomeChannelAlert(
        account_id=account_id,
        account_name="<script>account</script>",
        platform="instagram",
        health_status="REAUTH_REQUIRED",
        checked_at=None,
    )
    views = saas_console._home_alert_views("default", (alert,))
    rendered = render_template(
        "tenant/home.html",
        channel_alerts=views,
        alerts_title="Channel notices",
        alerts_description="Recorded checks, not a live probe",
        view_channel_label="View channel",
        next_action_html="",
        queue_summary_html="",
        message_count=0,
        business_activities=(),
    )
    assert f"/app/t/default/channels/accounts/{account_id}" in rendered
    assert "&lt;script&gt;account&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    assert "<time" not in rendered
    assert "saas-home-today" not in rendered


@pytest.mark.parametrize(
    "kind",
    ["handoff", "auto_sent", "draft_sent", "manual_sent", "delivery_failed", "delivery_review"],
)
def test_business_activity_links_to_its_conversation_with_observed_event_time(kind):
    conversation_id = uuid.uuid4()
    occurred_at = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
    event = HomeBusinessActivity(uuid.uuid4(), conversation_id, kind, "Support", occurred_at)

    views = saas_console._home_activity_views("default", (event,))

    assert len(views) == 1
    assert views[0]["href"] == f"/app/t/default/conversations/{conversation_id}"
    assert views[0]["occurred_at"] == occurred_at.isoformat()
    assert views[0]["time_label"].endswith("UTC")
    assert views[0]["title"]


@pytest.mark.parametrize("role", ["MANAGER", "WORKSPACE_ADMIN"])
@pytest.mark.parametrize("received_count", [0, 9])
@pytest.mark.parametrize("has_work", [False, True])
async def test_home_loads_real_scoped_data_without_agent_readiness_queries(
    monkeypatch, role, received_count, has_work
):
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="manager",
        actor="user:manager",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role=role,
    )
    monkeypatch.setattr(
        saas_console, "_require_tenant_principal", AsyncMock(return_value=principal)
    )
    agent_loader = AsyncMock(side_effect=AssertionError("Homepage must not load agent readiness"))
    monkeypatch.setattr(saas_console, "_load_agent_ids", agent_loader)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.return_value = 0
    # Keep the former loader bounded while proving the route switches to dashboard facts.
    monkeypatch.setattr(saas_console, "_load_inbox_summary", AsyncMock(return_value=_summary()))
    monkeypatch.setattr(
        saas_console,
        "load_home_overview",
        AsyncMock(return_value=HomeOverview((), ())),
        raising=False,
    )
    current_day = datetime.now(UTC).date()
    account_id = uuid.uuid4()
    dashboard = SimpleNamespace(
        pending_count=int(has_work),
        ai_count=received_count,
        account_count=1,
        enabled_account_count=1,
        resolved_count=0,
        published_knowledge_count=0,
        draft_knowledge_count=0,
        pending_draft_count=0,
        trend=tuple(
            SimpleNamespace(
                day=current_day - timedelta(days=6 - offset),
                received=received_count,
                ai=0,
            )
            for offset in range(7)
        ),
        channels=(SimpleNamespace(platform="telegram", accounts=1, received=received_count),),
        attention=(
            SimpleNamespace(
                title="<account>",
                description="<recorded observation>",
                href=f"/app/t/default/channels/accounts/{account_id}",
                kind="channel",
            ),
        )
        if has_work
        else (),
    )
    dashboard_loader = AsyncMock(return_value=dashboard)
    monkeypatch.setattr(saas_console, "load_home_dashboard", dashboard_loader, raising=False)
    monkeypatch.setattr(saas_console, "get_session_factory", lambda: lambda: session)
    request = Request({"type": "http", "method": "GET", "path": "/app/t/default", "headers": []})

    response = await saas_console.tenant_home(request, "default")
    rendered = response.body.decode()

    assert response.status_code == 200
    dashboard_loader.assert_awaited_once()
    assert dashboard_loader.await_args.args == (session, principal, "default")
    assert dashboard_loader.await_args.kwargs["now"].utcoffset() == timedelta(0)
    agent_loader.assert_not_awaited()
    for section in (
        "当前待处理",
        "AI 接待中",
        "渠道账号",
        "已解决会话",
        "会话趋势",
        "需要关注",
        "各渠道接待",
        "接待质量",
    ):
        assert section in rendered
    assert "<svg" in rendered
    assert "home-dashboard.css" in rendered
    assert "Telegram" in rendered
    if has_work:
        assert "&lt;account&gt;" in rendered
        assert "<account>" not in rendered
        assert f"/channels/accounts/{account_id}" in rendered
    for demo_value in (">284<", ">221<", "77.8%", "rev_902", "@olivia_chen", "SLA"):
        assert demo_value not in rendered


@pytest.mark.parametrize("role", ["USER", "AGENT"])
async def test_staff_home_redirects_to_inbox_before_database_access(monkeypatch, role):
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="staff",
        actor="user:staff",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role=role,
    )
    monkeypatch.setattr(saas_console, "current_principal", AsyncMock(return_value=principal))

    def unexpected_session_factory():
        raise AssertionError("Staff home must redirect before database access")

    monkeypatch.setattr(saas_console, "get_session_factory", unexpected_session_factory)
    request = Request({"type": "http", "method": "GET", "path": "/app/t/default", "headers": []})

    response = await saas_console.tenant_home(request, "default")

    assert response.status_code == 303
    assert response.headers["location"] == "/app/t/default/inbox"
