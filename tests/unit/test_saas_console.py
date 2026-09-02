import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from apps.api.main import create_app
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.saas_ui import (
    format_age,
    navigation_icon,
    render_saas_page,
)
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


def _tenant_principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role="SUPERADMIN",
    )


def test_format_age_returns_a_duration_for_waiting_copy() -> None:
    current_time = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    started_at = current_time - timedelta(minutes=5)

    assert format_age(started_at, now=current_time) == "5 分钟"

    locale_token = set_locale("en")
    try:
        assert format_age(started_at, now=current_time) == "5 minutes"
    finally:
        reset_locale(locale_token)


def test_navigation_icons_are_outline_svg_with_a_square_fallback() -> None:
    inbox_icon = navigation_icon("inbox")
    unknown_icon = navigation_icon("not-a-known-navigation-key")

    assert '<svg class="saas-nav-icon" aria-hidden="true"' in inbox_icon
    assert 'fill="none"' in inbox_icon
    assert 'stroke="currentColor"' in inbox_icon
    assert '<rect x="4" y="4" width="16" height="16" rx="3"/>' in unknown_icon


def _ordinary_user_principal(tenant_id: str = "tenant-a") -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        tenant_id=tenant_id,
        allowed_tenants=frozenset({tenant_id}),
        role="USER",
    )


async def test_saas_workspace_redirects_unauthenticated_users_to_login() -> None:
    async with await _client() as client:
        response = await client.get("/app")

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Fapp"


async def test_saas_workspace_login_redirect_preserves_path_and_query() -> None:
    async with await _client() as client:
        response = await client.get("/app/t/default/inbox?queue=drafts")

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/login?next=%2Fapp%2Ft%2Fdefault%2Finbox%3Fqueue%3Ddrafts"
    )


async def test_tenant_workspace_authenticates_non_default_tenant_paths() -> None:
    async with await _client() as client:
        response = await client.get("/app/t/tenant-b")
        redirect_response = await client.get("/app/t/tenant-b/agents/default")

    assert response.status_code == 303
    assert response.headers["location"] == ("/auth/login?next=%2Fapp%2Ft%2Ftenant-b")
    assert redirect_response.status_code == 303
    assert redirect_response.headers["location"] == (
        "/auth/login?next=%2Fapp%2Ft%2Ftenant-b%2Fagents%2Fdefault"
    )


async def test_database_user_enters_the_fixed_default_workspace(monkeypatch) -> None:
    from social_reply.application.account_management import saas_console

    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="database-user",
        actor="user:database-user",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="USER",
    )

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        response = await client.get("/app")

    assert response.status_code == 303
    assert response.headers["location"] == "/app/t/default"


async def test_database_user_cannot_enter_non_default_workspace(monkeypatch) -> None:
    from social_reply.application.account_management import saas_console

    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="tenant-a-user",
        actor="user:tenant-a-user",
        tenant_id="tenant-a",
        allowed_tenants=frozenset({"tenant-a"}),
        role="USER",
    )

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        response = await client.get("/app")

    assert response.status_code == 404
    assert response.json() == {"detail": "tenant_workspace_not_found"}


async def test_superadmin_selector_redirects_to_default_tenant_workspace(monkeypatch) -> None:
    from social_reply.application.account_management import saas_console

    principal = Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role="SUPERADMIN",
    )

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        selector_response = await client.get("/app")

    assert selector_response.status_code == 303
    assert selector_response.headers["location"] == "/app/t/default"


async def test_tenant_user_cannot_open_system_admin_pages(monkeypatch) -> None:
    from social_reply.application.account_management import saas_console

    async def fake_current_principal(_request):
        return _ordinary_user_principal()

    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        response = await client.get("/admin/system/overview")

    assert response.status_code == 403
    assert response.json() == {"detail": "superadmin_required"}


