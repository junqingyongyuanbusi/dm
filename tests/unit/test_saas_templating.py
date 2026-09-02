import uuid

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
