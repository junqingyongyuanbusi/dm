from dataclasses import FrozenInstanceError, replace
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from social_reply.application.account_management import financial_knowledge
from social_reply.application.account_management.templating import trusted_html
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_guide_has_six_public_templates_and_review_metadata(locale):
    token = set_locale(locale)
    try:
        rendered = financial_knowledge.render_financial_knowledge_guide("/app/t/demo", True)
    finally:
        reset_locale(token)

    assert "wikiglobal" in rendered
    assert rendered.count("data-financial-category=") == 6
    assert "<form" not in rendered
    assert "<script" not in rendered
    assert len(financial_knowledge.FINANCIAL_KNOWLEDGE_TEMPLATES) == 6
    for template in financial_knowledge.FINANCIAL_KNOWLEDGE_TEMPLATES:
        content = template.en if locale == "en" else template.zh
        assert content.title in rendered
        assert content.source_hint in rendered
        assert content.verified_at_hint in rendered
        assert content.required_information in rendered
        assert template.en.question and template.en.reply
    assert "wp-knowledge-safety" in rendered
    assert "Internal SOPs" not in rendered and "内部 SOP" not in rendered
    assert "Protected values JSON" not in rendered


def test_category_links_use_only_fixed_public_keywords():
    rendered = financial_knowledge.render_financial_knowledge_guide("/app/t/demo", False)
    links = [part.split('"', 1)[0] for part in rendered.split('href="')[1:]]
    assert len(links) == 6
    for template, link in zip(
        financial_knowledge.FINANCIAL_KNOWLEDGE_TEMPLATES, links, strict=True
    ):
        parsed = urlsplit(unescape(link))
        assert parsed.path == "/app/t/demo/knowledge-query"
        assert parse_qs(parsed.query)["keyword"] == [template.keyword]
        assert "q=" not in parsed.query


def test_reference_keeps_one_risk_notice_without_internal_publication_instructions():
    token = set_locale("en")
    try:
        readonly = financial_knowledge.render_financial_knowledge_guide("/app/t/demo", False)
        manager = financial_knowledge.render_financial_knowledge_guide("/app/t/demo", True)
    finally:
        reset_locale(token)
    assert "Verify sources before publication." in readonly
    assert "investing involves risk." in manager
    assert "Protected values JSON" not in manager
    assert "Protected values JSON" not in readonly
    assert "Not published" in readonly and "Not published" in manager
    assert "language_verified" not in manager
    assert '<form' not in manager and '<form' not in readonly


@pytest.mark.parametrize(
    "root",
    [
        "https://evil.example",
        "//evil.example",
        "/app/t/demo?x=1",
        "/app/t/../admin",
        '/app/t/demo" onclick="alert(1)',
        "/app/t/demo#x",
        "/app/t/demo%2fadmin",
    ],
)
def test_guide_rejects_unsafe_tenant_roots(root):
    with pytest.raises(ValueError, match="tenant root"):
        financial_knowledge.render_financial_knowledge_guide(root, True)


def test_plain_draft_content_is_escaped_before_trusted_html(monkeypatch):
    template = financial_knowledge.FINANCIAL_KNOWLEDGE_TEMPLATES[0]
    unsafe = '<img src=x onerror="alert(1)">'
    modified = replace(template, en=replace(template.en, reply=unsafe))
    monkeypatch.setattr(financial_knowledge, "FINANCIAL_KNOWLEDGE_TEMPLATES", (modified,))
    token = set_locale("en")
    try:
        rendered = financial_knowledge.render_financial_knowledge_guide("/app/t/demo", True)
    finally:
        reset_locale(token)
    assert unsafe not in rendered
    assert "&lt;img" in rendered
    assert "&lt;img" in trusted_html(rendered)


def test_template_constants_are_immutable():
    template = financial_knowledge.FINANCIAL_KNOWLEDGE_TEMPLATES[0]
    with pytest.raises(FrozenInstanceError):
        template.keyword = "changed"
