import asyncio
import uuid

import pytest
from sqlalchemy import insert, select, update

from social_reply.application.message_delivery import outbox as outbox_module
from social_reply.application.message_delivery import sweep as sweep_module
from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.application.message_delivery.sweep import sweep_outbox
from social_reply.application.reply_decision import runner
from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.domain.automation.state_machine import ensure_state, flip_to_human_active
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flush_fake_sent():
    # Fake 为模块级单例，测试间累积 .sent；本套件各 seed 用相同 content/会话，
    # 故按 [-1] 断言前需隔离——每测试前清空。
    get_chatwoot_client().sent.clear()
    yield


async def _seed(
    session, *, state="BOT_ACTIVE", message_type="text", status="PENDING", with_mapping=True
):
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id, brand_id="b1", platform="telegram", name="a", chatwoot_inbox_id=101
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:x:9",
        )
    )
    await ensure_state(session, conv_id, state)
    if with_mapping:
        await session.execute(
            insert(models.ConversationMapping).values(
                chatwoot_account_id=1, chatwoot_conversation_id=77, conversation_id=conv_id
            )
        )
    ob_id = uuid.uuid4()
    await session.execute(
        insert(models.OutboxMessage).values(
            id=ob_id,
            conversation_id=conv_id,
            platform_account_id=account_id,
            destination_type="chatwoot_conversation",
            destination_id="telegram:x:9",
            message_type=message_type,
            payload={"text": "您好，请提供订单号。", "visibility": "public"},
            idempotency_key=str(ob_id),
            status=status,
        )
    )
    await session.commit()
    return conv_id, ob_id


