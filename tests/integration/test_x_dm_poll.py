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


async def _seed_x_account(session, *, dm_capable: bool = True) -> uuid.UUID:
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
            capability={"dm": dm_capable, "x_chat": True},
            chatwoot_inbox_id=None,
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()
    return account_id


async def test_legacy_reenable_reconciles_unknown_dm_capability(session, monkeypatch):
    x_dm_poll._last_poll_at = None
    account_id = await _seed_x_account(session, dm_capable=False)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def read_dm_events(self, *, max_results=100, pagination_token=None):
            return [], None

        async def aclose(self):
            pass

    monkeypatch.setattr(x_dm_poll, "XClient", FakeClient)
    assert await x_dm_poll.poll_x_direct_messages() == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.capability["dm"] is True
    assert account.capability["x_chat"] is True
    assert account.config_version == 2


async def test_legacy_disabled_skips_account_without_verified_capability(session, monkeypatch):
    x_dm_poll._last_poll_at = None
    await _seed_x_account(session, dm_capable=False)
    monkeypatch.setattr(
        x_dm_poll,
        "get_settings",
        lambda: type("Settings", (), {"x_legacy_dm_enabled": False})(),
    )

    class UnexpectedClient:
        def __init__(self, **kwargs):
            raise AssertionError("unverified legacy account must not be polled while disabled")

    monkeypatch.setattr(x_dm_poll, "XClient", UnexpectedClient)
    assert await x_dm_poll.poll_x_direct_messages() == []