def test_saas_shell_keeps_tenant_and_system_navigation_distinct() -> None:
    tenant_html = render_saas_page(
        principal=_tenant_principal(),
        title="首页",
        description="Tenant 工作区",
        body="<p>tenant body</p>",
        active_navigation="home",
        tenant_id="tenant-a",
    )
    system_principal = Principal(
        session_id=uuid.uuid4(),
        username="system-admin",
        actor="bootstrap:system-admin",
        allowed_tenants=frozenset({"tenant-a"}),
        role="SUPERADMIN",
    )
    system_html = render_saas_page(
        principal=system_principal,
        title="系统总览",
        description="系统控制面",
        body="<p>system body</p>",
        active_navigation="system-overview",
        tenant_id=None,
        system_admin=True,
    )

    assert "/app/t/tenant-a/agents" in tenant_html
    assert "跨租户审计" not in tenant_html
    for group_label in ("工作区", "AI Studio", "洞察", "管理"):
        assert f">{group_label}<" in tenant_html
    assert "href=\"/app/t/tenant-a\" aria-current='page'" in tenant_html
    assert '<script src="/static/theme.js?v=' in tenant_html
    assert tenant_html.index("/static/theme.js") < tenant_html.index("/static/saas.css")
    assert '<link rel="stylesheet" href="/static/saas.css?v=' in tenant_html
    assert '<script src="/static/app.js?v=' in tenant_html
    assert '" defer></script>' in tenant_html
    assert "data-sidebar-toggle" in tenant_html
    assert "data-sidebar-close" in tenant_html
    assert "data-sidebar" in tenant_html
    tenant_sidebar_start = tenant_html.index('<aside class="saas-sidebar"')
    tenant_sidebar_end = tenant_html.index("</aside>", tenant_sidebar_start)
    tenant_topbar_start = tenant_html.index('<header class="saas-topbar')
    tenant_topbar_end = tenant_html.index("</header>", tenant_topbar_start)
    assert tenant_sidebar_start < tenant_html.index('class="saas-brand"') < tenant_sidebar_end
    assert "saas-brand" not in tenant_html[tenant_topbar_start:tenant_topbar_end]
    assert '<svg class="saas-nav-icon" aria-hidden="true"' in tenant_html
    assert tenant_html.count('class="saas-nav-icon"') >= 8
    assert 'data-popover-trigger="language-menu"' in tenant_html
    assert 'data-popover-trigger="theme-menu"' in tenant_html
    assert 'aria-label="Language"' in tenant_html
    assert "简体中文（中国）" in tenant_html
    assert "English" in tenant_html
    for theme_option in ("system", "light", "dark"):
        assert f'data-theme-option="{theme_option}"' in tenant_html
    assert "羊毛纸" not in tenant_html
    assert "Wool Paper" not in tenant_html
    assert 'href="/auth/logout"' in tenant_html
    assert "data-refresh" not in tenant_html
    assert "saas-global-search" not in tenant_html
    assert 'data-page-layout="page"' in tenant_html
    assert "saas-page-header" in tenant_html
    assert "系统管理员" in system_html
    for system_path in (
        "/admin/system/overview",
        "/admin/system/users",
        "/admin/system/safety",
        "/admin/system/audit",
    ):
        assert system_path in system_html
    assert "/admin/system/health" not in system_html
    assert 'href="/app"' in system_html
    assert "进入租户工作区" in system_html
    assert "/app/t/tenant-a/agents" not in system_html
    assert 'href="/admin/system/overview"' in tenant_html
    assert 'href="/app/t/tenant-a/channels"' in tenant_html


def test_tenant_topbar_uses_neutral_workspace_context_without_selector() -> None:
    html = render_saas_page(
        principal=Principal(
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            username="workspace-user",
            actor="user:workspace-user",
            tenant_id="default",
            allowed_tenants=frozenset({"default"}),
            role="USER",
        ),
        title="首页",
        description="工作区首页",
        body="<p>body</p>",
        active_navigation="home",
        tenant_id="default",
    )

    topbar_start = html.index('<header class="saas-topbar')
    topbar_end = html.index("</header>", topbar_start)
    topbar_html = html[topbar_start:topbar_end]

    assert "工作区" in topbar_html
    assert "default" not in topbar_html
    assert "Tenant" not in topbar_html
    assert "saas-tenant-switcher" not in topbar_html
    assert 'href="/app"' not in topbar_html
    assert 'data-popover-trigger="language-menu"' in topbar_html
    assert 'data-popover-trigger="theme-menu"' in topbar_html


class _ScalarResult:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self):
        return _ScalarValues(self._values)


class _ScalarValues:
    def __init__(self, values) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)

    def all(self):
        return list(self._values)


