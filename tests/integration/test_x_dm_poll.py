import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.event_ingestion import x_dm_poll
from social_reply.infrastructure.database import models
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration

_SELF = "1740258119773458432"
_USER = "2041798240056598528"


async def _seed_x_account(session) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="x",
            name="x-bot",
            external_account_id=_SELF,
            public_id="primary",
            credential_bundle={
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "access_token": "at",
                "access_token_secret": "ats",
            },
            webhook_secret_bundle={"consumer_secret": "cs"},
            config={"delivery_mode": "direct"},
            capability={"dm": True},
            chatwoot_inbox_id=None,
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()
    return account_id


async def test_poll_ingests_user_dm_skips_self_and_advances_cursor(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    x_dm_poll._last_poll_at = 0.0  # 重置节流，避免跨用例串扰
    account_id = await _seed_x_account(session)

    # X 返回按时间倒序：自己发的回复 + 用户真实 DM
    events = [
        {
            "id": "200",
            "sender_id": _SELF,
            "text": "我们的回复",
            "created_at": "2026-07-17T12:59:00Z",
        },
        {
            "id": "100",
            "sender_id": _USER,
            "text": "What is pip?",
            "created_at": "2026-07-17T12:47:00Z",
        },
    ]

    async def fake_read(self, *, max_results=50):
        return events

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)

    ingested = await x_dm_poll.poll_x_direct_messages()

    # 只有用户那条被入站，自己发的被过滤
    assert ingested == ["100"]

    normalized = (
        await session.execute(
            select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
        )
    ).scalars().all()
    assert len(normalized) == 1
    assert normalized[0].external_event_id == "100"

    # 游标推进到最新 id（200）
    acc = await session.get(models.PlatformAccount, account_id)
    assert acc.config.get("x_dm_cursor") == "200"


async def test_poll_throttled_within_interval(session, monkeypatch):
    """节流：间隔内的重复调用直接跳过，不打 X API（防 429）。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    await _seed_x_account(session)

    calls = {"n": 0}

    async def fake_read(self, *, max_results=50):
        calls["n"] += 1
        return []

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    monkeypatch.setattr(x_dm_poll, "_POLL_INTERVAL_SECONDS", 60)

    x_dm_poll._last_poll_at = 0.0
    await x_dm_poll.poll_x_direct_messages()  # 首次真实拉取
    await x_dm_poll.poll_x_direct_messages()  # 间隔内，应跳过
    await x_dm_poll.poll_x_direct_messages()  # 间隔内，应跳过

    assert calls["n"] == 1  # 只真实调用了一次


async def test_poll_burst_guard_replies_only_latest_per_sender(session, monkeypatch):
    """突发保护：同一发送者一批多条消息只入站最新一条，防止恢复积压时连发回复被 X 反垃圾降权。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    await _seed_x_account(session)

    # 同一小号积压 3 条（倒序，如 X 返回），只应处理最新的 300
    events = [
        {"id": "300", "sender_id": _USER, "text": "third", "created_at": "2026-07-17T12:03:00Z"},
        {"id": "200", "sender_id": _USER, "text": "second", "created_at": "2026-07-17T12:02:00Z"},
        {"id": "100", "sender_id": _USER, "text": "first", "created_at": "2026-07-17T12:01:00Z"},
    ]

    async def fake_read(self, *, max_results=50):
        return events

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    ingested = await x_dm_poll.poll_x_direct_messages()

    assert ingested == ["300"]  # 只回最新
    session.expire_all()
    normalized = (
        await session.execute(
            select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
        )
    ).scalars().all()
    assert [n.external_event_id for n in normalized] == ["300"]


async def test_poll_cursor_numeric_comparison(session, monkeypatch):
    """游标必须按数值比较：新雪花 id 位数可能与旧游标不同，字符串比较会错判。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)

    # 游标设为一个数值较小的 19 位 id；新事件数值更大，应被处理
    acc = await session.get(models.PlatformAccount, account_id)
    acc.config = {**acc.config, "x_dm_cursor": "1000000000000000000"}
    await session.commit()

    # 新事件 id 数值 > 游标（都是19位），应被处理
    events = [
        {
            "id": "2078120216798941669",
            "sender_id": _USER,
            "text": "What is pip?",
            "created_at": "2026-07-17T14:00:00Z",
        },
    ]

    async def fake_read(self, *, max_results=50):
        return events

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    ingested = await x_dm_poll.poll_x_direct_messages()

    assert ingested == ["2078120216798941669"]
    session.expire_all()  # 丢弃身份映射缓存，读 _save_cursor 独立 session 的提交
    acc2 = await session.get(models.PlatformAccount, account_id)
    assert acc2.config.get("x_dm_cursor") == "2078120216798941669"


async def test_poll_is_idempotent_on_repeat(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    await _seed_x_account(session)

    events = [
        {
            "id": "100",
            "sender_id": _USER,
            "text": "What is pip?",
            "created_at": "2026-07-17T12:47:00Z",
        },
    ]

    async def fake_read(self, *, max_results=50):
        return events

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)

    x_dm_poll._last_poll_at = 0.0
    first = await x_dm_poll.poll_x_direct_messages()
    x_dm_poll._last_poll_at = 0.0  # 绕过节流，验证的是游标/去重幂等而非节流
    second = await x_dm_poll.poll_x_direct_messages()

    assert first == ["100"]
    # 游标已推进，第二次不再重复入站
    assert second == []
    normalized = (
        await session.execute(
            select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
        )
    ).scalars().all()
    assert len(normalized) == 1
