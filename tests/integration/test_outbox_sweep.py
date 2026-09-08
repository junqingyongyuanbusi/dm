import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from social_reply.application.message_delivery import sweep as sweep_module
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def reset_dispatch_cursor(monkeypatch):
    monkeypatch.setattr(sweep_module, "_dispatch_cursor", None)


async def _seed(
    session, *, status="PENDING", next_attempt_at=None, locked_at=None, attempt_count=0
):
    outbox_id = uuid.uuid4()
    await _seed_batch(
        session,
        outbox_ids=[outbox_id],
        status=status,
        next_attempt_at=next_attempt_at,
        locked_at=locked_at,
        attempt_count=attempt_count,
    )
    return outbox_id


async def _seed_batch(
    session,
    *,
    outbox_ids: list[uuid.UUID],
    status="PENDING",
    next_attempt_at=None,
    locked_at=None,
    attempt_count=0,
):
    """满足全部 FK 的最小种子（照抄 test_deliver_outbox 的写法）。"""
    account_id, contact_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"telegram:x:{uuid.uuid4().hex[:8]}"
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="a",
            config={"delivery_mode": "direct"},
            capability={"dm": True, "max_text_length": 4096},
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
            conversation_key=key,
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.execute(
        insert(models.OutboxMessage),
        [
            {
                "id": outbox_id,
                "conversation_id": conv_id,
                "platform_account_id": account_id,
                "destination_type": "telegram_dm",
                "destination_id": key,
                "message_type": "text",
                "payload": {
                    "text": "hi",
                    "visibility": "public",
                    "target": {"kind": "dm", "chat_id": "9"},
                },
                "idempotency_key": str(outbox_id),
                "status": status,
                "next_attempt_at": next_attempt_at,
                "locked_at": locked_at,
                "attempt_count": attempt_count,
            }
            for outbox_id in outbox_ids
        ],
    )
    await session.commit()


async def test_sweep_enqueues_pending_and_due_failed(session):
    from social_reply.application.message_delivery.sweep import sweep_outbox

    now = datetime.now(UTC)
    pending_id = await _seed(session, status="PENDING")
    due_failed_id = await _seed(
        session, status="FAILED", next_attempt_at=now - timedelta(seconds=1)
    )
    not_due_id = await _seed(session, status="FAILED", next_attempt_at=now + timedelta(minutes=5))
    sent_id = await _seed(session, status="SENT")

    enqueued = await sweep_outbox()
    assert set(enqueued) == {pending_id, due_failed_id}
    assert not_due_id not in enqueued and sent_id not in enqueued


async def test_outbox_sweep_isolates_broker_dispatch_failures(session, monkeypatch):
    first = await _seed(session)
    second = await _seed(session)
    calls: list[uuid.UUID] = []
    from social_reply.application.message_delivery import sweep as sweep_module

    async def dispatch(_actor, outbox_id: str, **_kwargs):
        calls.append(uuid.UUID(outbox_id))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(sweep_module, "dispatch_actor", dispatch)

    dispatched = await sweep_module.sweep_outbox()
    assert set(calls) == {first, second}
    assert len(dispatched) == 1
    assert dispatched[0] == calls[1]