class _AgentBrandSession:
    def __init__(self, result_batches: list[list[str]]) -> None:
        self._result_batches = iter(result_batches)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(next(self._result_batches))


async def test_user_agent_ids_only_include_owned_account_brands() -> None:
    from social_reply.application.account_management import saas_console

    session = _AgentBrandSession([["owned-brand"]])

    agent_ids = await saas_console._load_agent_ids(
        session,
        _ordinary_user_principal(),
        "tenant-a",
    )

    assert agent_ids == ["owned-brand"]
    assert len(session.statements) == 1


async def test_user_without_accounts_only_gets_default_onboarding_agent() -> None:
    from social_reply.application.account_management import saas_console

    session = _AgentBrandSession([[]])

    agent_ids = await saas_console._load_agent_ids(
        session,
        _ordinary_user_principal(),
        "tenant-a",
    )

    assert agent_ids == ["default"]
    assert len(session.statements) == 1


async def test_admin_agent_ids_keep_full_tenant_brand_enumeration() -> None:
    from social_reply.application.account_management import saas_console

    session = _AgentBrandSession(
        [["account-brand"], ["prompt-brand"], ["knowledge-brand"], ["control-brand"]]
    )

    agent_ids = await saas_console._load_agent_ids(
        session,
        _tenant_principal(),
        "tenant-a",
    )

    assert agent_ids == [
        "account-brand",
        "control-brand",
        "default",
        "knowledge-brand",
        "prompt-brand",
    ]
    assert len(session.statements) == 4


async def test_agent_control_plane_view_distinguishes_latest_and_deployed_versions() -> None:
    from types import SimpleNamespace

    from social_reply.application.account_management import saas_console

    agent_id = uuid.uuid4()
    deployed_version_id = uuid.uuid4()
    latest_version_id = uuid.uuid4()
    session = _AgentBrandSession(
        [
            [
                SimpleNamespace(
                    id=agent_id,
                    legacy_brand_id="support",
                    name="Support Agent",
                    status="active",
                )
            ],
            [
                SimpleNamespace(
                    id=latest_version_id,
                    agent_id=agent_id,
                    revision=2,
                ),
                SimpleNamespace(
                    id=deployed_version_id,
                    agent_id=agent_id,
                    revision=1,
                ),
            ],
            [
                SimpleNamespace(
                    agent_id=agent_id,
                    agent_version_id=deployed_version_id,
                    revision=1,
                )
            ],
        ]
    )

    views = await saas_console._load_agent_control_plane_views(
        session,
        "tenant-a",
        ["support"],
    )

    assert views["support"] == saas_console.AgentControlPlaneView(
        name="Support Agent",
        status="active",
        version_revision=2,
        deployed_version_revision=1,
    )
    assert len(session.statements) == 3


def test_workbench_shell_omits_page_chrome_and_marks_layout() -> None:
    html = render_saas_page(
        principal=_tenant_principal(),
        title="收件箱",
        description="不应显示的大标题说明",
        body="<div data-test-workbench-body>workspace</div>",
        active_navigation="inbox",
        tenant_id="tenant-a",
        primary_action_html='<a href="/ignored">Ignored</a>',
        breadcrumbs=(("首页", "/app/t/tenant-a"),),
        workbench=True,
    )

    assert 'class="saas-surface saas-surface-tenant saas-layout-workbench"' in html
    assert 'data-page-layout="workbench"' in html
    assert 'class="saas-main saas-main-workbench"' in html
    assert "data-workbench-main" in html
    assert "saas-page-header" not in html
    assert "saas-breadcrumbs" not in html
    assert '<div class="saas-content">' not in html
    assert "data-test-workbench-body" in html


def test_ordinary_user_shell_only_shows_authorized_navigation_items() -> None:
    html = render_saas_page(
        principal=_ordinary_user_principal(),
        title="首页",
        description="普通用户工作区",
        body="<p>body</p>",
        active_navigation="home",
        tenant_id="tenant-a",
    )

    for label in (
        "首页",
        "收件箱",
        "对话",
        "Agents",
        "知识查询",
        "我的活动",
        "Channels",
        "个人中心",
    ):
        assert f">{label}<" in html
    for forbidden_label in ("审计中心", "Processing Journey", "工作区设置", "打开系统后台"):
        assert forbidden_label not in html
    assert 'href="/admin"' not in html


