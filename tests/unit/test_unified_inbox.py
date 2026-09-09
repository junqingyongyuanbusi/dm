import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.dialects import postgresql

from social_reply.application.account_management import unified_inbox
from social_reply.application.account_management.ui_i18n import reset_locale, set_locale


def _principal(*capabilities: str):
    allowed = frozenset(capabilities)

    def require_capability(capability):
        if capability not in allowed:
            raise HTTPException(status_code=403, detail="capability_required")

    return SimpleNamespace(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        actor="user:reader",
        username="reader",
        role="USER",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        is_workspace_admin=False,
        is_admin=False,
        is_superadmin=False,
        has_capability=lambda capability: capability in allowed,
        require_capability=require_capability,
    )


def _entry(principal, *, state="BOT_ACTIVE", work_status=None, owned=True):
    conversation_id = uuid.uuid4()
    work = (
        None
        if work_status is None
        else SimpleNamespace(
            id=uuid.uuid4(),
            status=work_status,
            version=7,
            assigned_actor=principal.actor if owned else "user:someone-else",
            assigned_user_id=principal.user_id if owned else uuid.uuid4(),
            assigned_session_id=None,
        )
    )
    return unified_inbox.ConversationEntry(
        conversation=SimpleNamespace(
            id=conversation_id,
            platform="telegram",
            channel_type="dm",
            created_at=datetime.now(UTC),
        ),
        contact=SimpleNamespace(display_name="<script>alert(1)</script>", external_user_id="123"),
        account=SimpleNamespace(name="Support", automation_default="BOT_ACTIVE"),
        latest_message=SimpleNamespace(
            id=uuid.uuid4(),
            direction="inbound",
            sender_type="customer",
            text="Need help",
            occurred_at=None,
            created_at=datetime.now(UTC),
        ),
        state=state,
        work_item=work,
    )


def _sql(statement):
    return str(
        statement.compile(
            dialect=postgresql.dialect(paramstyle="named"),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_conversation_query_scopes_every_tenant_relation_and_limits_page():
    principal = _principal("inbox.read")
    statement = unified_inbox.build_conversation_query(
        principal,
        "default",
        queue="all",
        search="refund",
        platform="telegram",
        channel_type="dm",
        page=2,
    )
    query = _sql(statement)
    assert "platform_accounts.owner_user_id" in query
    assert str(principal.user_id) in query
    assert "account_access_grants.platform_account_id = platform_accounts.id" in query
    assert "account_access_grants.active IS true" in query
    assert "contacts.tenant_id = 'default'" in query
    assert "contacts.platform_account_id = conversations.platform_account_id" in query
    assert "human_work_items.tenant_id = 'default'" in query
    assert "conversations.tenant_id = 'default'" in query
    assert "conversations.platform = 'telegram'" in query
    assert "conversations.channel_type = 'dm'" in query
    assert "ILIKE" in query
    assert "LIMIT 100 OFFSET 100" in query
    assert "history_seq DESC" in query


@pytest.mark.parametrize(
    "queue,expected",
    [
        ("ai", "'BOT_ACTIVE', 'BOT_DRAFT_ONLY'"),
        ("human", "'HUMAN_ACTIVE', 'HANDOFF_PENDING'"),
        ("resolved", "'CLOSED'"),
        ("mine", "human_work_items.assigned_actor = 'user:reader'"),
    ],
)
def test_queues_are_filtered_in_database(queue, expected):
    query = _sql(unified_inbox.build_conversation_query(_principal(), "default", queue=queue))
    assert expected in query


def test_search_wildcards_are_literal_and_selection_remains_scoped():
    selected_id = uuid.uuid4()
    query = _sql(
        unified_inbox.build_conversation_query(
            _principal(),
            "default",
            search="50%_off",
            selected_id=selected_id,
        )
    )
    assert "50/%/_off" in query
    assert str(selected_id) in query
    assert "platform_accounts.owner_user_id" in query
    assert "human_work_items.id" in query


def test_no_account_access_produces_fail_closed_query():
    query = _sql(unified_inbox.build_conversation_query(_principal(), "other-tenant"))
    assert "WHERE false" in query


def test_resolved_query_uses_completed_work_and_latest_stored_inbound_time():
    query = _sql(unified_inbox.build_conversation_query(_principal(), "default", queue="resolved"))
    assert "max(resolved_work.resolved_at)" in query
    assert "resolved_work.status = 'RESOLVED'" in query
    assert "resolved_work.tenant_id = 'default'" in query
    assert "max(messages.created_at)" in query
    assert "messages.direction = 'inbound'" in query
    assert "human_work_items.id IS NULL" in query
    assert "occurred_at)" not in query


@pytest.mark.parametrize(
    "locale,notice",
    [
        ("zh-CN", "人工处理已完成；自动化状态独立显示"),
        ("en", "Human handling completed; automation state is shown separately"),
    ],
)
def test_resolved_inspector_does_not_claim_restored_bot_is_closed(locale, notice):
    entry = _entry(_principal(), state="BOT_DRAFT_ONLY")
    token = set_locale(locale)
    try:
        inspector = unified_inbox._inspector(entry, queue="resolved")
    finally:
        reset_locale(token)
    assert notice in inspector
    assert 'title="BOT_DRAFT_ONLY"' in inspector
    assert 'title="CLOSED"' not in inspector


def test_mine_queue_requires_claimed_assignment_identity_not_just_actor():
    principal = _principal("inbox.read")
    query = _sql(unified_inbox.build_conversation_query(principal, "default", queue="mine"))
    assert "human_work_items.status = 'CLAIMED'" in query
    assert f"human_work_items.assigned_user_id = '{principal.user_id}'" in query
    assert "human_work_items.assigned_session_id =" not in query


def test_mine_queue_separates_bootstrap_sessions_sharing_an_actor():
    principal = _principal("inbox.read")
    bootstrap = SimpleNamespace(
        **{
            **vars(principal),
            "user_id": None,
            "is_superadmin": True,
            "is_workspace_admin": True,
        }
    )
    query = _sql(unified_inbox.build_conversation_query(bootstrap, "default", queue="mine"))
    assert f"human_work_items.assigned_session_id = '{bootstrap.session_id}'" in query
    assert "human_work_items.assigned_user_id IS NULL" in query


@pytest.mark.parametrize("capabilities", [(), ("inbox.read",)])
def test_readers_never_receive_mutation_forms(capabilities):
    principal = _principal(*capabilities)
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        entry,
        [entry.latest_message],
        csrf_token="csrf",
    )
    assert "<form" not in actions
    assert "<textarea" not in actions


def test_other_assignee_cannot_reply_even_with_capability():
    principal = _principal("reply", "takeover")
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED", owned=False)
    actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        entry,
        [entry.latest_message],
        csrf_token="csrf",
    )
    assert '/reply"' not in actions
    assert 'name="text"' not in actions


