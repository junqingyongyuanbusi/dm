"""Settings views must expose real configuration, not pretend persistence."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from social_reply.application.account_management.ui_i18n import (
    reset_locale,
    reset_request_location,
    set_locale,
    set_request_location,
)
from social_reply.application.account_management.workspace_settings_view import (
    audit_result_label,
    render_workspace_settings,
    validate_settings_section,
)


def render_settings(section="general", locale="zh-CN"):
    locale_token = set_locale(locale)
    try:
        return render_workspace_settings(
            tenant_id="tenant-a",
            section=section,
            instructions_url="/app/t/tenant-a/agents/default/instructions",
        )
    finally:
        reset_locale(locale_token)


@pytest.mark.parametrize("section", ["general", "hours", "notifications", "integration"])
def test_sections_are_real_get_links_without_fake_save(section):
    html = render_settings(section)
    for target in ("general", "hours", "notifications", "integration"):
        assert f'href="/app/t/tenant-a/settings?section={target}"' in html
    assert html.count('aria-current="page"') == 1
    assert f'data-section="{section}"' in html
    assert "<form" not in html
    assert "<button" not in html


@pytest.mark.parametrize("section", ["", "unknown", "../general", "<script>"])
def test_unknown_sections_are_rejected(section):
    with pytest.raises(ValueError, match="invalid_settings_section"):
        validate_settings_section(section)


def test_general_uses_reference_fields_without_invented_workspace_values():
    html = render_settings()
    assert "<img" not in html
    assert "当前用户" not in html
    assert "工作空间 ID" not in html
    assert "BOT_DRAFT_ONLY" not in html
    assert html.index('id="workspace-name"') < html.index('id="workspace-timezone"')
    assert html.index('id="workspace-timezone"') < html.index('id="workspace-language"')
    assert html.index('id="workspace-language"') < html.index('id="workspace-retention"')
    assert html.count(" disabled") == 3
    assert "Asia/Shanghai" not in html
    assert "90 天" not in html


def test_language_switch_preserves_current_section_and_uses_existing_locale_url():
    location_token = set_request_location(
        "/app/t/tenant-a/settings", (("section", "general"), ("ui_lang", "zh-CN"))
    )
    try:
        html = render_settings(locale="en")
    finally:
        reset_request_location(location_token)
    assert "Basic information" in html
    assert "基础信息" not in html
    assert 'href="/app/t/tenant-a/settings?section=general&amp;ui_lang=en"' in html
    assert "Not configured" in html


def test_hours_are_explicitly_unavailable_not_a_default_schedule():
    html = render_settings("hours", "en")
    assert 'type="text" disabled' in html
    assert "09:00" not in html
    assert "Asia/Shanghai" not in html
    assert "Not configured" in html


def test_notifications_link_to_real_feishu_configuration():
    html = render_settings("notifications", "en")
    assert 'href="/app/t/tenant-a/channels/feishu/handoff"' in html
    assert "Feishu" in html
    assert 'type="checkbox" disabled' in html
    assert " checked" not in html


def test_integration_links_are_canonical_and_do_not_render_credentials():
    html = render_settings("integration", "en")
    for suffix in ("channels", "health", "knowledge", "agents/default/instructions"):
        assert f'href="/app/t/tenant-a/{suffix}"' in html
    assert "App Secret" not in html
    assert "prototype" not in html.lower()


def test_invalid_section_returns_422_before_any_settings_query(monkeypatch):
    from social_reply.application.account_management import saas_console

    monkeypatch.setattr(
        saas_console, "_require_tenant_admin_principal", AsyncMock(return_value=object())
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with pytest.raises(HTTPException) as error:
        asyncio.run(saas_console.tenant_settings(request, "tenant-a", section="invalid"))
    assert error.value.status_code == 422


@pytest.mark.parametrize("detail, expected", [
    ({}, "—"),
    (None, "—"),
    ({"status": "active"}, "—"),
    ({"enabled": True}, "—"),
    ({"outcome": "success"}, "成功"),
    ({"status": "FAILED"}, "失败"),
    ({"outcome": "<script>"}, "—"),
    ({"outcome": {"status": "success"}}, "—"),
])
def test_audit_result_requires_explicit_reliable_evidence(detail, expected):
    assert audit_result_label(detail) == expected


def test_audit_keeps_real_filter_and_detail_link_inside_readonly_card(monkeypatch):
    from social_reply.application.account_management import saas_console

    record = SimpleNamespace(
        id="audit-id", created_at=datetime(2026, 9, 8, tzinfo=UTC),
        actor="<script>actor</script>", category="settings", action="account.updated",
        subject_type="account", subject_id="account-id", detail={},
    )
    audit_result = MagicMock()
    audit_result.scalars.return_value.all.return_value = [record]
    category_result = MagicMock()
    category_result.scalars.return_value = ["settings"]
    session = AsyncMock()
    session.execute.side_effect = [audit_result, category_result]
    session.__aenter__.return_value = session
    monkeypatch.setattr(saas_console, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(saas_console, "audit_read_condition", lambda *args: True)
    monkeypatch.setattr(
        saas_console, "_require_tenant_principal", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        saas_console, "_load_inbox_summary", AsyncMock(return_value=SimpleNamespace(total=0))
    )
    monkeypatch.setattr(saas_console, "_render_page", lambda **values: HTMLResponse(values["body"]))
    locale_token = set_locale("en")
    try:
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        response = asyncio.run(saas_console.tenant_audit(request, "tenant-a", category="settings"))
    finally:
        reset_locale(locale_token)
    html = response.body.decode()
    assert 'class="workspace-audit-card"' in html
    assert 'method="get"' in html and 'name="category"' in html
    assert '<option value="settings" selected>' in html
    assert 'href="/app/t/tenant-a/audit/audit-id"' in html
    assert "Read-only records" in html
    assert '<th scope="col">Actor</th>' in html
    assert '<th scope="col">Result</th>' in html
    assert '<td>—</td>' in html
    assert "workspace-audit-note" not in html
    assert "&lt;script&gt;actor&lt;/script&gt;" in html
    assert "<script>" not in html
    assert html.count("<button") == 1