def test_saas_shell_uses_request_locale_without_translating_identity_data() -> None:
    locale_token = set_locale("en")
    try:
        html = render_saas_page(
            principal=_tenant_principal(),
            title="Home",
            description="Tenant workspace",
            body="<p>body</p>",
            active_navigation="home",
            tenant_id="tenant-a",
        )
    finally:
        reset_locale(locale_token)

    assert '<html lang="en">' in html
    for group_label in ("Workspace", "AI Studio", "Insights", "Manage"):
        assert f">{group_label}<" in html
    assert 'href="?ui_lang=zh-CN"' in html
    assert "system-admin" in html
    assert "tenant-a" in html
    assert "管理控制面" not in html


def test_english_shell_and_representative_body_have_localized_product_copy() -> None:
    from social_reply.application.account_management import saas_console

    locale_token = set_locale("en")
    try:
        body = saas_console._render_agent_overview(
            tenant_id="tenant-a",
            agent_id="default",
            accounts=[],
            prompt_pointer=None,
            knowledge_counts={"published": 0, "draft": 0},
            is_admin=False,
        )
        html = render_saas_page(
            principal=_ordinary_user_principal(),
            title="Agents",
            description="Review agent configuration.",
            body=body,
            active_navigation="agents",
            tenant_id="tenant-a",
        )
    finally:
        reset_locale(locale_token)

    assert '<html lang="en">' in html
    assert "Connect the first channel account" in html
    assert "Runtime summary" in html
    assert "Business instructions are versioned" in html
    assert "tenant-a" in html
    assert "operator" in html
    for untranslated_product_copy in (
        "连接第一个渠道账号",
        "运行摘要",
        "业务指令已版本化",
        "就绪度",
        "打开系统后台",
    ):
        assert untranslated_product_copy not in html


def test_user_agent_instruction_summary_hides_prompt_and_admin_contract() -> None:
    from social_reply.application.account_management import saas_console

    summary_html = saas_console._render_user_agent_behavior_summary(
        accounts=[SimpleNamespace(automation_default="BOT_DRAFT_ONLY")],
        published_knowledge_count=2,
    )

    assert "仅生成草稿" in summary_html
    assert "已发布知识" in summary_html
    assert "转人工" in summary_html
    for forbidden_value in (
        "完整业务指令",
        "content_hash",
        "内容 Hash",
        "updated_by",
        "更新人",
        "代码固定安全契约",
        "Immutable WikiFX response contract",
        "<textarea",
        "/admin",
    ):
        assert forbidden_value not in summary_html


def test_admin_agent_instruction_editor_is_path_scoped_and_complete() -> None:
    from social_reply.application.account_management import saas_console
    from social_reply.application.account_management.reply_prompt_policy import (
        ReplyBusinessPromptVersionSummary,
    )
    from social_reply.application.account_management.reply_prompt_web import (
        ReplyBusinessPromptEditorView,
    )

    version_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    historical_agent_version_id = uuid.uuid4()
    updated_at = datetime(2026, 9, 1, 10, 30, tzinfo=UTC)
    editor_view = ReplyBusinessPromptEditorView(
        tenant_id="default",
        brand_id="brand-a",
        current_content="Answer directly, then provide one practical next step.",
        current_revision=3,
        content_hash="a" * 64,
        updated_by="user:tenant-admin",
        updated_at=updated_at,
        is_default=False,
        versions=(
            ReplyBusinessPromptVersionSummary(
                id=version_id,
                revision=2,
                content="Earlier prompt content.",
                content_hash="b" * 64,
                change_note="Earlier version",
                created_by="user:tenant-admin",
                created_at=updated_at - timedelta(hours=1),
                is_active=False,
                agent_version_id=historical_agent_version_id,
                agent_revision=3,
            ),
        ),
        has_channel=True,
        latest_agent_version_id=agent_version_id,
        latest_agent_revision=4,
        deployed_agent_version_id=None,
        deployed_agent_revision=None,
        deployment_revision=0,
    )

    editor_html = saas_console._render_admin_reply_prompt_editor(
        editor_view,
        csrf_token="safe-csrf",
    )

    canonical_root = "/app/t/default/agents/brand-a/instructions"
    assert "Answer directly, then provide one practical next step." in editor_html
    assert "a" * 64 in editor_html
    assert "user:tenant-admin" in editor_html
    assert "09-01 10:30" in editor_html
    assert 'name="content"' in editor_html
    assert 'maxlength="4000"' in editor_html
    assert 'name="change_note"' in editor_html
    assert 'maxlength="240"' in editor_html
    assert 'name="expected_revision" value="3"' in editor_html
    assert f'action="{canonical_root}/save"' in editor_html
    assert f'action="{canonical_root}/trial"' in editor_html
    assert f'action="{canonical_root}/versions/{version_id}/rollback"' in editor_html
    assert f'action="{canonical_root}/releases/{agent_version_id}/deploy"' in editor_html
    assert (
        f'action="{canonical_root}/releases/{historical_agent_version_id}/deploy"'
        in editor_html
    )
    assert 'name="expected_deployment_revision" value="0"' in editor_html
    assert 'name="tenant_id"' not in editor_html
    assert 'name="brand_id"' not in editor_html
    assert "/admin/content/reply-prompt" not in editor_html


