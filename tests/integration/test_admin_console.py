import httpx
import pytest
from sqlalchemy import insert, select

from apps.api.main import create_app
from social_reply.domain.automation.state_machine import ensure_state
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

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


async def test_accounts_page_renders_oauth_connect_cards(migrated_db):
    """账号页 web UI 与 OAuth 模块同步:两张 OAuth 推荐卡 + Telegram 指引都要渲染出来。"""
    async with _app_client() as client:
        await _login(client)
        resp = await client.get("/admin/accounts")
    assert resp.status_code == 200
    html = resp.text
    # OAuth 一键授权卡片(发起端点 + 推荐标记)
    assert 'action="/admin/oauth/x/start"' in html
    assert 'action="/admin/oauth/meta/start"' in html
    assert html.count("OAuth 一键授权（推荐）") == 2
    # Meta OAuth 卡片提供 facebook / instagram 两个平台选项
    assert 'name="platform"' in html and "Instagram（专业账号" in html
    # 回调 URL 直接渲染在页面,便于登记到平台后台
    assert "/admin/oauth/x/callback" in html
    assert "/admin/oauth/meta/callback" in html
    # Telegram 无 OAuth,给 BotFather 指引 + 手工表单
    assert "BotFather" in html and 'action="/admin/connect/telegram"' in html


async def test_conversation_state_flip_takeover(session, migrated_db):
    # 构造一个 BOT_ACTIVE 会话，验证人工接管把状态翻到 HUMAN_ACTIVE
    account_id, contact_id, conv_id = (
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform="telegram",
            name="acc",
            public_id="p1",
            credential_bundle=encrypt_secret_bundle({"bot_token": "t"}),
            config={"delivery_mode": "direct"},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform="telegram",
            platform_account_id=account_id,
            external_user_id="u1",
            display_name="小明",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conv_id,
            brand_id="b1",
            platform="telegram",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key="telegram:x:u1",
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
                "tenant_id": "default",
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
                "/admin/killswitch/toggle",
                data={"csrf_token": csrf, "scope": "global", "tenant_id": settings.tenant_id},
            )
            assert resp.status_code == 303
        assert await redis.get(key) is not None  # 已置急停
        # 再次切换应解除
        async with _app_client() as client:
            csrf = await _login(client)
            await client.post(
                "/admin/killswitch/toggle",
                data={"csrf_token": csrf, "scope": "global", "tenant_id": settings.tenant_id},
            )
        assert await redis.get(key) is None
    finally:
        await redis.delete(key)
        await redis.aclose()
