import uuid
from datetime import UTC, datetime, timedelta

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


async def test_accounts_page_renders_five_channel_tiles(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": True,
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "meta_comment_reply_enabled": True,
            "meta_auto_reply_enabled": True,
            "whatsapp_enabled": True,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")

    assert response.status_code == 200
    html = response.text
    assert "添加渠道" in html
    for channel, label in (
        ("x", "X"),
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("telegram", "Telegram"),
        ("whatsapp", "WhatsApp"),
    ):
        assert f'data-channel="{channel}"' in html
        assert f"/static/channel-icons/{channel}.svg" in html
        assert f'aria-label="连接 {label}"' in html
    assert 'id="channel-setup"' not in html
    assert 'action="/admin/oauth/x/start"' not in html
    assert 'action="/admin/connect/telegram"' not in html


async def test_accounts_page_renders_oauth_channel_panels(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": True,
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "meta_comment_reply_enabled": True,
            "meta_auto_reply_enabled": True,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        x_page = await client.get("/admin/accounts?connect=x")
        facebook_page = await client.get("/admin/accounts?connect=facebook")
        instagram_page = await client.get("/admin/accounts?connect=instagram")

    assert 'action="/admin/oauth/x/start"' in x_page.text
    assert "XChat 4 位 PIN" in x_page.text
    assert "/admin/oauth/x/callback" in x_page.text
    assert 'action="/admin/connect/x"' in x_page.text

    assert 'action="/admin/oauth/meta/start"' in facebook_page.text
    assert 'name="platform" value="facebook"' in facebook_page.text
    assert "pages_messaging" in facebook_page.text
    assert "pages_read_user_content" in facebook_page.text
    assert 'name="enable_comments" value="true"' in facebook_page.text
    assert 'name="automation_default" value="BOT_ACTIVE"' in facebook_page.text
    assert 'action="/admin/connect/meta"' in facebook_page.text

    assert 'action="/admin/oauth/instagram/start"' in instagram_page.text
    assert 'action="/admin/oauth/meta/start"' in instagram_page.text
    assert 'name="platform" value="instagram"' in instagram_page.text
    assert "不需要关联 Facebook Page" in instagram_page.text
    assert "适用于已关联 Facebook Page" in instagram_page.text
    assert 'name="page_id"' in instagram_page.text
    assert "instagram_business_manage_comments" in instagram_page.text
    assert "instagram_manage_comments" in instagram_page.text
    assert 'name="enable_comments" value="true"' in instagram_page.text
    assert 'name="automation_default" value="BOT_ACTIVE"' in instagram_page.text


async def test_accounts_page_renders_manual_channel_panels(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"whatsapp_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        telegram_page = await client.get("/admin/accounts?connect=telegram")
        whatsapp_page = await client.get("/admin/accounts?connect=whatsapp")

    assert 'action="/admin/connect/telegram"' in telegram_page.text
    assert "@BotFather" in telegram_page.text
    assert 'name="token"' in telegram_page.text
    assert 'action="/admin/connect/whatsapp"' in whatsapp_page.text
    assert "Phone Number ID" in whatsapp_page.text
    assert 'name="access_token"' in whatsapp_page.text
    assert 'name="verify_token"' in whatsapp_page.text


async def test_meta_account_automation_only_converges_to_draft_while_switch_is_off(
    session, migrated_db
):
    account_id = uuid.uuid4()
    legacy_active_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="instagram",
            name="@shop",
            external_account_id="ig-1",
            public_id=f"ig_{uuid.uuid4().hex}",
            config={"meta_health_status": "READY"},
            capability={"dm": True, "comments": False, "max_text_length": 1000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=legacy_active_id,
            tenant_id="default",
            brand_id="default",
            platform="facebook",
            name="Legacy Page",
            external_account_id="page-legacy",
            public_id=f"fb_{uuid.uuid4().hex}",
            config={"meta_health_status": "READY"},
            capability={"dm": True, "comments": False, "max_text_length": 2000},
            automation_default="BOT_ACTIVE",
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/accounts")
        assert f'action="/admin/accounts/{account_id}/automation"' not in page.text
        assert f'action="/admin/accounts/{legacy_active_id}/automation"' in page.text
        rejected = await client.post(
            f"/admin/accounts/{account_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE"},
        )
        converged = await client.post(
            f"/admin/accounts/{legacy_active_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_DRAFT_ONLY"},
        )
    assert rejected.status_code == 422
    assert converged.status_code == 303
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    legacy_active = await session.get(models.PlatformAccount, legacy_active_id)
    assert account.automation_default == "BOT_DRAFT_ONLY"
    assert legacy_active.automation_default == "BOT_DRAFT_ONLY"


async def test_meta_account_can_be_promoted_once_deployment_opts_in(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"meta_auto_reply_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="facebook",
            name="Page",
            external_account_id="page-optin",
            public_id=f"fb_{uuid.uuid4().hex}",
            config={"meta_health_status": "READY"},
            capability={"dm": True, "comments": False, "max_text_length": 2000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/accounts")
        assert f'action="/admin/accounts/{account_id}/automation"' in page.text
        promoted = await client.post(
            f"/admin/accounts/{account_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE"},
        )
    assert promoted.status_code == 303
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.automation_default == "BOT_ACTIVE"
    entry = (
        await session.execute(
            select(models.AuditLog).where(
                models.AuditLog.subject_id == str(account_id),
                models.AuditLog.action == "SET_AUTOMATION_DEFAULT",
            )
        )
    ).scalar_one()
    assert entry.detail == {
        "from": "BOT_DRAFT_ONLY",
        "to": "BOT_ACTIVE",
        "platform": "facebook",
    }


async def test_accounts_page_disables_future_platform_tiles_when_flagged_off(
    migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "facebook_messenger_enabled": False,
            "instagram_messaging_enabled": False,
            "whatsapp_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")
        disabled = await client.get("/admin/accounts?connect=instagram")

    assert response.status_code == 200
    for channel in ("facebook", "instagram", "whatsapp"):
        assert f'data-channel="{channel}" role="listitem" aria-disabled="true"' in response.text
        assert f'href="/admin/accounts?connect={channel}' not in response.text
    assert 'action="/admin/oauth/meta/start"' not in response.text
    assert 'action="/admin/oauth/instagram/start"' not in response.text
    assert 'action="/admin/connect/whatsapp"' not in response.text
    assert "该渠道尚未在当前部署启用" in disabled.text
    assert 'id="channel-setup"' not in disabled.text


async def test_accounts_page_disables_x_tile_when_all_stacks_are_off(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": False,
            "x_activity_enabled": False,
            "xchat_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")
        disabled = await client.get("/admin/accounts?connect=x")

    assert response.status_code == 200
    assert 'data-channel="x" role="listitem" aria-disabled="true"' in response.text
    assert 'href="/admin/accounts?connect=x' not in response.text
    assert 'action="/admin/oauth/x/start"' not in response.text
    assert 'action="/admin/connect/x"' not in response.text
    assert "XChat 4 位 PIN" not in disabled.text
    assert "该渠道尚未在当前部署启用" in disabled.text


async def test_accounts_page_renders_x_oauth_result_banner(migrated_db):
    async with _app_client() as client:
        await _login(client)
        connected = await client.get("/admin/accounts?provider=x&status=connected")
        processing = await client.get(
            "/admin/accounts?provider=x&status=processing&code=provisioning_in_progress"
        )
        failed = await client.get(
            "/admin/accounts?provider=x&status=error&code=x_token_exchange_rejected"
        )
    assert "X 账号授权并连接成功" in connected.text
    assert "正在后台完成" in processing.text
    assert "x_token_exchange_rejected" in failed.text


async def test_accounts_page_shows_independent_x_transport_states(session, migrated_db):
    import uuid

    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            name="@xbot",
            external_account_id="x-1",
            public_id="x-public-state",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            config={
                "xchat_registered": True,
                "xchat_key_state": "RECOVERY_REQUIRED",
                "x_activity_subscriptions": {
                    "dm.received": {"status": "ACTIVE"},
                    "chat.received": {"status": "ACTIVE"},
                },
            },
            capability={"dm": True, "x_chat": False, "mentions": True},
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")

    assert response.status_code == 200
    assert "Legacy DM" in response.text
    assert "DM Activity" in response.text
    assert "XChat Key" in response.text
    assert "RECOVERY_REQUIRED" in response.text
    assert f'action="/admin/accounts/{account_id}/xchat"' in response.text
    assert "恢复 XChat 密钥" in response.text


async def test_xchat_activation_error_renders_operator_notice(session, migrated_db, monkeypatch):
    import uuid

    from social_reply.application.account_management import admin_console
    from social_reply.application.account_management.xchat_activation import (
        XChatActivationError,
    )

    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            name="@xbot",
            external_account_id="x-1",
            public_id="x-public",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            config={"delivery_mode": "direct"},
            capability={"dm": True, "x_chat": False},
            status="active",
        )
    )
    await session.commit()

    async def fail_activation(**kwargs):
        raise XChatActivationError(
            "XCHAT_DM_PERMISSION_REQUIRED",
            "请配置 Read and write and Direct message。",
        )

    monkeypatch.setattr(admin_console, "enable_xchat_for_account", fail_activation)

    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/accounts/{account_id}/xchat",
            data={"csrf_token": csrf, "xchat_pin": "1234"},
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "XCHAT_DM_PERMISSION_REQUIRED" in response.text
    assert "Read and write and Direct message" in response.text
    assert "1234" not in response.text


async def test_pin_provisioning_job_requires_secret_resubmission(session, migrated_db):
    import uuid

    job_id = uuid.uuid4()
    await session.execute(
        insert(models.ProvisioningJob).values(
            id=job_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            actor="user:admin",
            idempotency_key="pin-resubmit",
            request={"environment": "oauth"},
            staging_secret=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            status="NEEDS_ACTION",
            current_step="FAILED",
            result={
                "requires_secret_resubmission": True,
                "required_secret": "xchat_pin",
            },
            last_error_code="XCHAT_PIN_INVALID",
            last_error_message="PIN 不正确",
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get(f"/admin/jobs/{job_id}")
        retry = await client.post(
            f"/admin/jobs/{job_id}/retry",
            data={"csrf_token": csrf},
        )

    assert page.status_code == 200
    assert "返回账号页重新提交 PIN 或凭证" in page.text
    assert f'action="/admin/jobs/{job_id}/retry"' not in page.text
    assert retry.status_code == 409
    assert retry.json()["detail"] == "provisioning_secret_resubmission_required"


async def test_retryable_provisioning_job_renders_as_processing(session, migrated_db):
    import uuid

    job_id = uuid.uuid4()
    await session.execute(
        insert(models.ProvisioningJob).values(
            id=job_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            actor="user:admin",
            idempotency_key="scheduled-retry",
            request={"environment": "oauth"},
            staging_secret=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            status="FAILED",
            current_step="FAILED",
            next_attempt_at=datetime.now(UTC) + timedelta(minutes=1),
            last_error_code="PLATFORM_TEMPORARY_ERROR",
            last_error_message="temporary",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        page = await client.get(f"/admin/jobs/{job_id}")
        accounts = await client.get("/admin/accounts")

    assert page.status_code == 200
    assert "PROCESSING" in page.text
    assert "每 4 秒自动刷新" in page.text
    assert f'action="/admin/jobs/{job_id}/retry"' not in page.text
    assert "PROCESSING" in accounts.text


async def test_stalled_provisioning_retry_renders_as_failed(session, migrated_db):
    import uuid

    job_id = uuid.uuid4()
    await session.execute(
        insert(models.ProvisioningJob).values(
            id=job_id,
            tenant_id="default",
            brand_id="b1",
            platform="x",
            actor="user:admin",
            idempotency_key="stalled-retry",
            request={"environment": "oauth"},
            staging_secret=encrypt_secret_bundle(
                {
                    "consumer_key": "ck",
                    "consumer_secret": "cs",
                    "access_token": "at",
                    "access_token_secret": "ats",
                }
            ),
            status="FAILED",
            current_step="FAILED",
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
            last_error_code="PLATFORM_TEMPORARY_ERROR",
            last_error_message="temporary",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        page = await client.get(f"/admin/jobs/{job_id}")
        accounts = await client.get("/admin/accounts")

    assert page.status_code == 200
    assert "FAILED" in page.text
    assert "每 4 秒自动刷新" not in page.text
    assert f'action="/admin/jobs/{job_id}/retry"' in page.text
    assert "FAILED" in accounts.text


async def test_conversation_state_flip_takeover(session, migrated_db):
    # 构造一个 BOT_ACTIVE 会话，验证人工接管把状态翻到 HUMAN_ACTIVE
    account_id, contact_id, conv_id, outbox_id = (
        __import__("uuid").uuid4(),
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
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            conversation_id=conv_id,
            platform_account_id=account_id,
            destination_type="telegram_message",
            destination_id="telegram:x:u1",
            message_type="text",
            payload={"text": "pending"},
            idempotency_key=str(outbox_id),
            status="PENDING",
        )
    )
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
    session.expire_all()
    outbox = await session.get(models.OutboxMessage, outbox_id)
    audit = (
        await session.execute(
            select(models.AuditLog).where(
                models.AuditLog.category == "state_transition",
                models.AuditLog.subject_id == str(conv_id),
            )
        )
    ).scalar_one()
    assert outbox.status == "CANCELLED"
    assert outbox.last_error_code == "TAKEOVER"
    assert audit.action == "HUMAN_ACTIVE"
    assert audit.detail == {"reason": "admin_manual"}


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


async def test_knowledge_csv_import_via_console(session, migrated_db, monkeypatch):
    from social_reply.application.reply_decision import runner
    from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(runner, "_embedder", FakeEmbeddingClient())
    csv_body = (
        "question,reply,category\n"
        "怎么退款,3-5 个工作日原路退回,售后\n"
        "发货多久,48 小时内发货,物流\n"
        ",\n"
    )

    async with _app_client() as client:
        csrf = await _login(client)
        resp = await client.post(
            "/admin/knowledge/import",
            data={"csrf_token": csrf, "tenant_id": "default", "brand_id": "default"},
            files={"file": ("templates.csv", csv_body.encode("utf-8"), "text/csv")},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "notice=imported" in loc
        assert "inserted=2" in loc
        assert "skipped=0" in loc
        assert "blank=1" in loc

    docs = (await session.execute(select(models.KnowledgeDocument))).scalars().all()
    assert len(docs) == 2
    chunks = (await session.execute(select(models.KnowledgeChunk))).scalars().all()
    assert len(chunks) == 2
    assert all(len(c.embedding) == 1536 for c in chunks)
    assert all(d.source_file == "templates.csv" for d in docs)

    # 重复上传：全部 skipped，不新增
    async with _app_client() as client:
        csrf = await _login(client)
        resp = await client.post(
            "/admin/knowledge/import",
            data={"csrf_token": csrf, "tenant_id": "default"},
            files={"file": ("templates.csv", csv_body.encode("utf-8"), "text/csv")},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "notice=imported" in loc
        assert "inserted=0" in loc
        assert "skipped=2" in loc
    docs2 = (await session.execute(select(models.KnowledgeDocument))).scalars().all()
    assert len(docs2) == 2


async def test_knowledge_csv_import_bad_header(session, migrated_db, monkeypatch):
    from social_reply.application.reply_decision import runner
    from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(runner, "_embedder", FakeEmbeddingClient())
    async with _app_client() as client:
        csrf = await _login(client)
        resp = await client.post(
            "/admin/knowledge/import",
            data={"csrf_token": csrf, "tenant_id": "default"},
            files={"file": ("bad.csv", b"q,a\nx,y\n", "text/csv")},
        )
        assert resp.status_code == 303
        assert "notice=import_bad_csv" in resp.headers["location"]

    page = None
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/admin/knowledge?notice=import_bad_csv")
    assert page is not None and page.status_code == 200
    assert "CSV 无效" in page.text


async def test_knowledge_csv_import_rejects_bad_tenant_and_csrf(migrated_db, monkeypatch):
    from social_reply.application.reply_decision import runner
    from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(runner, "_embedder", FakeEmbeddingClient())
    payload = b"question,reply\nq1,r1\n"
    async with _app_client() as client:
        csrf = await _login(client)
        bad_tenant = await client.post(
            "/admin/knowledge/import",
            data={"csrf_token": csrf, "tenant_id": "not-allowed"},
            files={"file": ("t.csv", payload, "text/csv")},
        )
        assert bad_tenant.status_code == 403

        no_csrf = await client.post(
            "/admin/knowledge/import",
            data={"csrf_token": "wrong", "tenant_id": "default"},
            files={"file": ("t.csv", payload, "text/csv")},
        )
        assert no_csrf.status_code == 403


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
