"""Run serially against social_reply_test; never the local UI acceptance database."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import insert, text

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.home_dashboard_data import load_home_dashboard
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
START = datetime(2026, 9, 2, tzinfo=UTC)


def _principal(**values):
    return Principal(
        **{
            "session_id": uuid4(),
            "user_id": uuid4(),
            "username": "home-reader",
            "actor": "user:home-reader",
            "allowed_tenants": frozenset({"default"}),
            "tenant_id": "default",
            "role": "WORKSPACE_ADMIN",
            **values,
        }
    )


async def _account(session, **values):
    account_id = uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            **{
                "id": account_id,
                "tenant_id": "default",
                "brand_id": "visible-brand",
                "platform": "telegram",
                "name": str(account_id),
                **values,
            }
        )
    )
    return account_id


async def _conversation(session, account_id, *, state=None, tenant_id="default", name="Contact"):
    contact_id, conversation_id = uuid4(), uuid4()
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id=tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id=str(contact_id),
            display_name=name,
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id="visible-brand",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=str(conversation_id),
        )
    )
    if state:
        await session.execute(
            insert(models.AutomationState).values(
                conversation_id=conversation_id,
                state=state,
            )
        )
    return conversation_id


async def _message(session, conversation_id, created_at, **values):
    message_id = uuid4()
    await session.execute(
        insert(models.Message).values(
            **{
                "id": message_id,
                "conversation_id": conversation_id,
                "direction": "inbound",
                "sender_type": "contact",
                "private": False,
                "created_at": created_at,
                "occurred_at": created_at + timedelta(days=50),
                **values,
            }
        )
    )
    return message_id


async def _work(session, conversation_id, **values):
    await session.execute(
        insert(models.HumanWorkItem).values(
            **{
                "tenant_id": "default",
                "conversation_id": conversation_id,
                "status": "WAITING",
                "reason_code": "NEEDS_HUMAN",
                "created_at": NOW - timedelta(hours=1),
                **values,
            }
        )
    )


async def test_empty_dashboard_has_seven_zero_days_and_transaction_local_timeout(
    session, monkeypatch,
):
    execute_spy = AsyncMock(wraps=session.execute)
    scalar_spy = AsyncMock(wraps=session.scalar)
    monkeypatch.setattr(session, "execute", execute_spy)
    monkeypatch.setattr(session, "scalar", scalar_spy)
    await session.begin()
    original_transaction = session.get_transaction()
    result = await load_home_dashboard(session, _principal(), "default", NOW)
    assert session.get_transaction() is original_transaction
    assert execute_spy.await_count + scalar_spy.await_count == 9
    assert result.pending_count == result.ai_count == result.resolved_count == 0
    assert result.account_count == result.enabled_account_count == 0
    assert result.published_knowledge_count == result.draft_knowledge_count == 0
    assert result.pending_draft_count == 0
    assert result.channels == result.attention == ()
    assert [item.day for item in result.trend] == [
        START.date() + timedelta(days=offset) for offset in range(7)
    ]
    assert all(item.received == item.ai == 0 for item in result.trend)
    assert await session.scalar(text("SHOW statement_timeout")) == "5s"
    assert session.in_transaction()


async def test_snapshot_counts_and_utc_daily_distinct_inbound_bot_subset(session):
    await session.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
    account_id = await _account(session)
    await _account(session, platform="email", status="DISABLED")
    active = await _conversation(session, account_id, state="BOT_ACTIVE")
    pending = await _conversation(session, account_id)
    claimed = await _conversation(session, account_id)
    closed = await _conversation(session, account_id, state="CLOSED")
    resolved = await _conversation(session, account_id)
    reopened = await _conversation(session, account_id)
    await _work(session, pending)
    await _work(session, claimed, status="CLAIMED", assigned_actor="user:test", claimed_at=NOW)
    await _work(session, resolved, status="RESOLVED", resolved_at=NOW - timedelta(minutes=2))
    await _work(session, reopened, status="RESOLVED", resolved_at=NOW - timedelta(minutes=2))
    await _message(session, active, START)
    await _message(session, active, START + timedelta(hours=2))
    await _message(
        session, active, START + timedelta(hours=3), direction="outbound", sender_type="bot"
    )
    await _message(
        session, active, START + timedelta(days=1), direction="outbound", sender_type="bot"
    )
    await _message(session, pending, START + timedelta(days=2))
    await _message(
        session,
        pending,
        START + timedelta(days=2, hours=1),
        direction="outbound",
        sender_type="bot",
        private=True,
    )
    await _message(session, closed, START + timedelta(days=3))
    await _message(
        session,
        closed,
        START + timedelta(days=3, hours=1),
        direction="outbound",
        sender_type="agent",
    )
    await _message(session, resolved, NOW - timedelta(minutes=3))
    await _message(session, reopened, NOW - timedelta(minutes=1))
    await _message(session, active, NOW)
    await _message(session, active, NOW, direction="outbound", sender_type="bot")
    await _message(session, claimed, NOW, private=True)
    await _message(session, claimed, START - timedelta(microseconds=1))
    await _message(session, claimed, NOW + timedelta(microseconds=1))
    await _message(
        session, resolved, NOW + timedelta(seconds=1), direction="outbound", sender_type="bot"
    )

    result = await load_home_dashboard(session, _principal(), "default", NOW)
    assert (result.pending_count, result.ai_count, result.resolved_count) == (2, 3, 2)
    assert (result.account_count, result.enabled_account_count) == (2, 1)
    assert [(item.received, item.ai) for item in result.trend] == [
        (1, 1),
        (0, 0),
        (1, 0),
        (1, 0),
        (0, 0),
        (0, 0),
        (3, 1),
    ]
    assert [(item.platform, item.accounts, item.received) for item in result.channels] == [
        ("email", 1, 0),
        ("telegram", 1, 5),
    ]
    assert len(result.attention) == 3


async def test_manager_owner_grants_tenant_brand_and_review_authorization(session):
    manager_id = uuid4()
    await session.execute(
        insert(models.AdminUser).values(
            id=manager_id,
            tenant_id="default",
            username="manager",
            password_hash="unused",
            role="MANAGER",
        )
    )
    own = await _account(session, owner_user_id=manager_id)
    granted = await _account(session, platform="feishu", config={"feishu_health_status": "ERROR"})
    await _account(session, platform="email", brand_id="hidden-brand", status="DISABLED")
    other = await _account(session, tenant_id="other", brand_id="other-brand", status="DISABLED")
    await session.execute(
        insert(models.AccountAccessGrant).values(
            tenant_id="default",
            platform_account_id=granted,
            user_id=manager_id,
            active=True,
        )
    )
    own_conversation = await _conversation(session, own)
    grant_conversation = await _conversation(session, granted)
    other_conversation = await _conversation(session, other, tenant_id="other")
    malformed_conversation = await _conversation(session, own, tenant_id="other")
    for conversation_id in (
        own_conversation,
        grant_conversation,
        other_conversation,
        malformed_conversation,
    ):
        await _message(session, conversation_id, NOW)
    await _work(session, grant_conversation)
    for brand_id, tenant_id in (
        ("visible-brand", "default"),
        ("hidden-brand", "default"),
        ("visible-brand", "other"),
    ):
        for status in ("published", "draft"):
            await session.execute(
                insert(models.KnowledgeDocument).values(
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    status=status,
                    question="Question",
                    reply="Answer",
                )
            )
    draft_message = await _message(session, own_conversation, NOW, decision_generation=0)
    await session.execute(
        insert(models.ReplyDecision).values(
            tenant_id="default",
            conversation_id=own_conversation,
            message_id=draft_message,
            action="draft",
            source="rule",
            reply_text="Reviewable",
            decision_generation=0,
        )
    )
    manager = _principal(role="MANAGER", user_id=manager_id)
    result = await load_home_dashboard(session, manager, "default", NOW)
    assert (result.account_count, result.pending_count, result.ai_count) == (2, 1, 1)
    assert (result.published_knowledge_count, result.draft_knowledge_count) == (1, 1)
    assert result.pending_draft_count == 0
    assert (result.trend[-1].received, result.trend[-1].ai) == (2, 0)
    assert len(result.attention) == 2
    assert {item.kind for item in result.attention} == {"human", "channel"}
    admin_result = await load_home_dashboard(
        session,
        replace(manager, role="WORKSPACE_ADMIN"),
        "default",
        NOW,
    )
    assert admin_result.pending_draft_count == 1
    assert admin_result.published_knowledge_count == 2
    assert admin_result.account_count == 3


async def test_attention_limits_order_and_only_recorded_known_health_failures(session):
    account_id = await _account(session)
    for priority, minutes, name in (
        (0, 3, "low"),
        (9, 2, "later"),
        (9, 4, "first"),
        (5, 1, "third"),
    ):
        conversation_id = await _conversation(session, account_id, name=name)
        await _work(
            session, conversation_id, priority=priority, created_at=NOW - timedelta(minutes=minutes)
        )
    completed = await _conversation(session, account_id, name="not-active")
    await _work(session, completed, priority=100, status="RESOLVED", resolved_at=NOW)
    for name, platform, config, status in (
        ("a-disabled", "telegram", {}, "DISABLED"),
        ("b-meta", "facebook", {"meta_health_status": "REAUTH_REQUIRED"}, "active"),
        ("c-feishu", "feishu", {"feishu_health_status": "BOT_ID_MISMATCH"}, "active"),
        ("d-extra", "instagram", {"meta_health_status": "ERROR"}, "active"),
        ("0-ignore-ready", "facebook", {"meta_health_status": "READY"}, "active"),
        ("0-ignore-unknown", "feishu", {"feishu_health_status": "UNKNOWN"}, "active"),
        ("0-ignore-wrong-platform", "email", {"meta_health_status": "ERROR"}, "active"),
        ("0-ignore-null", "feishu", {"feishu_health_status": None}, "active"),
        ("0-ignore-missing", "facebook", {}, "active"),
    ):
        await _account(session, name=name, platform=platform, config=config, status=status)
    result = await load_home_dashboard(session, _principal(), "default", NOW)
    assert len(result.attention) == 6
    assert [item.title for item in result.attention[:3]] == ["first", "later", "third"]
    assert [item.title for item in result.attention[3:]] == ["a-disabled", "b-meta", "c-feishu"]
    assert all(item.href.startswith("/app/t/default/") for item in result.attention)
    assert all("/inbox?item_id=" in item.href for item in result.attention[:3])
    assert all("/channels/accounts/" in item.href for item in result.attention[3:])