def test_inbox_workbench_has_two_panes_search_and_nested_selected_action() -> None:
    from social_reply.application.account_management import saas_console

    item = saas_console.InboxItem(
        item_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        queue="human",
        title="Customer One",
        platform="telegram",
        channel_type="dm",
        account_name="Support Bot",
        status="WAITING",
        reason="HUMAN_REQUESTED",
        created_at=datetime.now(UTC),
        work_item_version=1,
    )
    summary = saas_console.InboxSummary(
        human_count=1,
        draft_count=2,
        delivery_count=3,
        oldest_human_at=item.created_at,
        oldest_draft_at=item.created_at,
        oldest_delivery_at=item.created_at,
    )
    queue_tabs = saas_console._render_queue_tabs(
        "tenant-a",
        "human",
        summary,
        include_admin_queues=True,
    )
    selected_list = saas_console._render_inbox_item_list(
        "tenant-a",
        "human",
        [item],
        item,
    )
    empty_thread = saas_console._render_conversation_thread(
        "tenant-a",
        [],
        None,
        suggested_item=item,
    )
    empty_workspace = saas_console._render_inbox_workspace(
        queue_tabs=queue_tabs,
        item_list=selected_list,
        thread=empty_thread,
        action_panel=saas_console._render_inbox_action_panel("tenant-a", None),
        item_count=1,
    )

    assert "saas-inbox-layout" in empty_workspace
    assert empty_workspace.count('<section class="saas-inbox-column ') == 2
    assert '<main class="saas-inbox-column ' not in empty_workspace
    assert "data-inbox-list" in empty_workspace
    assert "data-inbox-thread" in empty_workspace
    assert "data-inbox-actions" not in empty_workspace
    assert "<aside" not in empty_workspace
    assert 'aria-label="工作队列"' in empty_workspace
    assert "1 个工作项" in empty_workspace
    assert "data-list-search" in empty_workspace
    assert "data-search-empty" in empty_workspace
    assert 'data-filter-text="Customer One Support Bot telegram"' in selected_list
    assert "aria-current='page'" in queue_tabs
    assert "aria-current='true'" in selected_list
    assert "选择一个工作项" in empty_thread
    assert "打开最老工作项" in empty_thread
    assert empty_thread.count("saas-button primary") == 1

    selected_thread = saas_console._render_conversation_thread(
        "tenant-a",
        [],
        item,
    )
    selected_action = saas_console._render_inbox_action_panel("tenant-a", item)
    selected_workspace = saas_console._render_inbox_workspace(
        queue_tabs=queue_tabs,
        item_list=selected_list,
        thread=selected_thread,
        action_panel=selected_action,
        item_count=1,
    )
    workspace_start = selected_workspace.index(
        '<section class="saas-inbox-column saas-workspace-pane"'
    )
    workspace_end = selected_workspace.rindex("</section>\n</div>")
    action_start = selected_workspace.index("data-inbox-actions")

    assert workspace_start < action_start < workspace_end
    assert selected_workspace.count("data-inbox-actions") == 1
    assert selected_workspace.count('<section class="saas-inbox-column ') == 2
    assert '<main class="saas-inbox-column ' not in selected_workspace