def test_owner_reply_reuses_csrf_version_and_idempotency_contract():
    principal = _principal("reply")
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        entry,
        [entry.latest_message],
        csrf_token='csrf"token',
    )
    assert f'/conversations/{entry.conversation.id}/reply"' in actions
    assert 'name="csrf_token" value="csrf&amp;' not in actions
    assert 'value="csrf&quot;token"' in actions
    assert 'name="expected_version" value="7"' in actions
    assert f'name="work_item_id" value="{entry.work_item.id}"' in actions
    assert 'name="idempotency_key"' in actions
    assert f'name="reply_to_message_id" value="{entry.latest_message.id}"' in actions


def test_waiting_claim_and_ai_reception_are_capability_gated():
    principal = _principal("takeover")
    waiting = _entry(principal, state="HANDOFF_PENDING", work_status="WAITING")
    waiting_actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        waiting,
        [],
        csrf_token="csrf",
    )
    assert f'/work-items/{waiting.work_item.id}/claim"' in waiting_actions
    assert 'name="expected_version" value="7"' in waiting_actions
    active = _entry(principal)
    active_actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        active,
        [],
        csrf_token="csrf",
    )
    assert '/start-reception"' in active_actions


def test_pending_send_suppresses_reply_and_resolve():
    principal = _principal("reply", "takeover")
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        entry,
        [entry.latest_message],
        csrf_token="csrf",
        pending_send=True,
    )
    assert '/reply"' not in actions
    assert '/resolve"' not in actions


def test_localized_shell_escapes_contact_and_preserves_filters():
    principal = _principal("inbox.read")
    entry = _entry(principal)
    locale_token = set_locale("en")
    try:
        body = unified_inbox.render_inbox_body(
            tenant_id="default",
            principal=principal,
            entries=[entry],
            selected=entry,
            messages=[],
            queue="all",
            search="a&b",
            platform="telegram",
            channel_type="dm",
            page=2,
            total=201,
            csrf_token="csrf",
            has_selection=True,
            pending_send=False,
        )
    finally:
        reset_locale(locale_token)
    assert "All conversations" in body
    assert "Contact details" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "q=a%26b" in body
    assert "platform=telegram" in body
    assert 'data-has-selection="true"' in body
    assert 'class="unified-inbox-inspector"' in body