async def test_disabled_chatwoot_outbox_fails_closed(session, monkeypatch):
    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    fake = get_chatwoot_client()
    before = len(fake.sent)
    monkeypatch.setattr(
        outbox_module,
        "get_settings",
        lambda: type("Settings", (), {"chatwoot_enabled": False})(),
    )

    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    assert len(fake.sent) == before
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW"
    assert ob.attempt_count == 1
    assert ob.last_error_code == "CHATWOOT_DISABLED"
    attempt = (
        await session.execute(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
    ).scalar_one()
    assert attempt.outcome == "NEEDS_REVIEW"
    assert attempt.error_code == "CHATWOOT_DISABLED"

    def enabled():
        return type(
            "Settings",
            (),
            {
                "chatwoot_enabled": True,
                "x_legacy_dm_enabled": True,
                "xchat_enabled": True,
            },
        )()

    monkeypatch.setattr(outbox_module, "get_settings", enabled)
    monkeypatch.setattr(sweep_module, "get_settings", enabled)
    assert ob_id in await sweep_outbox()
    session.expire_all()
    assert (await session.get(models.OutboxMessage, ob_id)).status == "PENDING"
    assert await deliver_outbox(str(ob_id)) == "SENT"
    attempts = list(
        (
            await session.execute(
                select(models.DeliveryAttempt)
                .where(models.DeliveryAttempt.outbox_id == ob_id)
                .order_by(models.DeliveryAttempt.attempt_no)
            )
        ).scalars()
    )
    assert [(item.attempt_no, item.outcome) for item in attempts] == [
        (1, "NEEDS_REVIEW"),
        (2, "SENT"),
    ]


async def test_bot_active_text_delivers_and_marks_sent(session):
    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    result = await deliver_outbox(str(ob_id))
    assert result == "SENT"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "SENT" and ob.chatwoot_message_id is not None and ob.sent_at is not None
    # 真实发送到 Chatwoot（Fake）
    fake = get_chatwoot_client()
    assert fake.sent[-1] == {
        "account_id": 1,
        "conversation_id": 77,
        "content": "您好，请提供订单号。",
        "private": False,
        "id": ob.chatwoot_message_id,
    }
    att = (
        await session.execute(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
    ).scalar_one()  # noqa: E501
    assert att.outcome == "SENT"
    sent_message = (
        await session.execute(
            select(models.Message).where(models.Message.source_outbox_id == ob_id)
        )
    ).scalar_one()
    assert sent_message.conversation_id == conv_id
    assert sent_message.direction == "outbound"
    assert sent_message.sender_type == "bot"
    assert sent_message.text == "您好，请提供订单号。"
    assert sent_message.chatwoot_message_id == ob.chatwoot_message_id

    current_seq = (
        await session.execute(
            insert(models.Message)
            .values(
                id=uuid.uuid4(),
                conversation_id=conv_id,
                direction="inbound",
                sender_type="contact",
                text="那上一条是什么意思？",
            )
            .returning(models.Message.history_seq)
        )
    ).scalar_one()
    await session.commit()
    assert await runner._fetch_history(conv_id, current_seq) == (
        ("assistant", "您好，请提供订单号。"),
    )


async def test_private_note_delivers_as_private(session):
    conv_id, ob_id = await _seed(session, state="BOT_DRAFT_ONLY", message_type="private_note")
    assert await deliver_outbox(str(ob_id)) == "SENT"
    fake = get_chatwoot_client()
    assert fake.sent[-1]["private"] is True
    assert (
        await session.execute(
            select(models.Message).where(models.Message.source_outbox_id == ob_id)
        )
    ).first() is None


async def test_defense2_cancels_text_when_not_bot_active(session):
    # 认领后复检：会话已 HUMAN_ACTIVE → 公开回复不发，标 CANCELLED
    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    await flip_to_human_active(
        session, conv_id, "3", "agent_takeover"
    )  # 会一并取消 PENDING，故先取消  # noqa: E501
    await session.commit()
    # flip 的 defense 3 已把 PENDING 置 CANCELLED；deliver 认领 WHERE PENDING/FAILED 落空
    result = await deliver_outbox(str(ob_id))
    assert result == "SKIPPED_NOT_CLAIMABLE"
    fake = get_chatwoot_client()
    assert fake.sent == []
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "CANCELLED"


async def test_takeover_waits_for_inflight_send_then_commits(session, monkeypatch):
    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_send(**_kwargs):
        started.set()
        await release.wait()
        return 987

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", blocked_send)
    delivery_task = asyncio.create_task(deliver_outbox(str(ob_id)))
    await asyncio.wait_for(started.wait(), timeout=1)
    delivery_task.cancel()

    async def takeover():
        async with get_session_factory()() as takeover_session:
            flipped = await flip_to_human_active(
                takeover_session,
                conv_id,
                "3",
                "agent_takeover",
            )
            await takeover_session.commit()
            return flipped

    takeover_task = asyncio.create_task(takeover())
    await asyncio.sleep(0.05)
    assert takeover_task.done() is False

    release.set()
    assert await delivery_task == "SENT"
    assert await takeover_task is True

    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    state = await session.get(models.AutomationState, conv_id)
    assert outbox.status == "SENT"
    assert state.state == "HUMAN_ACTIVE"
    assert get_chatwoot_client().sent == []


async def test_cancelled_send_timeout_finalizes_ambiguity_and_releases_lock(
    session,
    monkeypatch,
):
    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()

    async def blocked_send(**_kwargs):
        started.set()
        try:
            await never.wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", blocked_send)
    monkeypatch.setattr(outbox_module, "_CANCELLED_SEND_DRAIN_SECONDS", 0.01)

    delivery_task = asyncio.create_task(deliver_outbox(str(ob_id)))
    await asyncio.wait_for(started.wait(), timeout=1)
    delivery_task.cancel()
    assert await delivery_task == "NEEDS_REVIEW"
    await asyncio.wait_for(cancelled.wait(), timeout=1)

    async with get_session_factory()() as takeover_session:
        assert await asyncio.wait_for(
            flip_to_human_active(
                takeover_session,
                conv_id,
                "3",
                "agent_takeover",
            ),
            timeout=1,
        )
        await takeover_session.commit()

    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    state = await session.get(models.AutomationState, conv_id)
    assert outbox.status == "NEEDS_REVIEW"
    assert outbox.last_error_code == "AMBIGUOUS_SEND"
    assert state.state == "HUMAN_ACTIVE"


async def test_defense2_direct_cancel_when_state_flips_without_defense3(session):
    # 模拟 defense 3 未覆盖的窗口：手动把 outbox 留在 PENDING 但状态已 HUMAN_ACTIVE
    conv_id, ob_id = await _seed(session, state="HUMAN_ACTIVE", message_type="text")
    result = await deliver_outbox(str(ob_id))
    assert result == "CANCELLED"  # defense 2 认领后复检拦截
    fake = get_chatwoot_client()
    assert (
        not any(s["conversation_id"] == 77 for s in fake.sent[-1:])
        or fake.sent[-1]["content"] != "您好，请提供订单号。"
    )  # noqa: E501
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "CANCELLED" and ob.last_error_code == "TAKEOVER_AT_SEND"


async def test_no_mapping_marks_needs_review(session):
    conv_id, ob_id = await _seed(
        session, state="BOT_ACTIVE", message_type="text", with_mapping=False
    )
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "NO_MAPPING"


async def test_blank_chatwoot_text_fails_before_network(session, monkeypatch):
    from social_reply.connectors.chatwoot import client as cw

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    await session.execute(
        update(models.OutboxMessage)
        .where(models.OutboxMessage.id == ob_id)
        .values(payload={"text": "   ", "visibility": "public"})
    )
    await session.commit()

    async def unexpected_send(**_kwargs):
        raise AssertionError("blank text must not reach Chatwoot")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", unexpected_send)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.attempt_count == 0
    assert outbox.last_error_code == "DELIVERY_TEXT_INVALID"
    assert (
        await session.scalar(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
        is None
    )


async def test_ambiguous_timeout_marks_needs_review_no_retry(session, monkeypatch):
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**kwargs):
        # 读超时：请求可能已到达服务端 → 歧义
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    result = await deliver_outbox(str(ob_id))
    assert result == "NEEDS_REVIEW"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "AMBIGUOUS_SEND"


async def test_5xx_marks_needs_review_ambiguous(session, monkeypatch):
    # 新语义：5xx 时服务端可能已创建消息 → 歧义，不盲目重试
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**kwargs):
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)
        )

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    result = await deliver_outbox(str(ob_id))
    assert result == "NEEDS_REVIEW"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "AMBIGUOUS_SEND"


