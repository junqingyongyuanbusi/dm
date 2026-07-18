import httpx
import pytest
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models

pytestmark = pytest.mark.integration


async def _login(client: httpx.AsyncClient) -> str:
    page = await client.get("/admin/login")
    assert page.status_code == 200
    csrf = client.cookies["reply_admin_csrf"]
    await client.post(
        "/admin/login",
        data={"csrf_token": csrf, "username": "admin", "password": "test-admin-password"},
    )
    return csrf


def _app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def test_console_pages_require_login():
    async with _app_client() as client:
        for path in (
            "/admin",
            "/admin/conversations",
            "/admin/decisions",
            "/admin/knowledge",
            "/admin/delivery",
            "/admin/accounts",
        ):
            resp = await client.get(path)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/admin/login"


async def test_console_pages_render_after_login(migrated_db):
    async with _app_client() as client:
        await _login(client)
        for path, marker in (
            ("/admin", "总览"),
            ("/admin/conversations", "对话"),
            ("/admin/decisions", "决策"),
            ("/admin/knowledge", "知识库"),
            ("/admin/delivery", "投递"),
            ("/admin/accounts", "账号"),
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert marker in resp.text


async def test_conversation_state_flip_takeover(session, migrated_db):
    # 构造一个 BOT_ACTIVE 会话，验证人工接管把状态翻到 HUMAN_ACTIVE
    account_id, contact_id, conv_id = (
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id, brand_id="b1", platform="telegram", name="acc",
            public_id="p1", credential_bundle={"bot_token": "t"},
            config={"delivery_mode": "direct"}, automation_default="BOT_ACTIVE", status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id, platform="telegram", platform_account_id=account_id,
            external_user_id="u1", display_name="小明",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id, brand_id="b1", platform="telegram", platform_account_id=account_id,
            contact_id=contact_id, conversation_key="telegram:x:u1",
        )
    )
    await ensure_state(session, conv_id, "BOT_ACTIVE")
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        detail = await client.get(f"/admin/conversations/{conv_id}")
        assert detail.status_code == 200
        assert "小明" in detail.text
        resp = await client.post(
            f"/admin/conversations/{conv_id}/state",
            data={"csrf_token": csrf, "target": "HUMAN_ACTIVE", "expect": "BOT_ACTIVE"},
        )
        assert resp.status_code == 303

    state = (
        await session.execute(
            select(models.AutomationState.state).where(
                models.AutomationState.conversation_id == conv_id
            )
        )
    ).scalar_one()
    assert state == "HUMAN_ACTIVE"


async def test_knowledge_add_and_delete_via_console(session, migrated_db, monkeypatch):
    # 注入 Fake embedder，避免真实 API 调用
    from social_reply.application.reply_decision import runner
    from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(runner, "_embedder", FakeEmbeddingClient())

    async with _app_client() as client:
        csrf = await _login(client)
        resp = await client.post(
            "/admin/knowledge/add",
            data={
                "csrf_token": csrf,
                "question": "你们几点营业",
                "reply": "每天 9:00-21:00",
                "category": "常见",
            },
        )
        assert resp.status_code == 303
        assert "notice=added" in resp.headers["location"]

    docs = (await session.execute(select(models.KnowledgeDocument))).scalars().all()
    assert any(d.question == "你们几点营业" for d in docs)
    chunk = (await session.execute(select(models.KnowledgeChunk))).scalars().first()
    assert chunk.embed_text == "你们几点营业"  # 非对称嵌入：只嵌问题


async def test_killswitch_toggle_sets_flag(migrated_db):
    import redis.asyncio as aioredis

    from social_reply.shared.config import get_settings

    settings = get_settings()
    key = f"killswitch:global:{settings.tenant_id}"
    redis = aioredis.from_url(settings.redis_url)
    await redis.delete(key)  # 清理前置状态
    try:
        async with _app_client() as client:
            csrf = await _login(client)
            resp = await client.post(
                "/admin/killswitch/toggle", data={"csrf_token": csrf, "scope": "global"}
            )
            assert resp.status_code == 303
        assert await redis.get(key) is not None  # 已置急停
        # 再次切换应解除
        async with _app_client() as client:
            csrf = await _login(client)
            await client.post(
                "/admin/killswitch/toggle", data={"csrf_token": csrf, "scope": "global"}
            )
        assert await redis.get(key) is None
    finally:
        await redis.delete(key)
        await redis.aclose()
