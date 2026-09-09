"""Presentation contracts for real server-rendered Agent workspaces."""

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from social_reply.application.account_management.templating import render_template, trusted_html


class FormContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str | None]] = []
        self.fields: list[dict[str, str | None]] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.forms.append(attributes)
        if tag in {"input", "textarea", "select"}:
            self.fields.append(attributes)
        if tag == "a" and attributes.get("href") is not None:
            self.links.append(attributes["href"] or "")


def parse_contract(html: str) -> FormContractParser:
    parser = FormContractParser()
    parser.feed(html)
    return parser


def agent_card(agent_id: str = "wikiglobal") -> SimpleNamespace:
    return SimpleNamespace(
        name=f"{agent_id} <script>alert(1)</script>",
        agent_id=agent_id,
        scope_label="Scope",
        prompt_text="Prompt v3",
        status_html=trusted_html('<span class="saas-status neutral">Unconfigured</span>'),
        mode_label="Mode",
        mode_html=trusted_html('<span class="saas-status neutral">Draft only</span>'),
        channels_label="Visible accounts",
        active_accounts=0,
        account_count=2,
        knowledge_label="Published knowledge",
        published_count=0,
        release_label="Release",
        release_text="Latest v7 / Deployed v5",
        readiness_label="Readiness",
        readiness_percent=25,
        next_step_label="Next step",
        next_step="Connect a channel",
        open_href=f"/app/t/tenant-a/agents/{agent_id}/overview",
        open_label="Open Agent",
    )


def list_context(*, cards: tuple[SimpleNamespace, ...], show_lifecycle: bool) -> dict:
    return {
        "cards": cards,
        "show_lifecycle": show_lifecycle,
        "list_summary": f"{len(cards)} visible agents",
        "scope_description": "Only authorized Agent scopes",
        "lifecycle_eyebrow": "Configuration",
        "lifecycle_title": "Agent lifecycle",
        "lifecycle_description": "Define, test, and deploy",
        "lifecycle_stages": tuple(
            {
                "label": section,
                "description": f"Open {section}",
                "href": f"/app/t/tenant-a/agents/wikiglobal/{section}",
                "current": False,
            }
            for section in ("overview", "instructions", "test", "channels", "activity")
        ),
    }


@pytest.mark.parametrize("show_lifecycle", [True, False])
def test_agent_list_uses_visible_cards_and_real_configuration_links(show_lifecycle: bool) -> None:
    cards = (agent_card(), agent_card("research"))
    context = list_context(cards=cards, show_lifecycle=show_lifecycle)
    html = render_template("tenant/agent_list.html", **context)
    contract = parse_contract(html)

    assert 'class="agent-workspace-layout"' in html
    assert 'class="agent-workspace-list"' in html
    assert 'id="agent-workspace-configuration"' in html
    assert '/static/agent-workspace.css' in html
    assert set(contract.links) == {
        *(card.open_href for card in cards),
        *(stage["href"] for stage in context["lifecycle_stages"]),
    }
    assert not contract.forms and not contract.fields
    assert "Latest v7 / Deployed v5" in html
    assert "Prompt v3" in html
    assert "0/2" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "threshold" not in html and "agentLanguage" not in html


def test_empty_agent_list_does_not_link_to_a_fallback_agent() -> None:
    html = render_template(
        "tenant/agent_list.html", **list_context(cards=(), show_lifecycle=False)
    )
    assert "0 visible agents" in html
    assert "Only authorized Agent scopes" in html
    assert "agent-workspace-empty" in html
    assert not parse_contract(html).links
    assert 'id="agent-workspace-configuration"' not in html


def test_card_renders_without_list_context_and_keeps_server_status() -> None:
    html = render_template("components/agent_card.html", card=agent_card())
    assert parse_contract(html).links == [agent_card().open_href]
    assert "Draft only" in html and "Unconfigured" in html
    assert "Latest v7 / Deployed v5" in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_create_form_preserves_post_contract_and_escaped_error_values() -> None:
    labels = (
        "eyebrow", "form_title", "form_description", "name_label", "name_placeholder",
        "slug_label", "slug_placeholder", "slug_help", "description_label",
        "description_placeholder", "description_help", "cancel_label", "submit_label",
        "next_title", "next_description", "safety_title", "safety_description",
    )
    html = render_template(
        "tenant/agent_create.html",
        **dict.fromkeys(labels, "Create configuration"),
        form_action="/app/t/tenant-a/agents",
        cancel_href="/app/t/tenant-a/agents",
        csrf_token="csrf-contract-token",
        name='wikiglobal " <script>unsafe</script>',
        slug="financial-support",
        description="Research\n</textarea><script>unsafe</script>",
        error_message="Name already exists <script>unsafe</script>",
        steps=({"number": "01", "title": "Identity", "description": "Create draft"},),
    )
    contract = parse_contract(html)
    assert contract.forms[0]["method"] == "post"
    assert contract.forms[0]["action"] == "/app/t/tenant-a/agents"
    assert {field["name"] for field in contract.fields} == {
        "csrf_token", "name", "slug", "description",
    }
    assert contract.fields[0]["value"] == "csrf-contract-token"
    assert contract.links == ["/app/t/tenant-a/agents"]
    assert 'role="alert"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '/static/agent-workspace.css' in html


