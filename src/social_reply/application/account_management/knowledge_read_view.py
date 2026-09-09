"""Published, account-scoped knowledge for support and operations staff."""

from collections.abc import Mapping
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from social_reply.application.account_management.access import account_read_condition
from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.financial_knowledge import (
    FINANCIAL_KNOWLEDGE_TEMPLATES,
    render_financial_knowledge_guide,
)
from social_reply.application.account_management.saas_ui import (
    empty_state,
    escape,
    format_datetime,
    render_saas_page,
)
from social_reply.application.account_management.templating import render_template, trusted_html
from social_reply.application.account_management.ui_i18n import get_locale
from social_reply.domain.reply.guard import has_contact_like
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory


def knowledge_search_query(request: Request) -> str:
    """Validate the display-only search; persistence queries retain their existing bounds."""
    query = request.query_params.get("search", "").strip()
    if len(query) > 200:
        raise HTTPException(422, detail="knowledge_search_too_long")
    return query


def knowledge_matches_search(query: str, *visible_values: object) -> bool:
    """Callers must pass only authorized, redacted display values, never hidden content."""
    return not query or any(
        query.casefold() in str(value or "").casefold() for value in visible_values
    )


def render_knowledge_workspace(
    *,
    root: str,
    can_manage: bool,
    counts: Mapping[str, int],
    review_count: int,
    loaded_count: int,
    shown_count: int,
    table_html: str,
    tabs_html: str = "",
    filters_html: str = "",
    tools_html: str = "",
    review_html: str = "",
    query: str = "",
    hidden_filters: tuple[tuple[str, str], ...] = (),
) -> str:
    """Compose trusted view fragments without loading data or granting write access."""
    english = get_locale() == "en"
    guide_html = render_financial_knowledge_guide(root, can_manage=can_manage)
    metrics = (
        ("total", sum(counts.values()) if can_manage else loaded_count),
        ("published", counts.get("published", 0) if can_manage else loaded_count),
        ("draft", counts.get("draft", 0) if can_manage else None),
        ("review", review_count if can_manage else None),
    )
    topics = tuple(
        {
            "title": (template.en if english else template.zh).title,
            "href": f"{root}/knowledge-query?{urlencode({'keyword': template.keyword})}",
        }
        for template in FINANCIAL_KNOWLEDGE_TEMPLATES
    )
    return render_template(
        "tenant/knowledge_workspace.html",
        root=root, english=english, can_manage=can_manage, metrics=metrics, topics=topics,
        loaded_count=loaded_count, shown_count=shown_count, limit=200 if can_manage else 100,
        query=query, hidden_filters=hidden_filters,
        table_html=trusted_html(table_html), tabs_html=trusted_html(tabs_html),
        filters_html=trusted_html(filters_html), tools_html=trusted_html(tools_html),
        review_html=trusted_html(review_html), guide_html=trusted_html(guide_html),
    )


async def render_published_knowledge(
    request: Request, principal: Principal, tenant_id: str
) -> HTMLResponse:
    from social_reply.application.account_management.saas_console import (
        _knowledge_document_is_sensitive,
    )

    principal.require_tenant(tenant_id)
    principal.require_capability("knowledge.read")
    english = get_locale() == "en"
    root = f"/app/t/{tenant_id}"
    category = request.query_params.get("category", "").strip()
    if len(category) > 64:
        raise HTTPException(422, detail="knowledge_category_too_long")
    visible_brands = select(models.PlatformAccount.brand_id).where(
        account_read_condition(principal, tenant_id)
    )
    statement = select(models.KnowledgeDocument).where(
        models.KnowledgeDocument.tenant_id == tenant_id,
        models.KnowledgeDocument.status == "published",
        models.KnowledgeDocument.brand_id.in_(visible_brands),
        models.KnowledgeDocument.is_official_contact.is_(False),
    )
    if category:
        statement = statement.where(models.KnowledgeDocument.category == category)
    async with get_session_factory()() as session:
        documents = list(
            await session.scalars(
                statement.order_by(models.KnowledgeDocument.updated_at.desc()).limit(100)
            )
        )
    safe_documents = [
        document
        for document in documents
        if not _knowledge_document_is_sensitive(document)
        and not any(
            has_contact_like(str(value or ""))
            for value in (
                document.brand_id,
                document.category,
                document.source_file,
                document.platform,
            )
        )
    ]
    query = knowledge_search_query(request)
    matching_documents = [
        document for document in safe_documents
        if knowledge_matches_search(
            query, document.question, document.reply, document.category, document.brand_id
        )
    ]
    answer_label = "Read answer" if english else "查看答案"
    published_label = "Published" if english else "已发布"
    records = "".join(
        f'<tr><td><strong>{escape(document.question)}</strong>'
        f'<p class="saas-muted">{escape(document.category or "FAQ")}</p>'
        f'<details class="knowledge-answer"><summary>{answer_label}</summary>'
        f'<p class="saas-knowledge-answer">{escape(document.reply)}</p></details></td>'
        f'<td>{escape(document.brand_id)}</td><td><span class="saas-status success">'
        f'{published_label}</span></td><td>{format_datetime(document.updated_at, include_year=True)}'
        '</td></tr>'
        for document in matching_documents
    )
    if records:
        headings = ("Knowledge / topic", "Brand scope", "Status", "Updated") if english else (
            "知识 / 主题", "品牌范围", "状态", "更新于"
        )
        header_html = "".join(f'<th scope="col">{heading}</th>' for heading in headings)
        records = (
            '<div class="saas-table-wrap"><table class="saas-table"><thead><tr>'
            f'{header_html}</tr></thead><tbody>{records}</tbody></table></div>'
        )
    elif query:
        records = empty_state(
            "No matching knowledge" if english else "未找到匹配知识",
            "Try another keyword in the currently loaded, accessible records."
            if english else "请尝试其他关键词；仅搜索本次已加载且有权访问的资料。",
        )
    else:
        records = empty_state(
            "No published knowledge in your scope" if english else "暂无可见的已发布知识",
            "Ask an administrator to review and publish knowledge for your assigned accounts."
            if english
            else "请管理员为你获分配的账号审核并发布知识；草稿及受保护内容不会显示。",
        )
    title = "Knowledge base" if english else "知识库"
    description = (
        "Verified answers for FX and financial customer support. Read-only access."
        if english
        else "外汇与金融客服的已发布知识。当前为只读访问，知识更新由管理员审核。"
    )
    content = render_knowledge_workspace(
        root=root, can_manage=False, counts={}, review_count=0,
        loaded_count=len(safe_documents), shown_count=len(matching_documents),
        table_html=records, query=query,
        hidden_filters=(("category", category),) if category else (),
    )
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            tenant_id=tenant_id,
            title=title,
            description=description,
            body=content,
            active_navigation="knowledge",
        ),
        headers={"Cache-Control": "no-store"},
    )
