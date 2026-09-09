import uuid
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from starlette.requests import Request
from starlette.responses import RedirectResponse

from social_reply.application.account_management import saas_console, workspace_pages
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale
from social_reply.application.account_management.workspace_i18n import workspace_text
from social_reply.application.account_management.workspace_queries import (
    agent_choices_statement,
    contact_conversations_statement,
    contact_list_statement,
    report_statements,
)


def _principal():
    return Principal(
        session_id=uuid.uuid4(),
        username="reader",
        actor="reader",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        user_id=uuid.uuid4(),
    )


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def test_contact_queries_scope_both_contact_and_account_and_bound_results():
    principal = _principal()
    query = contact_list_statement(principal, "default", search="<name>%_", page=1)
    rendered = _sql(query)
    assert "contacts.tenant_id =" in rendered
    assert "platform_accounts.tenant_id =" in rendered
    assert "platform_accounts.owner_user_id =" in rendered
    assert "account_access_grants.active IS true" in rendered
    assert "account_access_grants.user_id =" in rendered
    assert "account_access_grants.tenant_id = platform_accounts.tenant_id" in rendered
    assert "shared_with_support" not in rendered
    assert "LIMIT" in rendered
    assert "OFFSET" in rendered
    assert "<name>" not in rendered
    assert "credential" not in rendered
    assert "contacts.created_at DESC, contacts.id DESC" in rendered
    parameters = query.compile().params
    assert any("/%/_" in value for value in parameters.values() if isinstance(value, str))


def test_contact_detail_conversations_enforce_tenant_account_and_contact_consistency():
    rendered = _sql(contact_conversations_statement(_principal(), "default", uuid.uuid4()))
    assert "conversations.tenant_id =" in rendered
    assert "contacts.tenant_id =" in rendered
    assert "conversations.platform_account_id = contacts.platform_account_id" in rendered
    assert "platform_accounts.owner_user_id =" in rendered
    assert "LIMIT" in rendered


def test_cross_tenant_queries_fail_closed():
    statement = contact_list_statement(_principal(), "other", search="", page=1)
    assert "false" in _sql(statement)


@pytest.mark.parametrize("days", [7, 30])
def test_reports_use_bounded_recording_window_and_account_scopes(days):
    end = datetime(2026, 9, 8, tzinfo=UTC)
    statements = report_statements(_principal(), "default", days=days, end=end)
    assert set(statements) == {"messages", "conversations", "human", "outbox"}
    for statement in statements.values():
        rendered = _sql(statement)
        assert "platform_accounts.owner_user_id =" in rendered
        assert "platform_accounts.tenant_id =" in rendered
        assert "conversations.tenant_id =" in rendered
        assert "created_at >=" in rendered
        assert "created_at <" in rendered
        parameters = statement.compile().params.values()
        assert end in parameters
        assert end - timedelta(days=days) in parameters
    assert "messages.private IS false" in _sql(statements["messages"])
    assert "outbox_messages.platform_account_id = platform_accounts.id" in _sql(
        statements["outbox"]
    )
    assert "outbox_messages.tenant_id =" in _sql(statements["outbox"])


@pytest.mark.parametrize("days", [0, 1, 8, 365])
def test_reports_reject_unsupported_windows(days):
    with pytest.raises(ValueError, match="report_window_invalid"):
        report_statements(_principal(), "default", days=days, end=datetime.now(UTC))


def test_agent_choices_only_select_existing_visible_brands():
    rendered = _sql(agent_choices_statement(_principal(), "default"))
    assert "platform_accounts.owner_user_id =" in rendered
    assert "platform_accounts.tenant_id =" in rendered
    assert "LIMIT" in rendered
    assert "credential" not in rendered


