"""Rendering contracts; execution is delegated to CI / the parent agent."""

from html.parser import HTMLParser

import pytest
from fastapi import HTTPException, Request

from social_reply.application.account_management import knowledge_read_view
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


class _WorkspaceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.disclosures = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "details":
            self.disclosures.append(attributes)
        if tag == "a":
            self.links.append(attributes.get("href", ""))


def _render_workspace(*, can_manage=True, **overrides):
    context = {
        "root": "/app/t/demo",
        "can_manage": can_manage,
        "counts": {"published": 31, "draft": 9},
        "review_count": 4,
        "loaded_count": 12,
        "shown_count": 2,
        "table_html": '<table id="real-documents"><tr><td>Actual record</td></tr></table>',
        **overrides,
    }
    return knowledge_read_view.render_knowledge_workspace(**context)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_real_documents_precede_collapsed_examples_and_tools(locale):
    token = set_locale(locale)
    try:
        rendered = _render_workspace(tools_html='<form id="existing-actions"></form>')
    finally:
        reset_locale(token)
    parser = _WorkspaceParser()
    parser.feed(rendered)
    assert rendered.index('id="real-documents"') < rendered.index('id="existing-actions"')
    assert rendered.index('id="real-documents"') < rendered.index('id="knowledge-reference"')
    reference = next(item for item in parser.disclosures if item.get("id") == "knowledge-reference")
    assert "open" not in reference
    assert rendered.count('class="knowledge-metric"') == 4
    assert '<strong>40</strong>' in rendered
    assert '<strong>31</strong>' in rendered
    assert '<strong>9</strong>' in rendered
    assert '<strong>4</strong>' in rendered
    assert "200" in rendered
    assert "undefined" not in rendered
    assert "内部 SOP" not in rendered and "Internal SOPs" not in rendered
    assert "Protected values JSON" not in rendered
    assert "/static/knowledge-workspace.css" in rendered
    assert "/app/t/demo/knowledge-query?keyword=forex" in parser.links


def test_readonly_metrics_do_not_expose_tenant_totals_or_inaccessible_states():
    token = set_locale("en")
    try:
        rendered = _render_workspace(can_manage=False)
    finally:
        reset_locale(token)
    assert '<strong>40</strong>' not in rendered
    assert '<strong>31</strong>' not in rendered
    assert '<strong>9</strong>' not in rendered
    assert '<strong>4</strong>' not in rendered
    assert rendered.count('<strong>12</strong>') == 2
    assert "Outside your access" in rendered
    assert "100" in rendered
    assert '<form' in rendered  # GET search only, no mutation controls.
    assert 'method="post"' not in rendered
    assert "Protected values JSON" not in rendered
    assert "Not published" in rendered


def test_empty_loaded_scope_does_not_count_the_six_reference_templates():
    rendered = _render_workspace(
        counts={}, review_count=0, loaded_count=0, shown_count=0, table_html="No records"
    )
    metric_section = rendered.split('class="knowledge-scope-note"', 1)[0]
    assert metric_section.count('<strong>0</strong>') == 4
    assert '<strong>6</strong>' not in rendered
    assert rendered.count("data-financial-category=") == 6


def test_search_and_filter_values_are_escaped_and_preserved():
    attack = '\"><img src=x onerror=alert(1)>'
    rendered = _render_workspace(
        query=attack,
        hidden_filters=(("status_filter", "draft"), ("category", attack)),
    )
    assert '<img src=x' not in rendered
    assert '&lt;img' in rendered
    assert 'name="status_filter" value="draft"' in rendered
    assert 'name="search"' in rendered
    assert 'method="get"' in rendered


def test_search_is_bounded_and_matches_only_provided_visible_text():
    request = Request({"type": "http", "query_string": b"search=+FOREX+"})
    query = knowledge_read_view.knowledge_search_query(request)
    assert query == "FOREX"
    assert knowledge_read_view.knowledge_matches_search(query, "Forex spreads", None)
    assert not knowledge_read_view.knowledge_matches_search(query, "Protected content")
    assert knowledge_read_view.knowledge_matches_search("", None)
    oversized = Request({"type": "http", "query_string": b"search=" + b"x" * 201})
    with pytest.raises(HTTPException) as error:
        knowledge_read_view.knowledge_search_query(oversized)
    assert error.value.status_code == 422


def test_financial_topic_links_do_not_pretend_to_filter_document_categories():
    rendered = _render_workspace()
    parser = _WorkspaceParser()
    parser.feed(rendered)
    topic_links = [link for link in parser.links if "keyword=" in link]
    assert len(topic_links) == 12  # Six directory links plus the unchanged reference guide.
    assert all(link.startswith("/app/t/demo/knowledge-query?keyword=") for link in topic_links)
    assert not any("audience=" in link or "status_filter=internal" in link for link in parser.links)