async def test_4xx_marks_failed_for_retry(session, monkeypatch):
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**kwargs):
        raise httpx.HTTPStatusError(
            "422", request=httpx.Request("POST", "http://x"), response=httpx.Response(422)
        )

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    result = await deliver_outbox(str(ob_id))
    assert result == "FAILED"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "FAILED" and ob.last_error_code == "SEND_ERROR"


async def test_connect_error_marks_failed_with_backoff(session, monkeypatch):
    # 连接未建立 → 请求必然未发出 → 明确失败可重试，且退避到未来
    from datetime import UTC, datetime

    import httpx

    from social_reply.connectors.chatwoot import client as cw

    conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    now = datetime.now(UTC)
    result = await deliver_outbox(str(ob_id))
    assert result == "FAILED"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "FAILED" and ob.last_error_code == "SEND_ERROR"
    next_at = ob.next_attempt_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at > now  # 指数退避：下次尝试在未来


async def test_connect_timeout_is_retryable_before_request_is_sent(session, monkeypatch):
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**_kwargs):
        raise httpx.ConnectTimeout("connect timeout")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    assert await deliver_outbox(str(ob_id)) == "FAILED"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.status == "FAILED"
    assert outbox.last_error_code == "SEND_ERROR"
    assert outbox.next_attempt_at is not None


async def test_fifth_retryable_send_failure_requires_review(session, monkeypatch):
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")
    await session.execute(
        update(models.OutboxMessage).where(models.OutboxMessage.id == ob_id).values(attempt_count=4)
    )
    await session.commit()

    async def _boom(**_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.attempt_count == 5
    assert outbox.status == "NEEDS_REVIEW"
    assert outbox.last_error_code == "SEND_ERROR"
    assert outbox.next_attempt_at is None


async def test_duplicate_outbox_actor_respects_failed_backoff(session, monkeypatch):
    import httpx

    from social_reply.connectors.chatwoot import client as cw

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    assert await deliver_outbox(str(ob_id)) == "FAILED"
    assert await deliver_outbox(str(ob_id)) == "SKIPPED_NOT_CLAIMABLE"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.status == "FAILED"
    assert outbox.attempt_count == 1
    assert outbox.next_attempt_at is not None
    attempts = list(
        (
            await session.execute(
                select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
            )
        ).scalars()
    )
    assert len(attempts) == 1


async def test_failed_outbox_without_due_time_is_not_claimable(session):
    _conv_id, ob_id = await _seed(
        session,
        state="BOT_ACTIVE",
        message_type="text",
        status="FAILED",
    )

    assert await deliver_outbox(str(ob_id)) == "SKIPPED_NOT_CLAIMABLE"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.status == "FAILED"
    assert outbox.attempt_count == 0


async def test_retryable_platform_error_schedules_retry(session, monkeypatch):
    from social_reply.connectors.chatwoot import client as cw
    from social_reply.connectors.errors import RetryableSendError

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _limited(**_kwargs):
        raise RetryableSendError("RATE_LIMITED")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _limited)
    assert await deliver_outbox(str(ob_id)) == "FAILED"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "FAILED" and ob.next_attempt_at is not None


