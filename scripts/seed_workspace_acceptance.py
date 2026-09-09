"""Create fictional UI fixtures only in a dedicated loopback acceptance database.

Requires DATABASE_URL pointing to wikiglobal_ui_test and ACCEPTANCE_PASSWORD.
Does not connect providers, create credentials, enqueue jobs or publish knowledge.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import make_url

from social_reply.application.account_management.auth import hash_password
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

TENANT_ID = "default"
ROLE_NAMES = ("WORKSPACE_ADMIN", "MANAGER", "OPERATOR", "AGENT", "VIEWER")
ACCEPTANCE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "wikiglobal-local-acceptance")


def fixture_id(name: str) -> uuid.UUID:
    return uuid.uuid5(ACCEPTANCE_NAMESPACE, name)


def require_local_acceptance_database() -> None:
    database_url = make_url(os.environ.get("DATABASE_URL", ""))
    if (
        database_url.host not in {"localhost", "127.0.0.1", "::1"}
        or database_url.database != "wikiglobal_ui_test"
        or os.environ.get("TESTING", "").lower() != "true"
    ):
        raise RuntimeError("Acceptance fixtures require TESTING=true and local wikiglobal_ui_test")
    if not os.environ.get("ACCEPTANCE_PASSWORD"):
        raise RuntimeError("Set ACCEPTANCE_PASSWORD; it is never stored in source or printed")


async def create_staff(session, password_hash: str) -> None:
    session.add_all(
        [
            models.AdminUser(
                id=fixture_id(role),
                username=f"acceptance-{role.lower()}",
                password_hash=password_hash,
                tenant_id=TENANT_ID,
                role=role,
                must_change_password=False,
                status="active",
            )
            for role in ROLE_NAMES
        ]
    )
    await session.flush()


async def create_conversations(session) -> None:
    account_id = fixture_id("global-account")
    session.add(
        models.PlatformAccount(
            id=account_id,
            tenant_id=TENANT_ID,
            brand_id="default",
            platform="telegram",
            name="wikiglobal Global [local acceptance]",
            external_account_id="local-acceptance-only",
            owner_user_id=fixture_id("OPERATOR"),
            automation_default="BOT_DRAFT_ONLY",
            shared_with_support=False,
            capability={},
            config={},
        )
    )
    await session.flush()
    session.add_all(
        [
            models.AccountAccessGrant(
                tenant_id=TENANT_ID,
                user_id=fixture_id(role),
                platform_account_id=account_id,
            )
            for role in ("MANAGER", "AGENT", "VIEWER")
        ]
    )
    sample_conversations = (
        ("Sophie Martin", "How can I check a broker's regulatory registration?"),
        ("Alex Chen", "杠杆和保证金有什么区别？"),
        ("Daniel Wu", "Where can I find the official fee schedule?"),
        ("Emma Li", "How do I report a suspicious investment message?"),
    )
    now = datetime.now(UTC)
    for position, (contact_name, message_text) in enumerate(sample_conversations):
        contact_id = fixture_id(contact_name)
        conversation_id = fixture_id(f"conversation-{contact_name}")
        session.add(
            models.Contact(
                id=contact_id,
                tenant_id=TENANT_ID,
                platform="telegram",
                platform_account_id=account_id,
                external_user_id=f"local-{position}",
                display_name=f"{contact_name} [test]",
            )
        )
        await session.flush()
        session.add(
            models.Conversation(
                id=conversation_id,
                tenant_id=TENANT_ID,
                brand_id="default",
                platform="telegram",
                platform_account_id=account_id,
                contact_id=contact_id,
                conversation_key=f"local-acceptance-{position}",
                channel_type="dm",
            )
        )
        await session.flush()
        session.add(
            models.Message(
                conversation_id=conversation_id,
                direction="inbound",
                sender_type="contact",
                text=message_text,
                created_at=now - timedelta(minutes=position * 7),
            )
        )


async def ensure_fixture_automation_states(session) -> None:
    missing_conversations = list(
        await session.scalars(
            select(models.Conversation.id)
            .outerjoin(
                models.AutomationState,
                models.AutomationState.conversation_id == models.Conversation.id,
            )
            .where(
                models.Conversation.platform_account_id == fixture_id("global-account"),
                models.AutomationState.conversation_id.is_(None),
            )
        )
    )
    session.add_all(
        [
            models.AutomationState(conversation_id=conversation_id, state="BOT_DRAFT_ONLY")
            for conversation_id in missing_conversations
        ]
    )


async def main() -> None:
    require_local_acceptance_database()
    password_hash = await hash_password(os.environ["ACCEPTANCE_PASSWORD"])
    async with get_session_factory()() as session, session.begin():
        existing = await session.scalar(
            select(models.AdminUser.id).where(models.AdminUser.id == fixture_id("WORKSPACE_ADMIN"))
        )
        if existing:
            await ensure_fixture_automation_states(session)
            print("Fixtures exist; missing automation states ensured, passwords unchanged.")
            return
        await create_staff(session, password_hash)
        await create_conversations(session)
        await ensure_fixture_automation_states(session)
    print(
        "Created five local acceptance users and four fictional conversations. No external sends."
    )


if __name__ == "__main__":
    asyncio.run(main())
