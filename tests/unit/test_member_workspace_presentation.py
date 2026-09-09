import uuid
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from social_reply.application.account_management import users
from social_reply.application.account_management.permissions import ROLE_CAPABILITIES
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale
from social_reply.application.account_management.workspace_member_access import (
    MemberAccountAccess,
    WorkspaceMemberAccess,
)


class FormInspector(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.details_depth = 0
        self.details = []
        self.forms = []
        self.inputs = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "details":
            self.details_depth += 1
            self.details.append(attributes)
        if tag == "form":
            self.forms.append((attributes, self.details_depth))
        if tag == "input":
            self.inputs.append(attributes)

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1


@pytest.fixture(autouse=True)
def chinese_locale():
    token = set_locale("zh-CN")
    yield
    reset_locale(token)


def member_record(role="OPERATOR", username="Ada"):
    return SimpleNamespace(
        id=uuid.uuid4(), username=username, role=role, status="active",
        must_change_password=False,
    )


def render_members(member, tab="members", count=2):
    principal = SimpleNamespace(user_id=uuid.uuid4())
    return users._member_create_form('csrf"token') + users._render_member_workspace(
        principal, [(member, count)], 'csrf"token', tab=tab,
    )


def test_member_list_has_reference_tabs_identity_scope_and_honest_missing_fields():
    member = member_record(username='<img src=x onerror="alert(1)">')
    markup = render_members(member)
    assert 'href="/admin/users?tab=members"' in markup
    assert 'href="/admin/users?tab=roles"' in markup
    assert 'href="/admin/users?tab=scope"' in markup
    assert 'aria-current="page"' in markup
    assert 'href="/admin/users/' + str(member.id) + '/access"' in markup
    assert "分配账号" in markup
    assert "2 个指定账号" in markup
    assert "member-workspace-avatar" in markup
    assert "&lt;img" in markup
    assert "<img src=x" not in markup
    assert "—" in markup
    assert "邀请" not in markup


def test_create_and_sensitive_forms_are_collapsed_and_keep_post_security_fields():
    member = member_record()
    markup = render_members(member)
    inspector = FormInspector(markup)
    assert inspector.forms
    assert all(form["method"] == "post" and depth > 0 for form, depth in inspector.forms)
    assert inspector.details
    assert all("open" not in details for details in inspector.details)
    names = [field.get("name") for field in inspector.inputs]
    assert names.count("csrf_token") == len(inspector.forms)
    assert names.count("bootstrap_password") == len(inspector.forms)
    assert "username" in names
    assert "initial_password" in names
    assert "添加成员" in markup
    assert 'action="/admin/users"' in markup
    assert f'action="/admin/users/{member.id}/password-reset"' in markup


def test_bootstrap_presentation_keeps_emergency_actions_without_grant_assignment():
    member = member_record()
    principal = SimpleNamespace(user_id=None, is_superadmin=True)
    markup = users._render_member_workspace(principal, [(member, 2)], "csrf-token")

    assert "member-workspace-tabs" in markup
    assert f'href="/admin/users/{member.id}/access"' not in markup
    assert 'name="emergency_reason"' in markup
    assert 'name="bootstrap_password"' in markup
    assert f'action="/admin/users/{member.id}/password-reset"' in markup


def test_member_cannot_edit_own_role_or_status_but_retains_other_actions():
    member = member_record("WORKSPACE_ADMIN")
    markup = users._render_member_workspace(
        SimpleNamespace(user_id=member.id), [(member, 3)], "csrf", tab="members",
    )
    assert f'action="/admin/users/{member.id}/role"' not in markup
    assert f'action="/admin/users/{member.id}/status"' not in markup
    assert f'action="/admin/users/{member.id}/password-reset"' in markup
    assert "全部账号" in markup


def test_roles_matrix_uses_live_capabilities_and_has_no_global_operator_switches(monkeypatch):
    capabilities = {**ROLE_CAPABILITIES, "VIEWER": frozenset({"test.read"})}
    monkeypatch.setattr(users, "ROLE_CAPABILITIES", capabilities)
    markup = render_members(member_record(), tab="roles")
    assert 'data-capability="test.read"' in markup
    assert 'data-role="VIEWER" data-allowed="true"' in markup
    assert 'name="operator_reply_enabled"' not in markup
    assert 'role="switch"' not in markup
    assert "按成员设置" in markup


def test_member_query_excludes_system_roles_and_scopes_counts_without_join_duplicates():
    statement = users._member_list_statement()
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    roles = next(value for value in compiled.params.values() if isinstance(value, list))
    assert "USER" in roles and "WORKSPACE_ADMIN" in roles
    assert "SUPERADMIN" not in roles and "ADMIN" not in roles
    assert "account_access_grants.active IS true" in sql
    assert "platform_accounts.tenant_id = admin_users.tenant_id" in sql
    assert "account_access_grants.tenant_id = admin_users.tenant_id" in sql
    assert "EXISTS" in sql
    assert "count(platform_accounts.id)" in sql


def test_scope_cards_preserve_owner_lock_revision_password_and_operator_switches():
    owner_id, granted_id = uuid.uuid4(), uuid.uuid4()
    member = WorkspaceMemberAccess(
        user_id=uuid.uuid4(), username="Ada", role="OPERATOR",
        operator_reply_enabled=True, operator_takeover_enabled=False,
        accounts=(
            MemberAccountAccess(owner_id, "<owned>", "email", True, False),
            MemberAccountAccess(granted_id, "Granted", "telegram", False, True),
        ), revision="revision-token",
    )
    markup = users._member_access_form(member, "csrf-token")
    inputs = {field["name"]: field for field in FormInspector(markup).inputs}
    assert "member-workspace-scope-item" in markup
    assert "&lt;owned&gt;" in markup
    assert "disabled" in inputs[f"account_{owner_id}"]
    assert "checked" in inputs[f"account_{owner_id}"]
    assert "checked" in inputs[f"account_{granted_id}"]
    assert "disabled" not in inputs[f"account_{granted_id}"]
    assert inputs["access_revision"]["value"] == "revision-token"
    assert inputs["csrf_token"]["value"] == "csrf-token"
    assert inputs["bootstrap_password"]["type"] == "password"
    assert "checked" in inputs["operator_reply_enabled"]
    assert "checked" not in inputs["operator_takeover_enabled"]


def test_access_rules_are_short_and_do_not_describe_demo_or_backend_requirements():
    markup = render_members(member_record(), tab="scope")
    assert "服务端执行要求" not in markup
    assert "本原型" not in markup
    assert "团队批量" not in markup
    assert "member-workspace-rules" in markup


@pytest.mark.parametrize("tab", ["members", "roles", "scope", "unknown"])
@pytest.mark.asyncio
async def test_workspace_route_renders_selected_tab_and_keeps_csrf_cookie(monkeypatch, tab):
    from social_reply.application.account_management.auth import Principal
    from social_reply.shared.config import DEFAULT_TENANT_ID

    member = member_record()
    principal = Principal(
        session_id=uuid.uuid4(), username="admin", actor="user:admin",
        allowed_tenants=frozenset({DEFAULT_TENANT_ID}), tenant_id=DEFAULT_TENANT_ID,
        user_id=uuid.uuid4(), role="WORKSPACE_ADMIN",
    )
    result = MagicMock()
    result.all.return_value = [(member, 2)]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def session_context():
        yield session

    monkeypatch.setattr(users, "get_session_factory", lambda: session_context)
    monkeypatch.setattr(users, "_web_principal", AsyncMock(return_value=principal))
    page_renderer = MagicMock(side_effect=lambda **context: context["body"])
    monkeypatch.setattr(users, "render_saas_page", page_renderer)
    application = FastAPI()
    application.include_router(users.router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://test",
    ) as client:
        response = await client.get(f"/admin/users?tab={tab}")
    assert response.status_code == 200
    assert "reply_admin_csrf" in response.cookies
    assert "添加成员" in page_renderer.call_args.kwargs["primary_action_html"]
    selected_tab = "members" if tab == "unknown" else tab
    assert f'href="/admin/users?tab={selected_tab}" aria-current="page"' in response.text


@pytest.mark.asyncio
async def test_system_user_route_keeps_superadmin_boundary(monkeypatch):
    from social_reply.application.account_management.auth import Principal
    from social_reply.shared.config import DEFAULT_TENANT_ID

    principal = Principal(
        session_id=uuid.uuid4(), username="admin", actor="user:admin",
        allowed_tenants=frozenset({DEFAULT_TENANT_ID}), tenant_id=DEFAULT_TENANT_ID,
        user_id=uuid.uuid4(), role="WORKSPACE_ADMIN",
    )
    session_factory = MagicMock()
    monkeypatch.setattr(users, "get_session_factory", session_factory)
    monkeypatch.setattr(users, "_web_principal", AsyncMock(return_value=principal))
    application = FastAPI()
    application.include_router(users.router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="https://test",
    ) as client:
        response = await client.get("/admin/system/users")
    assert response.status_code == 403
    session_factory.assert_not_called()
