import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.event_ingestion import x_dm_poll
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
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
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            webhook_secret_bundle=encrypt_secret_bundle({"consumer_secret": "cs"}),
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

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return events, None

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)

    ingested = await x_dm_poll.poll_x_direct_messages()

    # First poll establishes a cursor only; historical messages are never auto-replied.
    assert ingested == []

    normalized = (
        (
            await session.execute(
                select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
            )
        )
        .scalars()
        .all()
    )
    assert normalized == []

    # 游标推进到最新 id（200）
    acc = await session.get(models.PlatformAccount, account_id)
    assert acc.config.get("x_dm_cursor") == "200"
    assert acc.config.get("x_dm_bootstrapped") is True


async def test_empty_bootstrap_does_not_drop_first_live_message(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    pages = [([], None), ([{"id": "100", "sender_id": _USER, "text": "first live"}], None)]

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return pages.pop(0)

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    assert await x_dm_poll.poll_x_direct_messages() == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config.get("x_dm_bootstrapped") is True
    assert account.config.get("x_dm_cursor") is None

    x_dm_poll._last_poll_at = 0.0
    assert await x_dm_poll.poll_x_direct_messages() == ["100"]


async def test_poll_throttled_within_interval(session, monkeypatch):
    """节流：间隔内的重复调用直接跳过，不打 X API（防 429）。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    await _seed_x_account(session)

    calls = {"n": 0}

    async def fake_read(self, *, max_results=50, pagination_token=None):
        calls["n"] += 1
        return [], None

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    monkeypatch.setattr(x_dm_poll, "_POLL_INTERVAL_SECONDS", 60)

    x_dm_poll._last_poll_at = 0.0
    await x_dm_poll.poll_x_direct_messages()  # 首次真实拉取
    await x_dm_poll.poll_x_direct_messages()  # 间隔内，应跳过
    await x_dm_poll.poll_x_direct_messages()  # 间隔内，应跳过

    assert calls["n"] == 1  # 只真实调用了一次


async def test_poll_ingests_all_fresh_messages_no_drop(session, monkeypatch):
    """同一发送者一批多条新 DM 必须全部入站。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    account.config = {**account.config, "x_dm_cursor": "50", "x_dm_bootstrapped": True}
    await session.commit()

    # 同一小号一个窗口内连发 3 条（X 返回倒序）——三条都必须入站，不能丢中间/旧的
    events = [
        {"id": "300", "sender_id": _USER, "text": "third", "created_at": "2026-07-17T12:03:00Z"},
        {"id": "200", "sender_id": _USER, "text": "second", "created_at": "2026-07-17T12:02:00Z"},
        {"id": "100", "sender_id": _USER, "text": "first", "created_at": "2026-07-17T12:01:00Z"},
    ]

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return events, None

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    ingested = await x_dm_poll.poll_x_direct_messages()

    # 三条按时间正序全部入站
    assert ingested == ["100", "200", "300"]
    session.expire_all()
    normalized = (
        (
            await session.execute(
                select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
            )
        )
        .scalars()
        .all()
    )
    assert sorted(n.external_event_id for n in normalized) == ["100", "200", "300"]
    # 游标推进到最大 id
    acc = (
        await session.execute(
            select(models.PlatformAccount).where(models.PlatformAccount.platform == "x")
        )
    ).scalar_one()
    assert acc.config.get("x_dm_cursor") == "300"


async def test_existing_cursor_implies_bootstrapped_during_rollout(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    account.config = {**account.config, "x_dm_cursor": "100"}
    await session.commit()

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return ([{"id": "200", "sender_id": _USER, "text": "new"}], None)

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    assert await x_dm_poll.poll_x_direct_messages() == ["200"]


async def test_poll_reads_all_pages_until_existing_cursor(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    account.config = {**account.config, "x_dm_cursor": "100", "x_dm_bootstrapped": True}
    await session.commit()
    calls: list[str | None] = []

    async def fake_read(self, *, max_results=50, pagination_token=None):
        calls.append(pagination_token)
        if pagination_token is None:
            return ([{"id": "300", "sender_id": _USER, "text": "third"}], "next")
        return (
            [
                {"id": "200", "sender_id": _USER, "text": "second"},
                {"id": "100", "sender_id": _USER, "text": "old"},
            ],
            "unused",
        )

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = 0.0
    assert await x_dm_poll.poll_x_direct_messages() == ["200", "300"]
    assert calls == [None, "next"]


async def test_poll_cursor_numeric_comparison(session, monkeypatch):
    """游标必须按数值比较：新雪花 id 位数可能与旧游标不同，字符串比较会错判。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)

    # 游标设为一个数值较小的 19 位 id；新事件数值更大，应被处理
    acc = await session.get(models.PlatformAccount, account_id)
    acc.config = {**acc.config, "x_dm_cursor": "1000000000000000000", "x_dm_bootstrapped": True}
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

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return events, None

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
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    account.config = {**account.config, "x_dm_cursor": "50", "x_dm_bootstrapped": True}
    await session.commit()

    events = [
        {
            "id": "100",
            "sender_id": _USER,
            "text": "What is pip?",
            "created_at": "2026-07-17T12:47:00Z",
        },
    ]

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return events, None

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)

    x_dm_poll._last_poll_at = 0.0
    first = await x_dm_poll.poll_x_direct_messages()
    x_dm_poll._last_poll_at = 0.0  # 绕过节流，验证的是游标/去重幂等而非节流
    second = await x_dm_poll.poll_x_direct_messages()

    assert first == ["100"]
    # 游标已推进，第二次不再重复入站
    assert second == []
    normalized = (
        (
            await session.execute(
                select(models.NormalizedEvent).where(models.NormalizedEvent.platform == "x")
            )
        )
        .scalars()
        .all()
    )
    assert len(normalized) == 1
