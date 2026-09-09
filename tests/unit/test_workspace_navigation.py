import uuid

import pytest

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.saas_ui import (
    _tenant_navigation_groups,
    render_saas_page,
)
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


@pytest.mark.parametrize(
    ("role", "expected_keys"),
    [
        (
            "WORKSPACE_ADMIN",
            (
                "home",
                "inbox",
                "contacts",
                "agents",
                "flows",
                "knowledge",
                "playground",
                "channels",
                "reports",
                "users",
                "audit",
                "settings",
            ),
        ),
        (
            "MANAGER",
            (
                "home",
                "inbox",
                "contacts",
                "agents",
                "flows",
                "knowledge",
                "playground",
                "channels",
                "reports",
            ),
        ),
        ("OPERATOR", ("inbox", "channels")),
        ("AGENT", ("inbox", "contacts", "knowledge")),
        ("USER", ("inbox", "contacts", "knowledge")),
        ("VIEWER", ("inbox", "reports", "audit")),
        ("UNKNOWN", ()),
    ],
)
def test_navigation_matches_role_capabilities_in_prototype_order(role, expected_keys):
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="staff",
        actor="user:staff",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role=role,
    )
    groups = _tenant_navigation_groups(principal, "default", 3)

    assert tuple(item.key for group in groups for item in group.items) == expected_keys
    assert all(group.items for group in groups)
    assert all("/admin/system" not in item.href for group in groups for item in group.items)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_workspace_keeps_brand_language_and_three_theme_modes(locale):
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="<support>",
        actor="user:support",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="AGENT",
    )
    locale_token = set_locale(locale)
    try:
        document = render_saas_page(
            principal=principal,
            tenant_id="default",
            title="Inbox",
            description="",
            body="",
            active_navigation="inbox",
        )
    finally:
        reset_locale(locale_token)

    assert "wikiglobal" in document
    assert f'<html lang="{locale}"' in document
    assert "&lt;support&gt;" in document
    assert 'data-popover-trigger="language-menu"' in document
    assert all(f'data-theme-option="{theme}"' in document for theme in ("system", "light", "dark"))
    assert document.index("/static/theme.js") < document.index("/static/saas.css")


def test_workspace_shell_has_safe_context_without_internal_design_material():
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="OPERATOR",
    )
    document = render_saas_page(
        principal=principal,
        tenant_id="default",
        title="Inbox <script>unsafe</script>",
        description="",
        body="",
        active_navigation="inbox",
    )

    assert 'class="saas-context-current"' in document
    assert 'href="/app/t/default/design"' not in document
    assert "设计说明与参考" not in document
    assert "saas-sidebar-collaboration" not in document
    assert "Inbox &lt;script&gt;unsafe&lt;/script&gt;" in document
    assert "<script>unsafe</script>" not in document
    assert 'id="roleSelect"' not in document
    assert 'href="/admin/users"' not in document
    assert 'href="/app/t/default/settings"' not in document
    assert '/static/workspace-pages.css?v=' in document


def test_internal_design_reference_is_not_a_workspace_route():
    from social_reply.application.account_management.workspace_pages import router

    assert all(route.path != "/app/t/{tenant_id}/design" for route in router.routes)


def test_reference_assets_are_loaded_only_for_relevant_workspaces():
    principal = Principal(
        session_id=uuid.uuid4(), username="admin", actor="admin",
        allowed_tenants=frozenset({"default"}), role="WORKSPACE_ADMIN",
    )
    for active_navigation in ("channels", "contacts"):
        document = render_saas_page(
            principal=principal, tenant_id="default", title="Page", description="",
            body="", active_navigation=active_navigation,
        )
        assert ("/static/channel-workspace.css?v=" in document) == (
            active_navigation == "channels"
        )
        assert ("/static/channel-workspace.js?v=" in document) == (
            active_navigation == "channels"
        )