def test_draft_action_panel_renders_review_forms_with_optimistic_fences() -> None:
    from social_reply.application.account_management import saas_console

    draft_item = saas_console.InboxItem(
        item_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        queue="drafts",
        title="Draft Customer",
        platform="telegram",
        channel_type="dm",
        account_name="Support Bot",
        status="PENDING",
        reason="NEEDS_REVIEW",
        created_at=datetime.now(UTC),
        draft_text="Original <draft> text",
        decision_generation=7,
        review_action="PENDING",
    )

    action_panel = saas_console._render_inbox_action_panel(
        "tenant-a",
        draft_item,
        csrf_token="safe-csrf-token",
    )

    assert "Original &lt;draft&gt; text" in action_panel
    assert 'name="final_reply_text"' in action_panel
    assert 'maxlength="10000"' in action_panel
    assert f'action="/app/t/tenant-a/decisions/{draft_item.item_id}/approve"' in action_panel
    assert f'action="/app/t/tenant-a/decisions/{draft_item.item_id}/discard"' in action_panel
    assert 'name="review_reason"' in action_panel
    assert 'maxlength="500"' in action_panel
    assert 'name="expected_generation" value="7"' in action_panel
    assert 'name="expected_review_action" value="PENDING"' in action_panel
    assert action_panel.count('name="csrf_token" value="safe-csrf-token"') == 2
    assert f"/conversations/{draft_item.conversation_id}" not in action_panel


def test_delivery_action_panel_renders_fenced_recovery_forms_without_sensitive_data() -> None:
    from social_reply.application.account_management import saas_console

    failed_item = saas_console.InboxItem(
        item_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        queue="delivery",
        title="Delivery Customer",
        platform="telegram",
        channel_type="dm",
        account_name="Support Bot",
        status="FAILED",
        reason="PROVIDER_REJECTED",
        created_at=datetime.now(UTC),
        expected_status="FAILED",
        expected_attempt_count=4,
        delivery_error_code="PROVIDER_REJECTED",
    )
    retry_panel = saas_console._render_inbox_action_panel(
        "tenant-a",
        failed_item,
        csrf_token="safe-csrf-token",
    )

    assert f'action="/app/t/tenant-a/delivery/{failed_item.item_id}/retry"' in retry_panel
    assert 'name="expected_status" value="FAILED"' in retry_panel
    assert 'name="expected_attempt_count" value="4"' in retry_panel
    assert 'name="review_reason"' in retry_panel
    assert 'maxlength="500"' in retry_panel
    assert 'name="verification_source"' in retry_panel
    assert 'name="csrf_token" value="safe-csrf-token"' in retry_panel
    assert "PROVIDER_REJECTED" in retry_panel
    assert "Attempt 4" in retry_panel or "第 4 次" in retry_panel
    assert f"/conversations/{failed_item.conversation_id}" not in retry_panel

    needs_review_item = saas_console.InboxItem(
        item_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        queue="delivery",
        title="Review Customer",
        platform="telegram",
        channel_type="dm",
        account_name="Support Bot",
        status="NEEDS_REVIEW",
        reason="AMBIGUOUS_SEND",
        created_at=datetime.now(UTC),
        expected_status="NEEDS_REVIEW",
        expected_attempt_count=5,
        delivery_error_code="AMBIGUOUS_SEND",
    )
    review_panel = saas_console._render_inbox_action_panel(
        "tenant-a",
        needs_review_item,
        csrf_token="safe-csrf-token",
    )

    assert f'action="/app/t/tenant-a/delivery/{needs_review_item.item_id}/resolve"' in review_panel
    for resolution in (
        "CONFIRMED_NOT_SENT_RETRY",
        "CONFIRMED_SENT",
        "CANCEL",
    ):
        assert f'name="resolution" value="{resolution}"' in review_panel
    assert 'name="provider_message_id"' in review_panel
    assert review_panel.count('name="csrf_token" value="safe-csrf-token"') == 3
    assert "AMBIGUOUS_SEND" in review_panel
    assert "Attempt 5" in review_panel or "第 5 次" in review_panel
    assert f"/conversations/{needs_review_item.conversation_id}" not in review_panel
    for sensitive_value in (
        "provider-secret-token",
        "provider-target-secret",
        "full-customer-body-secret",
        "secret-payload-text",
    ):
        assert sensitive_value not in retry_panel
        assert sensitive_value not in review_panel