async def test_sweep_marks_stale_sending_needs_review(session):
    from social_reply.application.message_delivery.sweep import sweep_outbox

    now = datetime.now(UTC)
    stale_id = await _seed(
        session, status="SENDING", locked_at=now - timedelta(minutes=11), attempt_count=3
    )
    fresh_id = await _seed(session, status="SENDING", locked_at=now - timedelta(minutes=1))

    enqueued = await sweep_outbox()
    # 滞留 SENDING 只转人工，不重新入队（防歧义重复发送）
    assert stale_id not in enqueued and fresh_id not in enqueued

    session.expire_all()
    stale = (
        await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.id == stale_id)
        )
    ).scalar_one()
    assert stale.status == "NEEDS_REVIEW" and stale.last_error_code == "STALE_SENDING"
    fresh = (
        await session.execute(
            select(models.OutboxMessage).where(models.OutboxMessage.id == fresh_id)
        )
    ).scalar_one()
    assert fresh.status == "SENDING"
    # 转 NEEDS_REVIEW 必须留审计行（与 deliver_outbox 终态一致）
    att = (
        await session.execute(
            select(models.DeliveryAttempt).where(models.DeliveryAttempt.outbox_id == stale_id)
        )
    ).scalar_one()
    assert att.outcome == "NEEDS_REVIEW" and att.error_code == "STALE_SENDING"
    assert att.attempt_no == 3


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
async def test_sweep_traverses_eligible_backlog_without_reserving_rows(
    session, monkeypatch, status
):
    outbox_ids = [uuid.UUID(int=number) for number in range(1, 206)]
    next_attempt_at = datetime.now(UTC) - timedelta(minutes=1) if status == "FAILED" else None
    await _seed_batch(
        session, outbox_ids=outbox_ids, status=status, next_attempt_at=next_attempt_at
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(sweep_module, "dispatch_actor", dispatch)

    for expected_batch in (
        outbox_ids[:100],
        outbox_ids[100:200],
        outbox_ids[200:],
        outbox_ids[:100],
    ):
        dispatch.reset_mock()
        assert await sweep_module.sweep_outbox() == expected_batch
        assert [uuid.UUID(call.args[1]) for call in dispatch.await_args_list] == expected_batch

    rows = (
        await session.execute(
            select(models.OutboxMessage.status, models.OutboxMessage.next_attempt_at)
        )
    ).all()
    assert len(rows) == len(outbox_ids)
    assert all(row.status == status and row.next_attempt_at == next_attempt_at for row in rows)

    # A process restart loses only the traversal hint, not durable eligible work.
    monkeypatch.setattr(sweep_module, "_dispatch_cursor", None)
    assert await sweep_module.sweep_outbox() == outbox_ids[:100]


async def test_sweep_advances_after_broker_failures_including_a_wrapped_batch(session, monkeypatch):
    outbox_ids = [uuid.UUID(int=number) for number in range(1, 106)]
    await _seed_batch(session, outbox_ids=outbox_ids)
    dispatch = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(sweep_module, "dispatch_actor", dispatch)

    assert await sweep_module.sweep_outbox() == []
    assert [uuid.UUID(call.args[1]) for call in dispatch.await_args_list] == outbox_ids[:100]
    assert sweep_module._dispatch_cursor == outbox_ids[99]

    dispatch.side_effect = None
    assert await sweep_module.sweep_outbox() == outbox_ids[100:]

    dispatch.reset_mock()
    dispatch.side_effect = RuntimeError("broker unavailable after wrap")
    assert await sweep_module.sweep_outbox() == []
    assert [uuid.UUID(call.args[1]) for call in dispatch.await_args_list] == outbox_ids[:100]
    assert sweep_module._dispatch_cursor == outbox_ids[99]

    dispatch.side_effect = None
    assert await sweep_module.sweep_outbox() == outbox_ids[100:]
    assert await sweep_module.sweep_outbox() == outbox_ids[:100]

    rows = (
        await session.execute(
            select(models.OutboxMessage.status, models.OutboxMessage.next_attempt_at)
        )
    ).all()
    assert len(rows) == len(outbox_ids)
    assert all(row.status == "PENDING" and row.next_attempt_at is None for row in rows)


async def test_sweep_wraps_to_newly_inserted_lower_uuid(session, monkeypatch):
    outbox_ids = [uuid.UUID(int=number) for number in range(1001, 1102)]
    await _seed_batch(session, outbox_ids=outbox_ids)
    monkeypatch.setattr(sweep_module, "dispatch_actor", AsyncMock())

    assert await sweep_module.sweep_outbox() == outbox_ids[:100]
    inserted_id = uuid.UUID(int=1)
    await _seed_batch(session, outbox_ids=[inserted_id])

    assert await sweep_module.sweep_outbox() == outbox_ids[100:]
    assert await sweep_module.sweep_outbox() == [inserted_id, *outbox_ids[:99]]


async def test_sweep_reviews_stale_sending_in_ordered_batches_without_duplicate_audits(
    session, monkeypatch
):
    now = datetime.now(UTC)
    tied_ids = [uuid.UUID(int=number) for number in range(1, 205)]
    await _seed_batch(
        session,
        outbox_ids=tied_ids,
        status="SENDING",
        locked_at=now - timedelta(minutes=11),
        attempt_count=3,
    )
    oldest_id = uuid.UUID(int=1000)
    await _seed_batch(
        session,
        outbox_ids=[oldest_id],
        status="SENDING",
        locked_at=now - timedelta(minutes=20),
        attempt_count=7,
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(sweep_module, "dispatch_actor", dispatch)
    ordered_ids = [oldest_id, *tied_ids]

    for reviewed_count in (100, 200, 205, 205):
        assert await sweep_module.sweep_outbox() == []
        reviewed_ids = set(
            (
                await session.execute(
                    select(models.OutboxMessage.id).where(
                        models.OutboxMessage.status == "NEEDS_REVIEW",
                        models.OutboxMessage.last_error_code == "STALE_SENDING",
                    )
                )
            ).scalars()
        )
        assert reviewed_ids == set(ordered_ids[:reviewed_count])
        attempts = (
            await session.execute(
                select(
                    models.DeliveryAttempt.outbox_id,
                    models.DeliveryAttempt.attempt_no,
                    models.DeliveryAttempt.outcome,
                    models.DeliveryAttempt.error_code,
                )
            )
        ).all()
        assert len(attempts) == reviewed_count
        assert {attempt.outbox_id for attempt in attempts} == reviewed_ids
        assert all(
            attempt.outcome == "NEEDS_REVIEW"
            and attempt.error_code == "STALE_SENDING"
            and attempt.attempt_no == (7 if attempt.outbox_id == oldest_id else 3)
            for attempt in attempts
        )

    dispatch.assert_not_awaited()


async def test_sweep_skips_locked_stale_rows_and_recovers_them_after_release(session, monkeypatch):
    outbox_ids = [uuid.UUID(int=number) for number in range(1, 102)]
    await _seed_batch(
        session,
        outbox_ids=outbox_ids,
        status="SENDING",
        locked_at=datetime.now(UTC) - timedelta(minutes=11),
    )
    monkeypatch.setattr(sweep_module, "dispatch_actor", AsyncMock())
    await session.execute(
        select(models.OutboxMessage.id)
        .where(models.OutboxMessage.id == outbox_ids[0])
        .with_for_update()
    )

    async with asyncio.timeout(10):
        assert await sweep_module.sweep_outbox() == []
    audited_ids = (await session.execute(select(models.DeliveryAttempt.outbox_id))).scalars().all()
    assert len(audited_ids) == 100
    assert set(audited_ids) == set(outbox_ids[1:])
    await session.rollback()

    assert await sweep_module.sweep_outbox() == []
    audited_ids = (await session.execute(select(models.DeliveryAttempt.outbox_id))).scalars().all()
    assert len(audited_ids) == 101
    assert set(audited_ids) == set(outbox_ids)