async def test_unknown_send_error_fails_closed_as_ambiguous(session, monkeypatch):
    from social_reply.connectors.chatwoot import client as cw

    _conv_id, ob_id = await _seed(session, state="BOT_ACTIVE", message_type="text")

    async def _boom(**_kwargs):
        raise RuntimeError("response parsing failed after send")

    monkeypatch.setattr(cw.get_chatwoot_client(), "create_message", _boom)

    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "AMBIGUOUS_SEND"


async def test_finalize_does_not_overwrite_non_sending_row(session, monkeypatch, caplog):
    # 迟到 finalize 场景：行已被 sweep 转 NEEDS_REVIEW，终态 UPDATE 不应覆盖
    from sqlalchemy import update as sa_update

    from social_reply.application.message_delivery.outbox import _finalize

    conv_id, ob_id = await _seed(
        session, state="BOT_ACTIVE", message_type="text", status="NEEDS_REVIEW"
    )
    await session.execute(
        sa_update(models.OutboxMessage)
        .where(models.OutboxMessage.id == ob_id)
        .values(last_error_code="SWEPT")
    )
    await session.commit()

    result = await _finalize(ob_id, "SENT", attempt_no=1, chatwoot_message_id=999)
    assert result == "STALE_FINALIZE"
    session.expire_all()
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    # outbox 状态未被覆盖
    assert ob.status == "NEEDS_REVIEW" and ob.last_error_code == "SWEPT"
    assert ob.chatwoot_message_id is None
    # Audit records the stale finalizer rather than contradicting durable Outbox state.
    att = (
        await session.execute(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
    ).scalar_one()  # noqa: E501
    assert att.outcome == "STALE_FINALIZE" and att.error_code == "STALE_FINALIZE"
    assert att.chatwoot_message_id == 999
    assert (
        await session.execute(
            select(models.Message).where(models.Message.source_outbox_id == ob_id)
        )
    ).first() is None


async def _seed_direct_platform(
    session,
    *,
    platform: str,
    destination_type: str,
    capability: dict,
    target: dict,
    external_account_id: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id, contact_id, conv_id, message_id, ob_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform=platform,
            name="direct",
            external_account_id=external_account_id,
            status="active",
            capability=capability,
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform=platform,
            platform_account_id=account_id,
            external_user_id="user-1",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            tenant_id="default",
            brand_id="b1",
            platform=platform,
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"{platform}:{account_id}:user-1",
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conv_id,
            direction="inbound",
            sender_type="contact",
            text="inbound",
            reply_target=target,
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=ob_id,
            tenant_id="default",
            conversation_id=conv_id,
            platform_account_id=account_id,
            destination_type=destination_type,
            destination_id=f"{platform}:user-1",
            message_type="text",
            payload={"text": "hi", "target": target},
            idempotency_key=str(ob_id),
            status="PENDING",
        )
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            tenant_id="default",
            conversation_id=conv_id,
            message_id=message_id,
            action="auto_reply",
            reply_text="hi",
            source="rule",
            outbox_id=ob_id,
        )
    )
    await session.commit()
    return account_id, ob_id


