import base64
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle
from social_reply.shared.config import get_settings

pytestmark = pytest.mark.integration


async def _seed(session, *, automation_default: str) -> uuid.UUID:
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="x",
            name="X App",
            public_id="x_mention_test",
            credential_bundle=encrypt_secret_bundle(
                {"consumer_key": "ck", "consumer_secret": "webhook-secret"}
            ),
            config={},
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="x",
            platform_app_id=app_id,
            name="bot",
            external_account_id="bot-1",
            public_id="x_mention_bot",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "webhook-secret",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            config={},
            capability={"dm": True, "x_chat": False, "mentions": True, "max_text_length": 280},
            automation_default=automation_default,
            status="active",
        )
    )
    await session.commit()
    return account_id


def _mention_body(
    *, event_uuid: str = "mention-1", post_id: str = "post-1", author_id: str = "user-9"
) -> tuple[bytes, str]:
    payload = {
        "data": {
            "event_type": "post.mention.create",
            "event_uuid": event_uuid,
            "filter": {"user_id": "bot-1"},
            "payload": {
                "id": post_id,
                "conversation_id": "thread-1",
                "author_id": author_id,
                "text": "@bot how do I avoid scams?",
                "created_at": "2026-07-24T21:23:23.000Z",
                "reply_settings": "everyone",
            },
        }
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        "sha256="
        + base64.b64encode(hmac.new(b"webhook-secret", body, hashlib.sha256).digest()).decode()
    )
    return body, signature


async def _post_mention(**kwargs) -> httpx.Response:
    body, signature = _mention_body(**kwargs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/webhooks/x/x_mention_test",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Twitter-Webhooks-Signature": signature,
            },
        )


async def test_mention_is_ignored_while_public_reply_is_disabled(session, migrated_db, monkeypatch):
    monkeypatch.setenv("X_PUBLIC_REPLY_ENABLED", "false")
    get_settings.cache_clear()
    await _seed(session, automation_default="BOT_ACTIVE")

    response = await _post_mention()

    assert response.status_code == 200
    session.expire_all()
    raw = (
        await session.execute(
            select(models.RawEvent).where(models.RawEvent.external_event_id == "mention-1")
        )
    ).scalar_one()
    assert raw.processing_status == "IGNORED_X_PUBLIC_REPLY_DISABLED"
    assert (await session.execute(select(models.Conversation))).first() is None
    get_settings.cache_clear()


async def test_mention_thread_never_inherits_bot_active(session, migrated_db, monkeypatch):
    # X 对 AI 生成的公开回复要求事先报批；账号即使是 BOT_ACTIVE，
    # mention 也必须落进人工待审队列，不能直接对外发推。
    monkeypatch.setenv("X_PUBLIC_REPLY_ENABLED", "true")
    get_settings.cache_clear()
    account_id = await _seed(session, automation_default="BOT_ACTIVE")

    response = await _post_mention()

    assert response.status_code == 200
    session.expire_all()
    conversation = (
        await session.execute(
            select(models.Conversation).where(models.Conversation.platform_account_id == account_id)
        )
    ).scalar_one()
    assert conversation.conversation_key.endswith(":thread-1:user-9")
    assert conversation.channel_type == "mention"
    state = (
        await session.execute(
            select(models.AutomationState).where(
                models.AutomationState.conversation_id == conversation.id
            )
        )
    ).scalar_one()
    assert state.state == "BOT_DRAFT_ONLY"
    # 公开回复绝不能在无人审核的情况下进入投递队列
    assert (await session.execute(select(models.OutboxMessage))).first() is None
    get_settings.cache_clear()


async def test_dm_still_inherits_the_account_default(session, migrated_db, monkeypatch):
    # 只有 mention 被强制降级；DM 仍然按账号默认值走，不受这次改动影响
    monkeypatch.setenv("X_PUBLIC_REPLY_ENABLED", "true")
    get_settings.cache_clear()
    account_id = await _seed(session, automation_default="BOT_ACTIVE")
    payload = {
        "data": {
            "event_type": "dm.received",
            "event_uuid": "dm-evt-1",
            "filter": {"user_id": "bot-1"},
            "payload": {
                "id": "dm-1",
                "event_type": "MessageCreate",
                "sender_id": "user-9",
                "dm_conversation_id": "dm-conv-1",
                "text": "hello",
                "created_at": "2026-07-20T01:51:31Z",
            },
        }
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        "sha256="
        + base64.b64encode(hmac.new(b"webhook-secret", body, hashlib.sha256).digest()).decode()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhooks/x/x_mention_test",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Twitter-Webhooks-Signature": signature,
            },
        )

    assert response.status_code == 200
    session.expire_all()
    conversation = (
        await session.execute(
            select(models.Conversation).where(models.Conversation.platform_account_id == account_id)
        )
    ).scalar_one()
    state = (
        await session.execute(
            select(models.AutomationState).where(
                models.AutomationState.conversation_id == conversation.id
            )
        )
    ).scalar_one()
    assert state.state == "BOT_ACTIVE"
    get_settings.cache_clear()


async def test_two_commenters_on_one_post_get_separate_conversations(
    session, migrated_db, monkeypatch
):
    # 一条帖子下所有评论共享 conversation_id。若只按 thread 建会话键，
    # 第一个评论者会占坑，后续所有人的留言都会挂到他的 contact 上。
    monkeypatch.setenv("X_PUBLIC_REPLY_ENABLED", "true")
    get_settings.cache_clear()
    account_id = await _seed(session, automation_default="BOT_DRAFT_ONLY")

    assert (
        await _post_mention(event_uuid="m-a", post_id="p-a", author_id="user-a")
    ).status_code == 200
    assert (
        await _post_mention(event_uuid="m-b", post_id="p-b", author_id="user-b")
    ).status_code == 200

    session.expire_all()
    conversations = (
        (
            await session.execute(
                select(models.Conversation).where(
                    models.Conversation.platform_account_id == account_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(conversations) == 2
    assert {c.conversation_key for c in conversations} == {
        f"x_reply:{account_id}:thread-1:user-a",
        f"x_reply:{account_id}:thread-1:user-b",
    }
    # 两个会话各自绑定到自己的联系人，没有串号
    assert len({c.contact_id for c in conversations}) == 2
    get_settings.cache_clear()
