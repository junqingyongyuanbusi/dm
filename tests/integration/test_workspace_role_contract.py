import uuid

import httpx
import pytest
from sqlalchemy import select
from tests.integration.company_permission_support import (
    create_staff,
    login_client,
    seed_conversation,
)

from apps.api.main import create_app
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration
ROOT = "/app/t/default"
BUSINESS_PAGES = (
    "inbox",
    "contacts",
    "agents",
    "flows",
    "knowledge",
    "playground",
    "channels",
    "reports",
    "audit",
    "settings",
)


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    ("role", "allowed_pages"),
    [
        ("WORKSPACE_ADMIN", set(BUSINESS_PAGES)),
        ("MANAGER", set(BUSINESS_PAGES) - {"audit", "settings"}),
        ("OPERATOR", {"inbox", "channels"}),
        ("AGENT", {"inbox", "contacts", "knowledge"}),
        ("VIEWER", {"inbox", "reports", "audit"}),
    ],
)
async def test_real_login_enforces_page_matrix_and_member_administration(
    session, role, allowed_pages
):
    staff = await create_staff(session, role=role)
    async with client() as browser:
        await login_client(browser, username=staff.username, password=staff.password)
        for page in BUSINESS_PAGES:
            response = await browser.get(f"{ROOT}/{page}")
            assert response.status_code == (200 if page in allowed_pages else 403), (
                role,
                page,
                response.status_code,
                response.text[:300],
            )
        members = await browser.get("/admin/users")
        assert members.status_code == (200 if role == "WORKSPACE_ADMIN" else 403)
        home = await browser.get(ROOT)
        assert home.status_code == (200 if role in {"MANAGER", "WORKSPACE_ADMIN"} else 303)


@pytest.mark.parametrize("role", ["OPERATOR", "VIEWER"])
async def test_read_only_roles_cannot_post_replies_or_takeover_owned_conversations(session, role):
    staff = await create_staff(session, role=role)
    conversation = await seed_conversation(session, owner_user_id=staff.user_id)
    async with client() as browser:
        csrf = await login_client(browser, username=staff.username, password=staff.password)
        inbox = await browser.get(f"{ROOT}/inbox?item_id={conversation.conversation_id}")
        assert inbox.status_code == 200
        assert (
            f'action="{ROOT}/conversations/{conversation.conversation_id}/reply"' not in inbox.text
        )
        for action in ("reply", "start-reception"):
            response = await browser.post(
                f"{ROOT}/conversations/{conversation.conversation_id}/{action}",
                data={"csrf_token": csrf},
            )
            assert response.status_code == 403


async def test_explicit_grant_and_revocation_scope_inbox_contacts_reports_and_audit(session):
    viewer = await create_staff(session, role="VIEWER")
    visible = await seed_conversation(
        session, account_name="VISIBLE ACCOUNT", shared_with_support=False
    )
    hidden = await seed_conversation(
        session, account_name="HIDDEN ACCOUNT", shared_with_support=True
    )
    grant = models.AccountAccessGrant(
        tenant_id="default",
        user_id=viewer.user_id,
        platform_account_id=visible.account_id,
    )
    visible_audit_id, hidden_audit_id = uuid.uuid4(), uuid.uuid4()
    session.add_all(
        [
            grant,
            models.AuditLog(
                id=visible_audit_id,
                tenant_id="default",
                actor="test",
                action="VISIBLE EVENT",
                category="test",
                subject_type="conversation",
                subject_id=str(visible.conversation_id),
                detail={"status": "active", "private_note": "MUST NOT LEAK"},
            ),
            models.AuditLog(
                id=hidden_audit_id,
                tenant_id="default",
                actor="test",
                action="HIDDEN EVENT",
                category="hidden_category",
                subject_type="conversation",
                subject_id=str(hidden.conversation_id),
                detail={},
            ),
        ]
    )
    await session.commit()
    async with client() as browser:
        await login_client(browser, username=viewer.username, password=viewer.password)
        inbox = await browser.get(f"{ROOT}/inbox")
        assert "VISIBLE ACCOUNT" in inbox.text and "HIDDEN ACCOUNT" not in inbox.text
        assert (
            await browser.get(f"{ROOT}/inbox?item_id={hidden.conversation_id}")
        ).status_code == 404
        audit = await browser.get(f"{ROOT}/audit")
        assert "VISIBLE EVENT" in audit.text and "HIDDEN EVENT" not in audit.text
        assert "hidden_category" not in audit.text
        assert (await browser.get(f"{ROOT}/audit/{hidden_audit_id}")).status_code == 404
        detail = await browser.get(f"{ROOT}/audit/{visible_audit_id}")
        assert detail.status_code == 200 and "MUST NOT LEAK" not in detail.text

        current_grant = await session.scalar(
            select(models.AccountAccessGrant).where(models.AccountAccessGrant.id == grant.id)
        )
        current_grant.active = False
        await session.commit()
        assert (
            await browser.get(f"{ROOT}/inbox?item_id={visible.conversation_id}")
        ).status_code == 404
        assert (await browser.get(f"{ROOT}/audit/{visible_audit_id}")).status_code == 404


@pytest.mark.parametrize("return_to", ["inbox", "https://outside.invalid"])
async def test_start_reception_returns_to_inbox_only_for_the_fixed_navigation_marker(
    session, return_to
):
    staff = await create_staff(session, role="AGENT")
    conversation = await seed_conversation(session, owner_user_id=staff.user_id)
    async with client() as browser:
        csrf = await login_client(browser, username=staff.username, password=staff.password)
        response = await browser.post(
            f"{ROOT}/conversations/{conversation.conversation_id}/start-reception",
            data={"csrf_token": csrf, "return_to": return_to},
        )
        assert response.status_code == 303
        expected_location = (
            f"{ROOT}/inbox?item_id={conversation.conversation_id}"
            if return_to == "inbox"
            else f"{ROOT}/conversations/{conversation.conversation_id}"
        )
        assert response.headers["location"] == expected_location