def test_workspace_renders_real_thread_and_profile_without_demo_controls():
    principal = _principal("inbox.read", "reply", "takeover")
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    token = set_locale("zh-CN")
    try:
        body = unified_inbox.render_inbox_body(
            tenant_id="default",
            principal=principal,
            entries=[entry],
            selected=entry,
            messages=[entry.latest_message],
            queue="mine",
            search="Need",
            platform="telegram",
            channel_type="dm",
            page=1,
            total=1,
            csrf_token="real-csrf-token",
            has_selection=True,
            pending_send=False,
        )
    finally:
        reset_locale(token)
    assert "wikiglobal" in body
    assert 'class="unified-inbox-profile"' in body
    assert 'aria-label="会话内容"' in body
    assert 'name="q" value="Need"' in body
    assert "Need help" in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert '<script>alert(1)</script>' not in body
    assert 'name="csrf_token" value="real-csrf-token"' in body
    assert 'name="expected_version" value="7"' in body
    assert 'name="idempotency_key"' in body
    assert body.count('id="unified-reply"') == 1
    assert body.count(f'/work-items/{entry.work_item.id}/resolve"') == 1
    assert body.index('/resolve"') < body.index('class="unified-inbox-thread"')
    assert 'action="/app/t/default/conversations/' in body
    assert "模拟已送达" not in body
    assert "使用建议" not in body
    assert "保存备注" not in body
    assert 'type="file"' not in body


@pytest.mark.parametrize(
    "capabilities,pending_send",
    [(("inbox.read",), False), (("reply", "takeover"), True)],
)
def test_workspace_pending_send_and_readonly_remain_non_mutating(capabilities, pending_send):
    principal = _principal(*capabilities)
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    body = unified_inbox.render_inbox_body(
        tenant_id="default",
        principal=principal,
        entries=[entry],
        selected=entry,
        messages=[entry.latest_message],
        queue="all",
        search="",
        platform="",
        channel_type="",
        page=1,
        total=1,
        csrf_token="csrf",
        has_selection=True,
        pending_send=pending_send,
    )
    assert 'method="post"' not in body
    assert "<textarea" not in body
    assert 'method="get"' in body
    assert f'href="/app/t/default/conversations/{entry.conversation.id}"' in body


async def test_inbox_capability_is_required_before_database_access(monkeypatch):
    principal = _principal()
    monkeypatch.setattr(
        unified_inbox.saas_console, "_require_tenant_principal", AsyncMock(return_value=principal)
    )
    database_factory = AsyncMock(side_effect=AssertionError("must not access database"))
    monkeypatch.setattr(unified_inbox, "get_session_factory", database_factory)
    application = FastAPI()
    application.include_router(unified_inbox.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/app/t/default/inbox")
    assert response.status_code == 403
    database_factory.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "queue=unknown",
        "platform=unknown",
        "item_id=bad-uuid",
        "page=0",
        "page=10001",
        "q=" + "a" * 201,
    ],
)
async def test_invalid_parameters_are_rejected_without_database_access(monkeypatch, query):
    principal = _principal("inbox.read")
    monkeypatch.setattr(
        unified_inbox.saas_console, "_require_tenant_principal", AsyncMock(return_value=principal)
    )
    database_factory = Mock(side_effect=AssertionError("must not access database"))
    monkeypatch.setattr(unified_inbox, "get_session_factory", database_factory)
    application = FastAPI()
    application.include_router(unified_inbox.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/app/t/default/inbox?{query}")
    assert response.status_code == 422
    database_factory.assert_not_called()


async def test_missing_selection_returns_404_instead_of_showing_another_conversation():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=1),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: []),
                SimpleNamespace(one_or_none=lambda: None),
            ]
        ),
    )
    with pytest.raises(HTTPException) as raised:
        await unified_inbox._load_workspace(
            session,
            _principal("inbox.read"),
            "default",
            selected_id=uuid.uuid4(),
            queue="all",
            search="",
            platform="",
            channel_type="",
            page=1,
        )
    assert raised.value.status_code == 404