def test_conversations_workbench_has_two_searchable_panes_in_both_locales() -> None:
    from social_reply.application.account_management import saas_console

    conversation_items = (
        '<a class="saas-work-item" data-filter-text="Customer One Support Bot telegram">'
        "Customer One</a>"
    )
    workspace_empty = '<section class="saas-empty">Select a conversation</section>'

    chinese_workspace = saas_console._render_conversations_workspace(
        conversation_items=conversation_items,
        item_count=1,
        workspace_empty=workspace_empty,
    )

    locale_token = set_locale("en")
    try:
        workspace = saas_console._render_conversations_workspace(
            conversation_items=conversation_items,
            item_count=1,
            workspace_empty=workspace_empty,
        )
    finally:
        reset_locale(locale_token)

    assert chinese_workspace.count('<section class="saas-inbox-column ') == 2
    assert '<main class="saas-inbox-column ' not in chinese_workspace
    assert "按名称、账号或平台搜索" in chinese_workspace
    assert workspace.count('<section class="saas-inbox-column ') == 2
    assert '<main class="saas-inbox-column ' not in workspace
    assert "data-conversation-list" in workspace
    assert "data-conversation-main" in workspace
    assert "1 conversations" in workspace
    assert "Search name, account, or platform" in workspace
    assert "data-list-search" in workspace
    assert "data-search-empty" in workspace
    assert 'data-filter-text="Customer One Support Bot telegram"' in workspace
    assert workspace_empty in workspace


def test_channel_forms_use_localized_pending_labels() -> None:
    from social_reply.application.account_management import saas_console

    locale_token = set_locale("en")
    try:
        html = saas_console._channel_oauth_form(
            action="/app/t/tenant-a/channels/oauth/x/start",
            csrf="safe-csrf",
            tenant_id="tenant-a",
            label="Authorize with X",
            brand_id="indonesia_support",
            available=True,
        )
    finally:
        reset_locale(locale_token)

    assert html.count('data-pending-label="Connecting…"') == 2
    assert "safe-csrf" in html
    assert 'name="brand_id" value="indonesia_support"' in html
    assert "正在连接" not in html


def test_channels_javascript_only_polls_in_flight_jobs() -> None:
    source = Path("src/social_reply/static/channels.js").read_text()
    polling_statuses = source.split("const pollingStatuses = new Set(", 1)[1].split(");", 1)[0]

    assert '"PENDING"' in polling_statuses
    assert '"PROCESSING"' in polling_statuses
    assert '"FAILED"' not in polling_statuses
    assert "previousStatus !== job.status" in source
    assert "window.location.reload()" in source


def test_ordinary_user_agent_sections_never_link_to_admin_pages() -> None:
    from social_reply.application.account_management import saas_console

    account = SimpleNamespace(
        name="My Telegram",
        platform="telegram",
        status="active",
        automation_default="BOT_DRAFT_ONLY",
        config_version=1,
    )
    channels_html = saas_console._render_agent_channels(
        [account],
        tenant_id="tenant-a",
        agent_id="default",
        is_admin=False,
    )
    instructions_html = saas_console._render_agent_instructions(
        tenant_id="tenant-a",
        agent_id="default",
        prompt_pointer=None,
        prompt_version=None,
        is_admin=False,
    )
    knowledge_html = saas_console._render_agent_knowledge(
        "tenant-a",
        "default",
        {"published": 0, "draft": 0},
        is_admin=False,
    )

    combined_html = channels_html + instructions_html + knowledge_html
    assert "/admin" not in combined_html
    assert "/app/t/tenant-a/channels" in channels_html
    assert "/app/t/tenant-a/knowledge-query" in knowledge_html


def test_admin_agent_channel_link_preserves_agent_scope() -> None:
    from social_reply.application.account_management import saas_console

    html = saas_console._render_agent_channels(
        [],
        tenant_id="tenant-a",
        agent_id="indonesia_support",
        is_admin=True,
    )

    assert "/app/t/tenant-a/channels?brand_id=indonesia_support" in html
    assert "/admin/integrations/accounts" not in html


def test_admin_agent_knowledge_link_is_canonical_and_preserves_brand() -> None:
    from social_reply.application.account_management import saas_console

    knowledge_html = saas_console._render_agent_knowledge(
        "tenant-a",
        "brand-a",
        {"published": 2, "draft": 1},
        is_admin=True,
    )

    assert "/app/t/tenant-a/knowledge?brand_id=brand-a" in knowledge_html
    assert "/admin/content/knowledge" not in knowledge_html