async def _seed_direct_x(
    session,
    *,
    destination_type="x_dm",
    capability=None,
    target=None,
):
    """直连 X outbox（BOT_ACTIVE），用于验证发送侧错误分类和功能开关。"""
    account_id, contact_id, conv_id, message_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    reply_target = target or {"kind": "dm", "participant_id": "u1"}
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="x",
            name="x-bot",
            status="active",
            capability=capability or {"dm": True, "max_text_length": 280},
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id, platform="x", platform_account_id=account_id, external_user_id="u1"
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            brand_id="b1",
            platform="x",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="x_dm:acc:u1",
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conv_id,
            direction="inbound",
            sender_type="contact",
            text="inbound",
            reply_target=reply_target,
        )
    )
    ob_id = uuid.uuid4()
    await session.execute(
        insert(models.OutboxMessage).values(
            id=ob_id,
            conversation_id=conv_id,
            platform_account_id=account_id,
            destination_type=destination_type,
            destination_id="x_dm:acc:u1",
            message_type="text",
            payload={
                "text": "hi",
                "target": reply_target,
            },
            idempotency_key=str(ob_id),
            status="PENDING",
        )
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            conversation_id=conv_id,
            message_id=message_id,
            action="auto_reply",
            reply_text="hi",
            source="rule",
            outbox_id=ob_id,
        )
    )
    await session.commit()
    return account_id, ob_id


@pytest.mark.parametrize(
    ("destination_type", "capability", "error_code"),
    [
        ("x_dm", {"dm": "false", "max_text_length": 280}, "CAPABILITY_INVALID"),
        ("x_dm", {"dm": True, "max_text_length": 10000}, "CAPABILITY_INVALID"),
        ("telegram_dm", {"dm": True, "max_text_length": 280}, "DELIVERY_ROUTE_INVALID"),
    ],
)
async def test_direct_delivery_fails_closed_for_invalid_account_contract(
    session, destination_type, capability, error_code
):
    _account_id, ob_id = await _seed_direct_x(
        session,
        destination_type=destination_type,
        capability=capability,
    )

    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.last_error_code == error_code


@pytest.mark.parametrize(
    ("platform", "destination_type", "capability", "target", "external_account_id"),
    [
        (
            "facebook",
            "meta_messenger_dm",
            {"dm": True, "comments": True, "max_text_length": 2000},
            {"kind": "comment", "comment_id": "comment-1"},
            "page-1",
        ),
        (
            "facebook",
            "meta_public_comment",
            {"dm": True, "comments": True, "max_text_length": 2000},
            {"kind": "dm", "recipient_id": "user-1"},
            "page-1",
        ),
        (
            "x",
            "x_dm",
            {"dm": True, "x_chat": True, "mentions": True, "max_text_length": 280},
            {"kind": "reply", "in_reply_to_post_id": "post-1"},
            "x-1",
        ),
        (
            "x",
            "x_post_reply",
            {"dm": True, "x_chat": True, "mentions": True, "max_text_length": 280},
            {"kind": "dm", "participant_id": "user-1"},
            "x-1",
        ),
        (
            "whatsapp",
            "whatsapp_session_message",
            {
                "dm": True,
                "session_messages": True,
                "templates": False,
                "max_text_length": 4096,
            },
            {
                "kind": "session_message",
                "phone_number_id": "phone-2",
                "to": "15551234567",
            },
            "phone-1",
        ),
    ],
)
async def test_mismatched_direct_target_fails_before_sender_resolution(
    session,
    monkeypatch,
    platform,
    destination_type,
    capability,
    target,
    external_account_id,
):
    _account_id, ob_id = await _seed_direct_platform(
        session,
        platform=platform,
        destination_type=destination_type,
        capability=capability,
        target=target,
        external_account_id=external_account_id,
    )

    async def unexpected_sender(_account_id):
        raise AssertionError("invalid command must not resolve a sender")

    monkeypatch.setattr(outbox_module, "get_platform_sender", unexpected_sender)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.attempt_count == 0
    assert outbox.last_error_code == "DELIVERY_TARGET_INVALID"
    assert (
        await session.scalar(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
        is None
    )


async def test_valid_shape_wrong_recipient_fails_before_sender_resolution(session, monkeypatch):
    _account_id, ob_id = await _seed_direct_x(session)
    await session.execute(
        update(models.OutboxMessage)
        .where(models.OutboxMessage.id == ob_id)
        .values(
            payload={
                "text": "hi",
                "target": {"kind": "dm", "participant_id": "user-2"},
            }
        )
    )
    await session.commit()

    async def unexpected_sender(_account_id):
        raise AssertionError("wrong recipient must not resolve a sender")

    monkeypatch.setattr(outbox_module, "get_platform_sender", unexpected_sender)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.attempt_count == 0
    assert outbox.last_error_code == "DELIVERY_TARGET_INVALID"


async def test_missing_direct_text_fails_as_contract_error(session, monkeypatch):
    _account_id, ob_id = await _seed_direct_platform(
        session,
        platform="telegram",
        destination_type="telegram_dm",
        capability={"dm": True, "max_text_length": 4096},
        target={"chat_id": 42},
    )
    await session.execute(
        update(models.OutboxMessage)
        .where(models.OutboxMessage.id == ob_id)
        .values(payload={"target": {"chat_id": 42}})
    )
    await session.commit()

    async def unexpected_sender(_account_id):
        raise AssertionError("invalid command must not resolve a sender")

    monkeypatch.setattr(outbox_module, "get_platform_sender", unexpected_sender)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, ob_id)
    assert outbox.attempt_count == 0
    assert outbox.last_error_code == "DELIVERY_TEXT_INVALID"