def build_test_context(**overrides: object) -> dict:
    labels = (
        "playground_title", "playground_description", "sandbox_label", "message_label",
        "message_placeholder", "isolation_notice", "run_label", "read_only_notice",
        "result_eyebrow", "result_title", "completed_label", "action_label", "intent_label",
        "risk_label", "confidence_label", "duration_label", "reason_codes_label", "reply_label",
        "no_reply_label", "context_title", "context_description", "prompt_label",
        "knowledge_label", "channels_label", "mode_label", "guardrails_title",
    )
    return {
        **{label: label for label in labels},
        "can_run": True,
        "test_action": "/app/t/tenant-a/agents/wikiglobal/test",
        "csrf_token": "csrf-test-token",
        "input_text": "Review broker license",
        "error_message": "",
        "result": None,
        "prompt_version": "v3",
        "published_knowledge_count": 0,
        "active_account_count": 0,
        "account_count": 2,
        "mode": "Draft only",
        "guardrails": ("No customer message is sent",),
        **overrides,
    }


def test_debug_form_preserves_isolation_and_actual_effective_context() -> None:
    html = render_template("tenant/agent_test.html", **build_test_context())
    contract = parse_contract(html)
    assert contract.forms[0]["action"] == "/app/t/tenant-a/agents/wikiglobal/test"
    assert contract.forms[0]["method"] == "post"
    assert {field["name"] for field in contract.fields} == {"csrf_token", "text"}
    assert contract.fields[1]["maxlength"] == "4000"
    assert "Review broker license" in html
    assert 'id="agent-test-isolation">isolation_notice</span>' in html
    assert "agent-workspace-guardrails" not in html
    assert "context_description" not in html
    assert "v3" in html and "0/2" in html and "Draft only" in html
    assert "agent-workspace-output-empty" in html
    assert "completed_label" not in html
    assert '/static/agent-workspace.css' in html


def test_read_only_debug_view_never_exposes_submission_controls() -> None:
    html = render_template("tenant/agent_test.html", **build_test_context(can_run=False))
    contract = parse_contract(html)
    assert not contract.forms and not contract.fields
    assert "csrf-test-token" not in html
    assert "read_only_notice" in html


@pytest.mark.parametrize("reply_text", ["", "License check\n<script>unsafe</script>"])
def test_debug_result_preserves_handoff_reasons_and_safe_multiline_reply(reply_text: str) -> None:
    result = SimpleNamespace(
        action="handoff", intent="broker_verification", risk_level="high", confidence=0.25,
        duration_ms=127, reason_codes=("NO_TRUSTED_KNOWLEDGE", "HUMAN_REQUIRED"),
        reply_text=reply_text,
    )
    html = render_template("tenant/agent_test.html", **build_test_context(result=result))
    assert 'aria-live="polite"' in html
    for value in ("handoff", "broker_verification", "high", "0.25", "127 ms"):
        assert value in html
    assert "NO_TRUSTED_KNOWLEDGE" in html and "HUMAN_REQUIRED" in html
    assert "<script>" not in html
    if reply_text:
        assert "&lt;script&gt;" in html
    else:
        assert "no_reply_label" in html
    assert "agent-workspace-output-empty" not in html


def test_debug_error_is_announced_without_a_success_indicator() -> None:
    html = render_template(
        "tenant/agent_test.html",
        **build_test_context(error_message="Trial failed <script>unsafe</script>"),
    )
    assert 'role="alert"' in html
    assert "Trial failed &lt;script&gt;" in html
    assert "completed_label" not in html


def test_production_card_and_lifecycle_context_render_without_extra_fields() -> None:
    from social_reply.application.account_management import saas_console

    card = saas_console._build_agent_card_view(
        tenant_id="tenant-a", agent_id="wikiglobal", accounts=[], published_count=0,
        prompt=None,
        control_plane=saas_console.AgentControlPlaneView(
            name="wikiglobal", status="active", version_revision=7,
            deployed_version_revision=5,
        ),
    )
    html = render_template(
        "tenant/agent_list.html",
        **saas_console._agent_lifecycle_context("tenant-a", "wikiglobal"),
        cards=(card,), show_lifecycle=True, list_summary="1", scope_description="Visible",
    )
    contract = parse_contract(html)
    assert card.release_text in html
    assert card.prompt_text in html
    assert card.open_href in contract.links
    assert "/app/t/tenant-a/agents/wikiglobal/instructions" in contract.links
    assert "/app/t/tenant-a/agents/wikiglobal/test" in contract.links


def test_production_debug_renderer_keeps_embedded_result_selectors() -> None:
    from social_reply.application.account_management import saas_console

    result = SimpleNamespace(
        action="handoff", intent=None, risk_level="high", confidence=0.0,
        duration_ms=42, reason_codes=("HUMAN_REQUIRED",), reply_text="",
    )
    html, mode = saas_console._render_agent_test_workspace(
        tenant_id="tenant-a", agent_id="wikiglobal", can_run=False, csrf_token="not-exposed",
        accounts=[], prompt_pointer=None, published_knowledge_count=0, trial_result=result,
    )
    assert mode == "unconfigured"
    assert not parse_contract(html).forms
    assert "not-exposed" not in html
    assert 'class="saas-test-result"' in html
    assert 'class="saas-test-result-grid"' in html
    assert 'class="saas-test-reply"' in html
    assert "HUMAN_REQUIRED" in html and "42 ms" in html
