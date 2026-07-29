import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from social_reply.application.reply_decision import runner
from social_reply.application.reply_decision.pipeline import DecisionSnapshot
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def _seed_conversation(session, *, tenant_id: str = "default") -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id=tenant_id,
            brand_id="b1",
            platform="telegram",
            name="a",
            chatwoot_inbox_id=101,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id=tenant_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="u1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id=tenant_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:acc:u1",
        )
    )
    await ensure_state(session, conversation_id, "BOT_ACTIVE")
    await session.commit()
    return account_id, conversation_id


async def _add_message(
    session,
    conversation_id: uuid.UUID,
    *,
    direction: str,
    text: str | None,
    minute: int,
    private: bool = False,
) -> tuple[uuid.UUID, int]:
    row = (
        await session.execute(
            insert(models.Message)
            .values(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                direction=direction,
                sender_type="contact" if direction == "inbound" else "agent",
                text=text,
                private=private,
                occurred_at=datetime(2026, 7, 14, 10, minute, tzinfo=UTC),
            )
            .returning(models.Message.id, models.Message.history_seq)
        )
    ).one()
    await session.commit()
    return row.id, row.history_seq


async def test_history_is_ordered_and_maps_roles(session):
    _account_id, conversation_id = await _seed_conversation(session)
    await _add_message(session, conversation_id, direction="inbound", text="想买 A 套餐", minute=1)
    await _add_message(session, conversation_id, direction="outbound", text="已记录", minute=2)
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="那这个多少钱？", minute=3
    )

    history = await runner._fetch_history(conversation_id, current_seq)
    assert history == (("user", "想买 A 套餐"), ("assistant", "已记录"))


async def test_history_excludes_private_empty_and_unknown_direction(session):
    _account_id, conversation_id = await _seed_conversation(session)
    await _add_message(session, conversation_id, direction="inbound", text="正常消息", minute=1)
    await _add_message(
        session,
        conversation_id,
        direction="outbound",
        text="内部备注",
        minute=2,
        private=True,
    )
    await _add_message(session, conversation_id, direction="outbound", text=None, minute=3)
    await _add_message(session, conversation_id, direction="system", text="系统事件", minute=4)
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="当前", minute=5
    )

    history = await runner._fetch_history(conversation_id, current_seq)
    assert history == (("user", "正常消息"),)


async def test_history_cutoff_is_stable_when_future_messages_arrive(session):
    _account_id, conversation_id = await _seed_conversation(session)
    await _add_message(session, conversation_id, direction="inbound", text="before", minute=1)
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="current", minute=2
    )
    await _add_message(session, conversation_id, direction="inbound", text="future-1", minute=3)

    first = await runner._fetch_history(conversation_id, current_seq)
    await _add_message(session, conversation_id, direction="inbound", text="future-2", minute=4)
    second = await runner._fetch_history(conversation_id, current_seq)

    assert first == (("user", "before"),)
    assert second == first


async def test_history_limit_and_character_budget(session, monkeypatch):
    _account_id, conversation_id = await _seed_conversation(session)
    for i in range(1, 6):
        await _add_message(
            session, conversation_id, direction="inbound", text=f"m{i}-xxxx", minute=i
        )
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="current", minute=6
    )

    get_settings.cache_clear()
    monkeypatch.setenv("CONVERSATION_HISTORY_LIMIT", "3")
    monkeypatch.setenv("CONVERSATION_HISTORY_MAX_CHARS", "10")
    try:
        history = await runner._fetch_history(conversation_id, current_seq)
    finally:
        get_settings.cache_clear()

    # Newest messages win the budget: m5 uses 7 chars, then m4 is clipped to 3.
    assert history == (("user", "m4-"), ("user", "m5-xxxx"))


async def test_history_redacts_pii_before_applying_budget(session, monkeypatch):
    _account_id, conversation_id = await _seed_conversation(session)
    await _add_message(
        session,
        conversation_id,
        direction="inbound",
        text="contact alice@example.com",
        minute=1,
    )
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="current", minute=2
    )

    get_settings.cache_clear()
    monkeypatch.setenv("CONVERSATION_HISTORY_MAX_CHARS", "12")
    try:
        history = await runner._fetch_history(conversation_id, current_seq)
    finally:
        get_settings.cache_clear()

    assert history == (("user", "contact [RED"),)
    assert "alice" not in history[0][1]


async def test_history_can_be_disabled(session, monkeypatch):
    _account_id, conversation_id = await _seed_conversation(session)
    await _add_message(session, conversation_id, direction="inbound", text="x", minute=1)
    _current_id, current_seq = await _add_message(
        session, conversation_id, direction="inbound", text="current", minute=2
    )

    get_settings.cache_clear()
    monkeypatch.setenv("CONVERSATION_HISTORY_LIMIT", "0")
    try:
        history = await runner._fetch_history(conversation_id, current_seq)
    finally:
        get_settings.cache_clear()
    assert history == ()


async def test_rule_decision_does_not_fetch_history(session, monkeypatch):
    account_id, conversation_id = await _seed_conversation(session)
    message_id, _message_seq = await _add_message(
        session,
        conversation_id,
        direction="inbound",
        text="我要起诉并投诉",
        minute=1,
    )
    snapshot = DecisionSnapshot(
        text="我要起诉并投诉",
        platform="telegram",
        tenant_id="default",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key="telegram:acc:u1",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )

    async def unexpected_history(*_args, **_kwargs):
        raise AssertionError("history should not be fetched for deterministic rules")

    monkeypatch.setattr(runner, "_fetch_history", unexpected_history)
    assert (
        await runner.run_and_persist_decision(snapshot, conversation_id, message_id, account_id)
        is None
    )


async def test_decision_scope_validation_rejects_cross_tenant_mismatch(session):
    account_id, conversation_id = await _seed_conversation(session, tenant_id="tenant-a")
    message_id, message_seq = await _add_message(
        session, conversation_id, direction="inbound", text="hello", minute=1
    )
    snapshot = DecisionSnapshot(
        text="hello",
        platform="telegram",
        tenant_id="tenant-a",
        brand_id="b1",
        account_id=str(account_id),
        conversation_key="telegram:acc:u1",
        automation_state="BOT_ACTIVE",
        state_version=1,
    )

    assert (
        await runner._validate_decision_scope(snapshot, conversation_id, message_id, account_id)
        == message_seq
    )

    for mismatched in (
        replace(snapshot, tenant_id="tenant-b"),
        replace(snapshot, text="different message"),
        replace(snapshot, account_id=str(uuid.uuid4())),
    ):
        with pytest.raises(runner.DecisionContextScopeError):
            await runner._validate_decision_scope(
                mismatched, conversation_id, message_id, account_id
            )