@pytest.mark.parametrize(
    ("destination_type", "settings_values", "error_code", "capability", "target"),
    [
        (
            "x_dm",
            {"x_legacy_dm_enabled": False, "xchat_enabled": True},
            "X_LEGACY_DM_DISABLED",
            {"dm": True, "max_text_length": 280},
            {"kind": "dm", "participant_id": "u1"},
        ),
        (
            "x_chat_message",
            {"x_legacy_dm_enabled": True, "xchat_enabled": False},
            "XCHAT_DISABLED",
            {"x_chat": True, "max_text_length": 280},
            {"kind": "x_chat", "conversation_id": "u1-u2"},
        ),
    ],
)
async def test_x_stack_disabled_outbox_pauses_and_recovers(
    session,
    monkeypatch,
    destination_type,
    settings_values,
    error_code,
    capability,
    target,
):
    _account_id, ob_id = await _seed_direct_x(
        session,
        destination_type=destination_type,
        capability=capability,
        target=target,
    )

    def settings(**overrides):
        values = {
            "chatwoot_enabled": True,
            "x_legacy_dm_enabled": True,
            "xchat_enabled": True,
            **settings_values,
            **overrides,
        }
        return type("Settings", (), values)()

    monkeypatch.setattr(outbox_module, "get_settings", settings)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    paused = await session.get(models.OutboxMessage, ob_id)
    assert paused.attempt_count == 0
    assert paused.last_error_code == error_code
    assert (
        await session.scalar(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
        is None
    )

    recovered_values = (
        {"x_legacy_dm_enabled": True} if destination_type == "x_dm" else {"xchat_enabled": True}
    )

    def recovered_settings():
        return settings(**recovered_values)

    monkeypatch.setattr(outbox_module, "get_settings", recovered_settings)
    monkeypatch.setattr(sweep_module, "get_settings", recovered_settings)
    assert ob_id in await sweep_outbox()
    session.expire_all()
    recovered = await session.get(models.OutboxMessage, ob_id)
    assert recovered.status == "PENDING"
    assert recovered.last_error_code is None


@pytest.mark.parametrize(
    (
        "platform",
        "destination_type",
        "capability",
        "target",
        "settings_update",
        "error_code",
    ),
    [
        (
            "facebook",
            "meta_messenger_dm",
            {"dm": True, "comments": False, "max_text_length": 2000},
            {"kind": "dm", "recipient_id": "user-1"},
            {"facebook_messenger_enabled": False},
            "FACEBOOK_MESSENGER_DISABLED",
        ),
        (
            "instagram",
            "meta_instagram_dm",
            {"dm": True, "comments": False, "max_text_length": 1000},
            {"kind": "dm", "recipient_id": "user-1"},
            {"instagram_messaging_enabled": False},
            "INSTAGRAM_MESSAGING_DISABLED",
        ),
        (
            "whatsapp",
            "whatsapp_session_message",
            {
                "dm": True,
                "session_messages": True,
                "templates": False,
                "max_text_length": 4096,
            },
            {"kind": "dm", "to": "user-1"},
            {"whatsapp_enabled": False},
            "WHATSAPP_DISABLED",
        ),
    ],
)
async def test_future_platform_disabled_outbox_pauses_and_recovers(
    session,
    monkeypatch,
    platform,
    destination_type,
    capability,
    target,
    settings_update,
    error_code,
):
    _account_id, ob_id = await _seed_direct_platform(
        session,
        platform=platform,
        destination_type=destination_type,
        capability=capability,
        target=target,
    )
    disabled = outbox_module.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(outbox_module, "get_settings", lambda: disabled)

    async def unexpected_sender(_account_id):
        raise AssertionError("disabled platform must not resolve a sender")

    monkeypatch.setattr(outbox_module, "get_platform_sender", unexpected_sender)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    paused = await session.get(models.OutboxMessage, ob_id)
    assert paused.attempt_count == 0
    assert paused.last_error_code == error_code
    assert (
        await session.scalar(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == ob_id)
        )
        is None
    )

    enabled = disabled.model_copy(
        update={
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "whatsapp_enabled": True,
        }
    )
    monkeypatch.setattr(sweep_module, "get_settings", lambda: enabled)
    assert ob_id in await sweep_outbox()
    session.expire_all()
    recovered = await session.get(models.OutboxMessage, ob_id)
    assert recovered.status == "PENDING"
    assert recovered.last_error_code is None