async def test_workspace_count_query_and_history_are_scoped_and_chronological():
    principal = _principal("inbox.read")
    entry = _entry(principal)
    older_message = SimpleNamespace(text="older")
    newer_message = SimpleNamespace(text="newer")
    row = (
        entry.conversation,
        entry.contact,
        entry.account,
        entry.latest_message,
        entry.state,
        None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=201),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [row])),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [newer_message, older_message])),
    )
    entries, selected, messages, total, pending = await unified_inbox._load_workspace(
        session,
        principal,
        "default",
        selected_id=None,
        queue="all",
        search="Need",
        platform="telegram",
        channel_type="dm",
        page=2,
    )
    assert entries == [entry]
    assert selected == entry
    assert messages == [older_message, newer_message]
    assert total == 201
    assert pending is False
    count_query = _sql(session.scalar.call_args.args[0])
    assert "SELECT count(*)" in count_query
    assert "LIMIT 100" not in count_query
    assert "latest_message.text ILIKE" in count_query
    assert "account_access_grants" in count_query
    message_query = _sql(session.scalars.call_args.args[0])
    assert "conversations.tenant_id = 'default'" in message_query
    assert "account_access_grants" in message_query
    assert "messages.history_seq DESC" in message_query
    assert "LIMIT 100" in message_query


async def test_default_queue_renders_empty_shell_and_sets_matching_csrf_cookie(monkeypatch):
    principal = _principal("inbox.read")
    monkeypatch.setattr(
        unified_inbox.saas_console, "_require_tenant_principal", AsyncMock(return_value=principal)
    )
    session = AsyncMock()
    monkeypatch.setattr(unified_inbox, "get_session_factory", lambda: lambda: session)
    load_workspace = AsyncMock(return_value=([], None, [], 0, False))
    monkeypatch.setattr(unified_inbox, "_load_workspace", load_workspace)
    monkeypatch.setattr(
        unified_inbox.saas_console, "_render_page", lambda **context: HTMLResponse(context["body"])
    )
    application = FastAPI()
    application.include_router(unified_inbox.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/app/t/default/inbox")
    assert response.status_code == 200
    assert 'class="unified-inbox"' in response.text
    assert 'data-has-selection="false"' in response.text
    assert "reply_admin_csrf=" in response.headers["set-cookie"]
    assert load_workspace.call_args.kwargs["queue"] == "all"


async def test_authentication_redirect_is_preserved(monkeypatch):
    redirect = RedirectResponse("/auth/login?next=%2Fapp%2Ft%2Fdefault%2Finbox", status_code=303)
    monkeypatch.setattr(
        unified_inbox.saas_console, "_require_tenant_principal", AsyncMock(return_value=redirect)
    )
    response = await unified_inbox.tenant_unified_inbox(Mock(), "default")
    assert response is redirect


@pytest.mark.parametrize("queue", ["drafts", "delivery"])
async def test_legacy_queues_keep_existing_workflow_but_deny_readers(monkeypatch, queue):
    principal = _principal("inbox.read")
    require_principal = AsyncMock(return_value=principal)
    monkeypatch.setattr(unified_inbox.saas_console, "_require_tenant_principal", require_principal)
    legacy = AsyncMock(return_value=HTMLResponse("legacy"))
    monkeypatch.setattr(unified_inbox.saas_console, "tenant_inbox", legacy)
    with pytest.raises(HTTPException) as raised:
        await unified_inbox.tenant_unified_inbox(Mock(), "default", queue=queue)
    assert raised.value.status_code == 403
    legacy.assert_not_called()
    admin = _principal("inbox.read", "reply")
    require_principal.return_value = SimpleNamespace(**{**vars(admin), "is_admin": True})
    response = await unified_inbox.tenant_unified_inbox(Mock(), "default", queue=queue)
    assert response.body == b"legacy"
    assert legacy.call_args.kwargs["queue"] == queue


@pytest.mark.parametrize("role", ["VIEWER", "OPERATOR"])
def test_real_default_readonly_roles_have_no_action_forms(role):
    principal = unified_inbox.Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        username="reader",
        actor="user:reader",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role=role,
    )
    assert principal.has_capability("inbox.read")
    entry = _entry(principal, state="HUMAN_ACTIVE", work_status="CLAIMED")
    actions = unified_inbox.render_conversation_actions(
        "default",
        principal,
        entry,
        [entry.latest_message],
        csrf_token="csrf",
    )
    assert "<form" not in actions