@pytest.mark.parametrize("page", ["contacts", "reports", "flows", "playground"])
async def test_every_workspace_page_checks_capability_before_database(monkeypatch, page):
    def deny(capability):
        assert capability == f"{page}.read"
        raise HTTPException(status_code=403, detail="capability_denied")

    monkeypatch.setattr(
        saas_console,
        "_require_tenant_principal",
        AsyncMock(return_value=SimpleNamespace(require_capability=deny)),
    )
    database = Mock(side_effect=AssertionError("authorization must precede database"))
    monkeypatch.setattr(workspace_pages, "get_session_factory", database)
    request = Request({"type": "http", "method": "GET", "path": f"/app/t/default/{page}"})
    with pytest.raises(HTTPException) as denied:
        await getattr(workspace_pages, f"workspace_{page}")(request, "default")
    assert denied.value.status_code == 403
    database.assert_not_called()


async def test_contact_detail_checks_contacts_capability(monkeypatch):
    require_capability = Mock(side_effect=HTTPException(status_code=403))
    monkeypatch.setattr(
        saas_console,
        "_require_tenant_principal",
        AsyncMock(return_value=SimpleNamespace(require_capability=require_capability)),
    )
    request = Request({"type": "http", "method": "GET", "path": "/"})
    with pytest.raises(HTTPException):
        await workspace_pages.workspace_contact_detail(request, "default", uuid.uuid4())
    require_capability.assert_called_once_with("contacts.read")


async def test_login_redirect_is_preserved_without_loading_database(monkeypatch):
    redirect = RedirectResponse("/auth/login", status_code=303)
    monkeypatch.setattr(saas_console, "_require_tenant_principal", AsyncMock(return_value=redirect))
    request = Request({"type": "http", "method": "GET", "path": "/"})
    assert await workspace_pages.workspace_flows(request, "default") is redirect


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_contact_template_escapes_data_and_has_truthful_empty_state(locale):
    token = set_locale(locale)
    try:
        response = workspace_pages.render_workspace_page(
            principal=_principal(),
            tenant_id="default",
            page="contacts",
            contacts=(),
            search="<script>alert(1)</script>",
            selected=None,
            conversations=(),
            previous_href="",
            next_href="",
        )
        rendered = response.body.decode()
        assert "<script>alert(1)</script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert workspace_text("contacts.empty") in rendered
        assert response.headers["Cache-Control"] == "no-store"
        assert workspace_text("contacts.title") in rendered
    finally:
        reset_locale(token)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_flows_and_playground_expose_limits_not_fake_execution_controls(locale):
    token = set_locale(locale)
    try:
        for page in ("flows", "playground"):
            response = workspace_pages.render_workspace_page(
                principal=_principal(),
                tenant_id="default",
                page=page,
                agents=(),
            )
            rendered = response.body.decode()
            assert workspace_text(f"{page}.notice") in rendered
            assert workspace_text("agents.empty") in rendered
            assert '<form method="post"' not in rendered
            assert "localStorage" not in rendered
            assert 'type="range"' not in rendered
            assert 'wp-test-boundaries' not in rendered
            assert 'saas-flow-branch-warning' not in rendered
    finally:
        reset_locale(token)


