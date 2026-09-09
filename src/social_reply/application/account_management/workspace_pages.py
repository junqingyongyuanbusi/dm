"""Tenant business pages. Include ``router`` from the API composition root."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import Date, cast, func, text
from sqlalchemy.exc import DBAPIError
from starlette.responses import HTMLResponse, Response

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.saas_ui import render_saas_page
from social_reply.application.account_management.templating import render_template
from social_reply.application.account_management.ui_i18n import get_locale
from social_reply.application.account_management.workspace_i18n import (
    workspace_status,
    workspace_text,
)
from social_reply.application.account_management.workspace_queries import (
    CONTACT_MAX_PAGE,
    CONTACT_PAGE_SIZE,
    agent_choices_statement,
    contact_conversations_statement,
    contact_list_statement,
    report_statements,
)
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.database.models import Message

router = APIRouter(tags=["tenant-workspace"])
_PAGE_NAMES = frozenset({"contacts", "reports", "flows", "playground"})
_FLOW_STAGES = ("state", "rules", "hard", "grounding", "language", "delivery")


def _tenant_root(tenant_id: str) -> str:
    return f"/app/t/{quote(tenant_id, safe='')}"


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return workspace_text("common.unknown")
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


async def _require_workspace_principal(
    request: Request,
    tenant_id: str,
    capability: str,
) -> Principal | Response:
    # Local import keeps the new router independent of the console's composition order.
    from social_reply.application.account_management.saas_console import _require_tenant_principal

    principal = await _require_tenant_principal(request, tenant_id)
    if not isinstance(principal, Response):
        principal.require_capability(capability)
    return principal


def render_workspace_page(
    *,
    principal: Principal,
    tenant_id: str,
    page: str,
    **context: object,
) -> HTMLResponse:
    if page not in _PAGE_NAMES:
        raise ValueError("workspace_page_invalid")
    body = render_template(
        f"tenant/{page}.html",
        english=get_locale() == "en",
        workspace_text=workspace_text,
        workspace_status=workspace_status,
        timestamp=_timestamp,
        tenant_root=_tenant_root(tenant_id),
        flow_stages=_FLOW_STAGES,
        **context,
    )
    return HTMLResponse(
        render_saas_page(
            principal=principal,
            tenant_id=tenant_id,
            title=workspace_text(f"{page}.title"),
            description=workspace_text(f"{page}.description"),
            body=body,
            active_navigation=page,
        ),
        headers={"Cache-Control": "no-store"},
    )


def _contact_view(tenant_id: str, contact) -> dict[str, object]:
    return {
        **dict(contact),
        "href": f"{_tenant_root(tenant_id)}/contacts/{contact['id']}",
    }


@router.get("/app/t/{tenant_id}/contacts", response_class=HTMLResponse)
async def workspace_contacts(
    request: Request,
    tenant_id: str,
    search: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1, le=CONTACT_MAX_PAGE)] = 1,
) -> Response:
    principal = await _require_workspace_principal(request, tenant_id, "contacts.read")
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        contacts = (
            (
                await session.execute(
                    contact_list_statement(
                        principal,
                        tenant_id,
                        search=search.strip(),
                        page=page,
                    )
                )
            )
            .mappings()
            .all()
        )
    root = f"{_tenant_root(tenant_id)}/contacts"
    previous_href = f"{root}?{urlencode({'search': search, 'page': page - 1})}" if page > 1 else ""
    next_href = (
        f"{root}?{urlencode({'search': search, 'page': page + 1})}"
        if len(contacts) > CONTACT_PAGE_SIZE and page < CONTACT_MAX_PAGE
        else ""
    )
    return render_workspace_page(
        principal=principal,
        tenant_id=tenant_id,
        page="contacts",
        search=search,
        contacts=tuple(
            _contact_view(tenant_id, contact) for contact in contacts[:CONTACT_PAGE_SIZE]
        ),
        selected=None,
        conversations=(),
        previous_href=previous_href,
        next_href=next_href,
    )


@router.get("/app/t/{tenant_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def workspace_contact_detail(
    request: Request,
    tenant_id: str,
    contact_id: UUID,
) -> Response:
    principal = await _require_workspace_principal(request, tenant_id, "contacts.read")
    if isinstance(principal, Response):
        return principal
    async with get_session_factory()() as session:
        selected = (
            (
                await session.execute(
                    contact_list_statement(
                        principal,
                        tenant_id,
                        search="",
                        page=1,
                        contact_id=contact_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="contact_not_found")
        conversations = (
            (
                await session.execute(
                    contact_conversations_statement(
                        principal,
                        tenant_id,
                        contact_id,
                    )
                )
            )
            .mappings()
            .all()
        )
    return render_workspace_page(
        principal=principal,
        tenant_id=tenant_id,
        page="contacts",
        search="",
        contacts=(),
        selected=_contact_view(tenant_id, selected),
        conversations=tuple(conversations),
        previous_href="",
        next_href="",
    )


async def _load_report(principal: Principal, tenant_id: str, days: int, end: datetime) -> dict:
    statements = report_statements(principal, tenant_id, days=days, end=end)
    message_day = cast(func.timezone("UTC", Message.created_at), Date)
    # Reuse the exact message window and account scope used by the metric cards.
    trend_statement = (
        statements["messages"]
        .with_only_columns(
            message_day.label("day"),
            func.count().filter(Message.direction == "inbound").label("inbound"),
            func.count().filter(Message.direction == "outbound").label("outbound"),
        )
        .group_by(None)
        .order_by(None)
        .group_by(message_day)
        .order_by(message_day)
    )
    async with get_session_factory()() as session:
        # Cap each PostgreSQL aggregate without persisting a database setting.
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        platforms = tuple((await session.execute(statements["messages"])).mappings().all())
        new_conversations = int((await session.execute(statements["conversations"])).scalar_one())
        trend_rows = tuple((await session.execute(trend_statement)).mappings().all())
    totals = {
        key: sum(int(platform[key]) for platform in platforms)
        for key in ("inbound", "outbound", "active_conversations")
    }
    first_day = (end - timedelta(days=days)).date()
    last_day = (end - timedelta(microseconds=1)).date()
    counts_by_day = {row["day"]: row for row in trend_rows}
    trend = tuple(
        {"day": day, "inbound": 0, "outbound": 0, **counts_by_day.get(day, {})}
        for day in (
            first_day + timedelta(days=offset)
            for offset in range((last_day - first_day).days + 1)
        )
    )
    return {
        "platforms": platforms,
        "trend": trend,
        "trend_max": max(1, *(max(row["inbound"], row["outbound"]) for row in trend)),
        "metrics": tuple(
            {"label": workspace_text(f"reports.{key}"), "value": value}
            for key, value in {**totals, "new_conversations": new_conversations}.items()
        ),
    }


@router.get("/app/t/{tenant_id}/reports", response_class=HTMLResponse)
async def workspace_reports(
    request: Request,
    tenant_id: str,
    days: Annotated[int, Query(ge=7, le=30)] = 7,
) -> Response:
    principal = await _require_workspace_principal(request, tenant_id, "reports.read")
    if isinstance(principal, Response):
        return principal
    if days not in (7, 30):
        raise HTTPException(status_code=422, detail="report_window_invalid")
    end = datetime.now(UTC)
    try:
        report = await _load_report(principal, tenant_id, days, end)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "57014":
            raise
        raise HTTPException(status_code=503, detail="workspace_report_timeout") from error
    return render_workspace_page(
        principal=principal,
        tenant_id=tenant_id,
        page="reports",
        days=days,
        start=end - timedelta(days=days),
        end=end,
        **report,
    )


async def _load_agent_choices(principal: Principal, tenant_id: str) -> tuple[dict, ...]:
    async with get_session_factory()() as session:
        agents = (
            (await session.execute(agent_choices_statement(principal, tenant_id))).mappings().all()
        )
    root = _tenant_root(tenant_id)
    return tuple(
        {
            "brand_id": agent["brand_id"],
            "name": agent["name"],
            "href": f"{root}/agents/{quote(agent['brand_id'], safe='')}",
            "test_href": f"{root}/agents/{quote(agent['brand_id'], safe='')}/test",
        }
        for agent in agents
    )


@router.get("/app/t/{tenant_id}/flows", response_class=HTMLResponse)
async def workspace_flows(request: Request, tenant_id: str) -> Response:
    principal = await _require_workspace_principal(request, tenant_id, "flows.read")
    if isinstance(principal, Response):
        return principal
    return render_workspace_page(
        principal=principal,
        tenant_id=tenant_id,
        page="flows",
        agents=await _load_agent_choices(principal, tenant_id),
    )


@router.get("/app/t/{tenant_id}/playground", response_class=HTMLResponse)
async def workspace_playground(
    request: Request,
    tenant_id: str,
    agent_id: Annotated[str, Query(max_length=128)] = "",
) -> Response:
    principal = await _require_workspace_principal(request, tenant_id, "playground.read")
    if isinstance(principal, Response):
        return principal
    from social_reply.application.account_management.admin import _csrf, _ensure_csrf

    agents = await _load_agent_choices(principal, tenant_id)
    selected = agents[0] if agents else None
    if agent_id:
        selected = next((agent for agent in agents if agent["brand_id"] == agent_id), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="agent_not_found")
    csrf_token = _csrf(request)
    response = render_workspace_page(
        principal=principal,
        tenant_id=tenant_id,
        page="playground",
        agents=agents,
        selected=selected,
        csrf_token=csrf_token,
    )
    return _ensure_csrf(response, request, csrf_token)
