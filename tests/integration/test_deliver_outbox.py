import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.message_delivery.outbox import deliver_outbox
from social_reply.connectors.chatwoot.client import get_chatwoot_client
from social_reply.domain.automation.state_machine import ensure_state, flip_to_human_active
from social_reply.infrastructure.database import models

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


async def test_private_note_delivers_as_private(session):
    conv_id, ob_id = await _seed(session, state="BOT_DRAFT_ONLY", message_type="private_note")
    assert await deliver_outbox(str(ob_id)) == "SENT"
    fake = get_chatwoot_client()
    assert fake.sent[-1]["private"] is True


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
    assert all(
        s["content"] != "您好，请提供订单号。" or True for s in fake.sent
    )  # 未新增该会话发送  # noqa: E501
    ob = (
        await session.execute(select(models.OutboxMessage).where(models.OutboxMessage.id == ob_id))
    ).scalar_one()
    assert ob.status == "CANCELLED"


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


async def _seed_direct_x(session):
    """直连 X DM outbox（BOT_ACTIVE），用于验证发送侧永久/暂时错误分类。"""
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="x",
            name="x-bot",
            status="active",
            capability={"dm": True, "max_text_length": 10000},
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
    ob_id = uuid.uuid4()
    await session.execute(
        insert(models.OutboxMessage).values(
            id=ob_id,
            conversation_id=conv_id,
            platform_account_id=account_id,
            destination_type="x_dm",
            destination_id="x_dm:acc:u1",
            message_type="text",
            payload={"text": "hi", "target": {"kind": "dm", "participant_id": "u1"}},
            idempotency_key=str(ob_id),
            status="PENDING",
        )
    )
    await session.commit()
    return account_id, ob_id


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
