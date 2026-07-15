import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _seed(session, *, status="PENDING", next_attempt_at=None, locked_at=None):
    """满足全部 FK 的最小种子（照抄 test_deliver_outbox 的写法）。"""
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"telegram:x:{uuid.uuid4().hex[:8]}"
    # chatwoot_inbox_id 唯一约束：本套件每测多次 seed，用随机值避免撞
    await session.execute(insert(models.PlatformAccount).values(
        id=account_id, brand_id="b1", platform="telegram", name="a",
        chatwoot_inbox_id=uuid.uuid4().int % 10**9))
    await session.execute(insert(models.Contact).values(
        id=contact_id, platform="telegram", platform_account_id=account_id, external_user_id="9"))
    await session.execute(insert(models.Conversation).values(
        id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
        contact_id=contact_id, conversation_key=key))
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    ob_id = uuid.uuid4()
    await session.execute(insert(models.OutboxMessage).values(
        id=ob_id, conversation_id=conv_id, platform_account_id=account_id,
        destination_type="chatwoot_conversation", destination_id=key,
        message_type="text", payload={"text": "hi", "visibility": "public"},
        idempotency_key=str(ob_id), status=status,
        next_attempt_at=next_attempt_at, locked_at=locked_at))
    await session.commit()
    return ob_id


async def test_sweep_enqueues_pending_and_due_failed(session):
    from social_reply.application.message_delivery.sweep import sweep_outbox

    now = datetime.now(UTC)
    pending_id = await _seed(session, status="PENDING")
    due_failed_id = await _seed(session, status="FAILED",
                                next_attempt_at=now - timedelta(seconds=1))
    not_due_id = await _seed(session, status="FAILED",
                             next_attempt_at=now + timedelta(minutes=5))
    sent_id = await _seed(session, status="SENT")

    enqueued = await sweep_outbox()
    assert set(enqueued) == {pending_id, due_failed_id}
    assert not_due_id not in enqueued and sent_id not in enqueued


async def test_sweep_marks_stale_sending_needs_review(session):
    from social_reply.application.message_delivery.sweep import sweep_outbox

    now = datetime.now(UTC)
    stale_id = await _seed(session, status="SENDING", locked_at=now - timedelta(minutes=11))
    fresh_id = await _seed(session, status="SENDING", locked_at=now - timedelta(minutes=1))

    enqueued = await sweep_outbox()
    # 滞留 SENDING 只转人工，不重新入队（防歧义重复发送）
    assert stale_id not in enqueued and fresh_id not in enqueued

    session.expire_all()
    stale = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == stale_id))).scalar_one()
    assert stale.status == "NEEDS_REVIEW" and stale.last_error_code == "STALE_SENDING"
    fresh = (await session.execute(
        select(models.OutboxMessage).where(models.OutboxMessage.id == fresh_id))).scalar_one()
    assert fresh.status == "SENDING"