def test_knowledge_filter_location_preserves_all_scopes() -> None:
    from social_reply.application.account_management import saas_console

    location = saas_console._knowledge_location(
        "tenant-a",
        status_filter="review",
        brand_id="brand-a",
        platform="telegram",
        category="faq",
    )

    assert location == (
        "/app/t/tenant-a/knowledge?status_filter=review&brand_id=brand-a"
        "&platform=telegram&category=faq"
    )


def test_ordinary_user_repairs_unavailable_accounts_from_channels() -> None:
    from social_reply.application.account_management import saas_console

    unavailable_account = SimpleNamespace(status="disabled")

    html = saas_console._render_agent_overview(
        tenant_id="tenant-a",
        agent_id="default",
        accounts=[unavailable_account],
        prompt_pointer=None,
        knowledge_counts={"published": 0, "draft": 0},
        is_admin=False,
    )

    assert "修复不可用的渠道账号" in html
    assert "/app/t/tenant-a/channels" in html
    assert "/app/t/tenant-a/profile" not in html


def test_ordinary_user_without_accounts_gets_direct_authorization_action() -> None:
    from social_reply.application.account_management import saas_console

    summary = saas_console.InboxSummary(
        human_count=0,
        draft_count=0,
        delivery_count=0,
        oldest_human_at=None,
        oldest_draft_at=None,
        oldest_delivery_at=None,
    )

    html = saas_console._home_next_action(
        _ordinary_user_principal(),
        "tenant-a",
        summary,
        0,
    )

    assert "授权你的第一个平台账号" in html
    assert "/app/t/tenant-a/channels" in html
    assert "授权新账号" in html


def test_channel_avatar_only_accepts_known_https_provider_hosts() -> None:
    from social_reply.application.account_management import saas_console

    assert (
        saas_console._safe_channel_avatar_url("https://pbs.twimg.com/profile_images/account.jpg")
        == "https://pbs.twimg.com/profile_images/account.jpg"
    )
    assert (
        saas_console._safe_channel_avatar_url("https://scontent.example.fbcdn.net/avatar.jpg")
        == "https://scontent.example.fbcdn.net/avatar.jpg"
    )
    for unsafe_url in (
        "http://pbs.twimg.com/avatar.jpg",
        "https://pbs.twimg.com.evil.example/avatar.jpg",
        "https://user:password@pbs.twimg.com/avatar.jpg",
        "javascript:alert(1)",
        "https://example.com/avatar.jpg",
    ):
        assert saas_console._safe_channel_avatar_url(unsafe_url) is None


def test_channel_avatar_falls_back_without_rendering_unsafe_url() -> None:
    from social_reply.application.account_management import saas_console

    account = SimpleNamespace(
        avatar_url="https://attacker.example/avatar.jpg",
        name="Owned Account",
        platform="instagram",
    )

    avatar_html = saas_console._channel_avatar(account)

    assert "attacker.example" not in avatar_html
    assert "fallback" in avatar_html
    assert ">O<" in avatar_html


async def test_ordinary_user_cannot_open_admin_or_tenant_governance_pages(monkeypatch) -> None:
    from social_reply.application.account_management import admin, admin_console, saas_console

    principal = _ordinary_user_principal("default")

    async def fake_current_principal(_request):
        return principal

    monkeypatch.setattr(admin, "current_principal", fake_current_principal)
    monkeypatch.setattr(admin_console, "current_principal", fake_current_principal)
    monkeypatch.setattr(saas_console, "current_principal", fake_current_principal)
    async with await _client() as client:
        admin_response = await client.get("/admin")
        audit_response = await client.get("/app/t/default/audit")
        draft_queue_response = await client.get("/app/t/default/inbox?queue=drafts")

    assert admin_response.status_code == 403
    assert admin_response.json() == {"detail": "tenant_admin_required"}
    assert audit_response.status_code == 403
    assert audit_response.json() == {"detail": "tenant_admin_required"}
    assert draft_queue_response.status_code == 403
    assert draft_queue_response.json() == {"detail": "inbox_queue_access_denied"}