@pytest.mark.parametrize("days", ["7", "30"])
async def test_report_period_form_accepts_http_query_strings(monkeypatch, days):
    monkeypatch.setattr(
        workspace_pages, "_require_workspace_principal", AsyncMock(return_value=_principal())
    )
    loader = AsyncMock(
        return_value={
            "platforms": (),
            "human": (),
            "outbox": (),
            "metrics": (),
        }
    )
    monkeypatch.setattr(workspace_pages, "_load_report", loader)
    app = FastAPI()
    app.include_router(workspace_pages.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/app/t/default/reports?days={days}")
    assert response.status_code == 200
    assert loader.await_args.args[2] == int(days)
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("days", ["8", "365", "-7", "no"])
async def test_report_period_rejects_invalid_http_values_without_querying(monkeypatch, days):
    monkeypatch.setattr(
        workspace_pages, "_require_workspace_principal", AsyncMock(return_value=_principal())
    )
    loader = AsyncMock()
    monkeypatch.setattr(workspace_pages, "_load_report", loader)
    app = FastAPI()
    app.include_router(workspace_pages.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/app/t/default/reports?days={days}")
    assert response.status_code == 422
    loader.assert_not_awaited()


@pytest.mark.parametrize("existing_csrf", ["", "existing-workspace-token"])
async def test_playground_embeds_only_a_visible_real_agent_with_csrf(monkeypatch, existing_csrf):
    monkeypatch.setattr(
        workspace_pages, "_require_workspace_principal", AsyncMock(return_value=_principal())
    )
    destination = "/app/t/default/agents/real-agent/test"
    monkeypatch.setattr(
        workspace_pages,
        "_load_agent_choices",
        AsyncMock(
            return_value=(
                {
                    "brand_id": "real-agent",
                    "name": "Real Agent",
                    "href": "/app/t/default/agents/real-agent",
                    "test_href": destination,
                },
            )
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "https",
            "headers": [(b"cookie", f"reply_admin_csrf={existing_csrf}".encode())],
        }
    )
    response = await workspace_pages.workspace_playground(request, "default", "real-agent")
    assert response.status_code == 200
    rendered = response.body.decode()
    assert f'action="{destination}"' in rendered
    assert 'name="csrf_token"' in rendered
    assert 'name="text"' in rendered
    assert 'maxlength="4000"' in rendered
    assert 'id="workspace-test-output"' in rendered
    if existing_csrf:
        csrf_token = existing_csrf
        assert "set-cookie" not in response.headers
    else:
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        csrf_token = cookies["reply_admin_csrf"].value
    assert csrf_token
    assert f'name="csrf_token" value="{csrf_token}"' in rendered
    assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPException) as denied:
        await workspace_pages.workspace_playground(request, "default", "https://evil.example")
    assert denied.value.status_code == 404


@pytest.mark.parametrize(
    ("submitted_token", "extra_fields", "expected_status"),
    [("wrong-token", {}, 403), ("cookie-token", {"agent_id": "other-agent"}, 422)],
)
async def test_embedded_trial_reuses_post_csrf_and_exact_field_validation(
    monkeypatch, submitted_token, extra_fields, expected_status
):
    monkeypatch.setattr(
        saas_console, "_require_tenant_principal", AsyncMock(return_value=_principal())
    )
    session_context = AsyncMock()
    monkeypatch.setattr(saas_console, "get_session_factory", lambda: lambda: session_context)
    monkeypatch.setattr(saas_console, "_load_agent_ids", AsyncMock(return_value={"real-agent"}))
    trial = AsyncMock()
    monkeypatch.setattr(saas_console, "run_reply_business_prompt_trial", trial)
    app = FastAPI()
    app.include_router(saas_console.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post(
            "/app/t/default/agents/real-agent/test",
            headers={"cookie": "reply_admin_csrf=cookie-token"},
            data={"csrf_token": submitted_token, "text": "What is forex risk?", **extra_fields},
        )
    assert response.status_code == expected_status
    trial.assert_not_awaited()


@pytest.mark.parametrize("error_key", ["", "agent.instructions.trial.rate_limited"])
def test_existing_trial_response_preserves_inline_result_selector_contract(error_key):
    trial_result = SimpleNamespace(
        action="draft", intent="risk_education", risk_level="low", confidence=0.8,
        duration_ms=123, reason_codes=("PROMPT_TRIAL",), reply_text="<unsafe>answer</unsafe>",
    )
    rendered, _mode = saas_console._render_agent_test_workspace(
        tenant_id="default",
        agent_id="real-agent",
        can_run=True,
        csrf_token="contract-token",
        accounts=[],
        prompt_pointer=None,
        published_knowledge_count=0,
        trial_result=trial_result if not error_key else None,
        error_message_key=error_key,
    )
    if error_key:
        assert 'class="saas-alert danger saas-test-result"' in rendered
    else:
        assert 'class="saas-test-result"' in rendered
        assert 'class="saas-test-result-grid"' in rendered
        assert 'class="saas-test-reply"' in rendered
        assert "&lt;unsafe&gt;answer&lt;/unsafe&gt;" in rendered
        assert "123 ms" in rendered


def test_flow_canvas_has_accessible_local_selection_without_mutation_actions():
    rendered = workspace_pages.render_workspace_page(
        principal=_principal(), tenant_id="default", page="flows", agents=()
    ).body.decode()
    for stage in workspace_pages._FLOW_STAGES:
        assert f'id="flow-stage-{stage}"' in rendered
        assert workspace_text(f"flows.{stage}_note") in rendered
    assert 'type="radio"' in rendered
    assert 'method="post"' not in rendered
    assert "localStorage" not in rendered


def test_contact_row_keeps_real_detail_link_and_escapes_identity():
    rendered = workspace_pages.render_workspace_page(
        principal=_principal(),
        tenant_id="default",
        page="contacts",
        search="",
        selected=None,
        contacts=(
            {
                "display_name": "<customer>",
                "external_user_id": "real-external-id",
                "platform": "telegram",
                "account_name": "Assigned account",
                "created_at": None,
                "href": "/app/t/default/contacts/real-contact",
            },
        ),
        conversations=(),
        previous_href="",
        next_href="",
    ).body.decode()
    assert 'href="/app/t/default/contacts/real-contact"' in rendered
    assert "&lt;customer&gt;" in rendered
    assert "real-external-id" in rendered
    assert "Assigned account" in rendered


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_report_template_has_four_metrics_real_trend_and_collapsed_counting_notes(locale):
    token = set_locale(locale)
    try:
        end = datetime(2026, 9, 8, tzinfo=UTC)
        response = workspace_pages.render_workspace_page(
            principal=_principal(),
            tenant_id="default",
            page="reports",
            days=7,
            start=end - timedelta(days=7),
            end=end,
            platforms=(
                {"platform": "<telegram>", "inbound": 12, "outbound": 3, "active_conversations": 4},
            ),
            trend=(
                {"day": end.date() - timedelta(days=1), "inbound": 12, "outbound": 3},
                {"day": end.date(), "inbound": 0, "outbound": 0},
            ),
            trend_max=12,
            metrics=tuple(
                {"label": workspace_text(f"reports.{key}"), "value": value}
                for key, value in (
                    ("inbound", 12), ("outbound", 3),
                    ("active_conversations", 4), ("new_conversations", 2),
                )
            ),
        )
        rendered = response.body.decode()
        assert "&lt;telegram&gt;" in rendered
        assert '<span class="saas-metric-value">12</span>' in rendered
        assert rendered.count('class="saas-metric-value"') == 4
        disclosure = rendered.split('<details class="wp-report-methodology">', 1)[1]
        disclosure = disclosure.split('</details>', 1)[0]
        assert disclosure.count('<p>') == 2
        assert workspace_text("reports.messages_note") in disclosure
        assert workspace_text("reports.conversations_note") in disclosure
        assert workspace_text("reports.cohort_note") not in rendered
        assert workspace_text("reports.not_available") not in rendered
        assert 'class="wp-report-trend"' in rendered
        assert '2026-09-07' in rendered
        assert 'report-human_status' not in rendered
        assert 'report-outbox_status' not in rendered
        assert rendered.index('id="report-days"') < rendered.index('saas-report-metrics')
        assert "<canvas" not in rendered
    finally:
        reset_locale(token)


def _database_result(*, rows=(), selected=None, scalar=0):
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    result.mappings.return_value.one_or_none.return_value = selected
    result.scalar_one.return_value = scalar
    return result


def _mock_session(monkeypatch, results):
    session = AsyncMock()
    session.execute.side_effect = results
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(workspace_pages, "get_session_factory", lambda: lambda: context)
    return session


async def test_unknown_or_inaccessible_contact_returns_404_before_conversation_query(monkeypatch):
    monkeypatch.setattr(
        workspace_pages, "_require_workspace_principal", AsyncMock(return_value=_principal())
    )
    session = _mock_session(monkeypatch, [_database_result()])
    request = Request({"type": "http", "method": "GET", "path": "/"})
    with pytest.raises(HTTPException) as missing:
        await workspace_pages.workspace_contact_detail(request, "default", uuid.uuid4())
    assert missing.value.status_code == 404
    assert session.execute.await_count == 1


async def test_report_loader_combines_only_database_counts_with_bounded_aggregates(monkeypatch):
    end = datetime(2026, 9, 8, 12, tzinfo=UTC)
    platforms = (
        {
            "platform": "telegram",
            "inbound": 4,
            "outbound": 1,
            "active_conversations": 2,
            "human_replies": 1,
        },
        {
            "platform": "email",
            "inbound": 7,
            "outbound": 3,
            "active_conversations": 3,
            "human_replies": 0,
        },
    )
    session = _mock_session(
        monkeypatch,
        [
            _database_result(),
            _database_result(rows=platforms),
            _database_result(scalar=2),
            _database_result(
                rows=({"day": end.date(), "inbound": 11, "outbound": 4},)
            ),
        ],
    )
    report = await workspace_pages._load_report(_principal(), "default", 7, end)
    metrics = {metric["label"]: metric["value"] for metric in report["metrics"]}
    assert metrics[workspace_text("reports.inbound")] == 11
    assert metrics[workspace_text("reports.outbound")] == 4
    assert metrics[workspace_text("reports.active_conversations")] == 5
    assert metrics[workspace_text("reports.new_conversations")] == 2
    assert len(metrics) == 4
    assert len(report["trend"]) == 8  # Rolling seven days spans two partial UTC dates.
    assert report["trend"][0] == {
        "day": end.date() - timedelta(days=7), "inbound": 0, "outbound": 0,
    }
    assert report["trend"][-1] == {"day": end.date(), "inbound": 11, "outbound": 4}
    assert report["trend_max"] == 11
    trend_statement = session.execute.await_args_list[-1].args[0]
    trend_sql = _sql(trend_statement)
    for scope in (
        "messages.private IS false", "platform_accounts.tenant_id =",
        "conversations.tenant_id =", "platform_accounts.owner_user_id =",
        "account_access_grants.user_id =", "messages.created_at >=", "messages.created_at <",
    ):
        assert scope in trend_sql
    assert end in trend_statement.compile().params.values()
    assert end - timedelta(days=7) in trend_statement.compile().params.values()
    assert "statement_timeout = '5000ms'" in str(session.execute.await_args_list[0].args[0])


@pytest.mark.parametrize("days", [7, 30])
async def test_empty_report_keeps_zero_counts_and_does_not_add_an_extra_midnight_day(
    monkeypatch, days
):
    _mock_session(monkeypatch, [_database_result() for _ in range(4)])
    end = datetime(2026, 9, 8, tzinfo=UTC)
    report = await workspace_pages._load_report(_principal(), "default", days, end)
    assert len(report["metrics"]) == 4
    assert all(metric["value"] == 0 for metric in report["metrics"])
    assert len(report["trend"]) == days
    assert report["trend"][-1]["day"] == end.date() - timedelta(days=1)
    assert all(record["inbound"] == record["outbound"] == 0 for record in report["trend"])
    assert report["trend_max"] == 1


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_contact_detail_escapes_names_and_links_real_conversations(locale):
    token = set_locale(locale)
    try:
        contact_id, conversation_id = uuid.uuid4(), uuid.uuid4()
        selected = {
            "id": contact_id,
            "display_name": "<script>customer</script>",
            "external_user_id": "user<&",
            "platform": "telegram",
            "account_name": "<account>",
            "created_at": datetime(2026, 9, 8, tzinfo=UTC),
        }
        response = workspace_pages.render_workspace_page(
            principal=_principal(),
            tenant_id="default",
            page="contacts",
            selected=selected,
            conversations=({"id": conversation_id, "channel_type": "dm", "created_at": None},),
        )
        rendered = response.body.decode()
        assert "&lt;script&gt;customer&lt;/script&gt;" in rendered
        assert "&lt;account&gt;" in rendered
        assert f"/app/t/default/conversations/{conversation_id}" in rendered
        assert workspace_text("contacts.detail_note") in rendered
    finally:
        reset_locale(token)