async def test_poll_ingests_user_dm_skips_self_and_advances_cursor(session, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    x_dm_poll._last_poll_at = None  # 重置节流，避免跨用例串扰
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
            "event_type": "MessageCreate",
            "dm_conversation_id": "dm-conversation-1",
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
    raw_events = (
        (await session.execute(select(models.RawEvent).order_by(models.RawEvent.external_event_id)))
        .scalars()
        .all()
    )
    assert [row.external_event_id for row in raw_events] == ["100", "200"]
    assert {row.processing_status for row in raw_events} == {"IGNORED_BOOTSTRAP"}
    user_raw = raw_events[0]
    assert user_raw.source == "x_dm_poll"
    assert user_raw.ingress_kind == "poll"
    assert user_raw.event_namespace == "x.legacy_dm"
    assert user_raw.external_conversation_id == "dm-conversation-1"
    assert user_raw.occurred_at.isoformat() == "2026-07-17T12:47:00+00:00"
    assert user_raw.payload["event_type"] == "MessageCreate"
    assert user_raw.context["poll_run_id"]
    assert user_raw.context["page_index"] == 0
    assert user_raw.context["item_index"] == 1

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
    x_dm_poll._last_poll_at = None
    assert await x_dm_poll.poll_x_direct_messages() == []
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.config.get("x_dm_bootstrapped") is True
    assert account.config.get("x_dm_cursor") is None

    x_dm_poll._last_poll_at = None
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

    x_dm_poll._last_poll_at = None
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
    x_dm_poll._last_poll_at = None
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
    raw_events = (
        (await session.execute(select(models.RawEvent).order_by(models.RawEvent.external_event_id)))
        .scalars()
        .all()
    )
    assert [row.external_event_id for row in raw_events] == ["100", "200", "300"]
    assert all(row.processing_status == "PROCESSED" for row in raw_events)
    raw_by_id = {row.external_event_id: row for row in raw_events}
    assert all(event.raw_event_id == raw_by_id[event.external_event_id].id for event in normalized)
    assert all(event.external_conversation_id == _USER for event in normalized)
    assert all(event.event_metadata["event_namespace"] == "x.legacy_dm" for event in normalized)
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
    x_dm_poll._last_poll_at = None
    assert await x_dm_poll.poll_x_direct_messages() == ["200"]


async def test_poll_reads_all_pages_until_existing_cursor(session, monkeypatch):
    """翻页直到越过游标回看窗口;窗口外旧事件不再收集。id 用真实雪花量级(回看窗口按时间位换算)。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    cursor = "2078000000000000000"
    account.config = {**account.config, "x_dm_cursor": cursor, "x_dm_bootstrapped": True}
    await session.commit()
    calls: list[str | None] = []

    async def fake_read(self, *, max_results=50, pagination_token=None):
        calls.append(pagination_token)
        if pagination_token is None:
            return ([{"id": "2078000300000000000", "sender_id": _USER, "text": "third"}], "next")
        return (
            [
                {"id": "2078000200000000000", "sender_id": _USER, "text": "second"},
                # 远早于游标回看窗口(floor = cursor - 5min 雪花偏移)的旧事件,触发停止翻页
                {"id": "2077000000000000000", "sender_id": _USER, "text": "ancient"},
            ],
            "unused",
        )

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = None
    assert await x_dm_poll.poll_x_direct_messages() == [
        "2078000200000000000",
        "2078000300000000000",
    ]
    assert calls == [None, "next"]


async def test_late_arriving_event_within_lookback_is_recovered(session, monkeypatch):
    """晚到事件(id < 游标但在回看窗口内)必须被捞回入站;游标不因此倒退。

    X 全局端点有官方未修复的事件晚到问题:事件延迟出现且 id 小于已推进的游标,
    严格 > cursor 过滤会永久丢弃它。
    """
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    cursor = "2078000300000000000"
    account.config = {**account.config, "x_dm_cursor": cursor, "x_dm_bootstrapped": True}
    await session.commit()

    late_event = "2078000250000000000"  # < cursor,但在 5min 回看窗口内

    async def fake_read(self, *, max_results=50, pagination_token=None):
        return [{"id": late_event, "sender_id": _USER, "text": "late arrival"}], None

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = None
    assert await x_dm_poll.poll_x_direct_messages() == [late_event]

    session.expire_all()
    acc = await session.get(models.PlatformAccount, account_id)
    assert acc.config.get("x_dm_cursor") == cursor  # 游标只进不退


async def test_page_budget_caps_requests_per_poll(session, monkeypatch):
    """深积压时单轮翻页受页帽限制,防止打爆 15 req/15min 限流。"""
    monkeypatch.setenv("KNOWLEDGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    account_id = await _seed_x_account(session)
    account = await session.get(models.PlatformAccount, account_id)
    account.config = {
        **account.config,
        "x_dm_cursor": "2070000000000000000",
        "x_dm_bootstrapped": True,
    }
    await session.commit()

    calls: list[str | None] = []
    next_tokens = {None: "t1", "t1": "t2", "t2": "t3", "t3": "t4"}
    base = 2078000300000000000

    async def fake_read(self, *, max_results=50, pagination_token=None):
        calls.append(pagination_token)
        page_no = len(calls)
        event_id = str(base - page_no * 10_000_000_000)  # 递减但均在游标回看窗口之上
        return (
            [{"id": event_id, "sender_id": _USER, "text": f"page {page_no}"}],
            next_tokens[pagination_token],
        )

    monkeypatch.setattr(x_dm_poll.XClient, "read_dm_events", fake_read)
    x_dm_poll._last_poll_at = None
    ingested = await x_dm_poll.poll_x_direct_messages()

    assert calls == [None, "t1", "t2"]  # 页帽 3:恰好三次请求后截断
    assert len(ingested) == 3  # 已拉到的三页事件正常入站


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
    x_dm_poll._last_poll_at = None
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

    x_dm_poll._last_poll_at = None
    first = await x_dm_poll.poll_x_direct_messages()
    x_dm_poll._last_poll_at = None  # 绕过节流，验证的是游标/去重幂等而非节流
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
    raw_events = (
        (
            await session.execute(
                select(models.RawEvent).where(models.RawEvent.external_event_id == "100")
            )
        )
        .scalars()
        .all()
    )
    assert len(raw_events) == 2
    assert {row.processing_status for row in raw_events} == {
        "PROCESSED",
        "SKIPPED_DUPLICATE",
    }