async def test_x_paused_outbox_waits_for_capability_reconciliation(session, monkeypatch):
    _account_id, ob_id = await _seed_direct_x(
        session,
        capability={"dm": False, "max_text_length": 280},
    )

    def disabled_settings():
        return type(
            "Settings",
            (),
            {
                "chatwoot_enabled": True,
                "x_legacy_dm_enabled": False,
                "xchat_enabled": True,
            },
        )()

    def enabled_settings():
        return type(
            "Settings",
            (),
            {
                "chatwoot_enabled": True,
                "x_legacy_dm_enabled": True,
                "xchat_enabled": True,
            },
        )()

    monkeypatch.setattr(outbox_module, "get_settings", disabled_settings)
    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    monkeypatch.setattr(sweep_module, "get_settings", enabled_settings)
    assert ob_id not in await sweep_outbox()
    session.expire_all()
    paused = await session.get(models.OutboxMessage, ob_id)
    assert paused.status == "NEEDS_REVIEW"
    assert paused.last_error_code == "X_LEGACY_DM_DISABLED"


async def test_x_post_reply_is_not_blocked_by_legacy_dm_flag(session, monkeypatch):
    from social_reply.connectors import registry

    account_id, ob_id = await _seed_direct_x(
        session,
        destination_type="x_post_reply",
        capability={"mentions": True, "max_text_length": 280},
        target={"kind": "reply", "in_reply_to_post_id": "post-1"},
    )

    class Sender:
        platform = "x"

        async def send_text(self, *, target, text):
            return "post-reply-1"

        async def aclose(self):
            pass

    async def get_sender(_account_id):
        assert _account_id == account_id
        return Sender()

    monkeypatch.setattr(
        outbox_module,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "chatwoot_enabled": True,
                "x_legacy_dm_enabled": False,
                "xchat_enabled": False,
            },
        )(),
    )
    monkeypatch.setattr(registry, "get_platform_sender", get_sender)
    monkeypatch.setattr(outbox_module, "get_platform_sender", get_sender)

    assert await deliver_outbox(str(ob_id)) == "SENT"


async def test_permanent_send_error_marks_needs_review_no_retry(session, monkeypatch):
    """X 349「对方不收 DM」→ 直接 NEEDS_REVIEW 并透传平台码,不进退避重试。"""
    from social_reply.connectors import registry
    from social_reply.connectors.errors import PermanentSendError

    account_id, ob_id = await _seed_direct_x(session)

    class _RejectingSender:
        platform = "x"

        async def send_text(self, *, target, text):
            raise PermanentSendError("X_CANNOT_DM_349", "You cannot send messages to this user.")

        async def aclose(self):
            pass

    async def _fake_get_sender(_account_id):
        return _RejectingSender()

    monkeypatch.setattr(registry, "get_platform_sender", _fake_get_sender)
    monkeypatch.setattr(
        "social_reply.application.message_delivery.outbox.get_platform_sender", _fake_get_sender
    )

    assert await deliver_outbox(str(ob_id)) == "NEEDS_REVIEW"
    session.expire_all()
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "NEEDS_REVIEW"
    assert ob.last_error_code == "X_CANNOT_DM_349"
    assert ob.next_attempt_at is None  # 永久错不排重试
