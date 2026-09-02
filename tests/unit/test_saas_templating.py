import uuid
from types import SimpleNamespace

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.saas_ui import render_saas_page
from social_reply.application.account_management.templating import render_template


def _principal() -> Principal:
    return Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        tenant_id="default",
        allowed_tenants=frozenset({"default"}),
        role="USER",
    )


def test_shared_template_autoescapes_plain_context() -> None:
    html = render_template(
        "shared/page.html",
        locale="en",
        title="<unsafe-title>",
        product_name="Reply Core",
        asset_version="test",
        refresh_seconds=0,
        surface="tenant",
        layout_mode="page",
        close_navigation_label='Close "navigation"',
        skip_to_content_label="Skip <content>",
        header_html="<header>unsafe</header>",
        page_layout_html="<main>unsafe</main>",
    )

    assert "&lt;unsafe-title&gt;" in html
    assert "Skip &lt;content&gt;" in html
    assert "<header>unsafe</header>" not in html
    assert "&lt;header&gt;unsafe&lt;/header&gt;" in html


def test_saas_renderer_marks_only_escaped_view_html_as_trusted() -> None:
    html = render_saas_page(
        principal=_principal(),
        title="<unsafe-title>",
        description="<unsafe-description>",
        body="<section data-safe-view>body</section>",
        active_navigation="home",
        tenant_id="default",
    )

    assert "&lt;unsafe-title&gt;" in html
    assert "&lt;unsafe-description&gt;" in html
    assert "<section data-safe-view>body</section>" in html
    assert '<aside class="saas-sidebar"' in html


def test_agent_card_template_escapes_scope_data_and_shows_readiness() -> None:
    from social_reply.application.account_management import saas_console

    html = saas_console._render_agent_card(
        tenant_id="default",
        agent_id='<img src=x onerror="alert(1)">',
        accounts=[
            SimpleNamespace(
                status="active",
                automation_default="BOT_DRAFT_ONLY",
            )
        ],
        published_count=2,
        prompt=SimpleNamespace(revision='<script>alert("prompt")</script>'),
    )

    assert "<img src=x" not in html
    assert "<script>" not in html
    assert "&lt;Img Src=X Onerror=" in html
    assert "&lt;script&gt;alert" in html
    assert "100%" in html
    assert 'class="saas-agent-open"' in html


def test_agent_card_shows_latest_and_production_versions_without_trusting_identity() -> None:
    from social_reply.application.account_management import saas_console

    html = saas_console._render_agent_card(
        tenant_id="tenant-a",
        agent_id="support",
        accounts=[],
        published_count=0,
        prompt=None,
        control_plane=saas_console.AgentControlPlaneView(
            name='<script>alert("agent")</script>',
            status="active",
            version_revision=2,
            deployed_version_revision=1,
        ),
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert" in html
    assert "配置 v2 · 生产 v1" in html


def test_agent_lifecycle_template_uses_real_product_routes() -> None:
    from social_reply.application.account_management import saas_console

    html = render_template(
        "tenant/agent_list.html",
        **saas_console._agent_lifecycle_context(
            "tenant-a",
            "support",
            current_stage="test",
        ),
        list_summary="All 0",
        scope_description="No agent yet",
        cards=(),
    )

    assert "/app/t/tenant-a/agents/support/instructions" in html
    assert "/app/t/tenant-a/agents/support/test" in html
    assert "/app/t/tenant-a/agents/support/channels" in html
    assert "/app/t/tenant-a/agents/support/activity" in html
    assert html.count('aria-current="step"') == 1


def test_agent_test_workspace_is_isolated_and_autoescapes_model_output() -> None:
    from social_reply.application.account_management import saas_console

    html, mode = saas_console._render_agent_test_workspace(
        tenant_id="tenant-a",
        agent_id="support",
        can_run=True,
        csrf_token='safe-token"><script>alert(1)</script>',
        accounts=[
            SimpleNamespace(
                status="active",
                automation_default="BOT_DRAFT_ONLY",
            )
        ],
        prompt_pointer=SimpleNamespace(revision=4),
        published_knowledge_count=8,
        trial_result=SimpleNamespace(
            action="draft",
            intent="withdrawal_delay",
            risk_level="low",
            confidence=0.91,
            duration_ms=248,
            reason_codes=("KNOWLEDGE_MATCH",),
            reply_text='<img src=x onerror="alert(2)">',
        ),
    )

    assert mode == "BOT_DRAFT_ONLY"
    assert 'action="/app/t/tenant-a/agents/support/test"' in html
    assert 'aria-current="step"' in html
    assert "不会创建生产决策、Outbox 或外发消息" in html
    assert "KNOWLEDGE_MATCH" in html
    assert "248 ms" in html
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
