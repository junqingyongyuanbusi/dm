import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select, update

from apps.api.main import create_app
from social_reply.application.account_management.auth import hash_password
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
        data={
            "csrf_token": csrf,
            "username": "admin",
            "password": "test-admin-password",
        },
    )
    return csrf


async def _login_superadmin(client: httpx.AsyncClient) -> str:
    return await _login(client)


def _app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _make_knowledge_publishable(session, document: models.KnowledgeDocument) -> None:
    document.source_language = "en"
    document.detected_language = "en"
    document.language_detection_status = "english"
    document.language_verified = True
    content_hash = hashlib.sha256(
        f"{document.tenant_id}:{document.id}:{document.question}".encode()
    ).hexdigest()
    session.add(
        models.KnowledgeChunk(
            tenant_id=document.tenant_id,
            document_id=document.id,
            content=f"Question: {document.question}\nAnswer: {document.reply}",
            embed_text=document.question,
            content_hash=content_hash,
            embedding_version="text-embedding-3-small",
            embedding=[0.01] * 1536,
        )
    )


async def _seed_inbox_conversation(
    session,
    *,
    suffix: str,
    display_name: str,
    work_created_at: datetime,
    platform: str = "telegram",
    channel_type: str = "dm",
    reply_target: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    account_id, contact_id, conversation_id, message_id, work_item_id = (
        uuid.uuid4() for _ in range(5)
    )
    capability = {"dm": True, "max_text_length": 4096}
    if platform in {"facebook", "instagram"}:
        capability = {"dm": True, "comments": True, "max_text_length": 2000}
    elif platform == "x":
        capability = {
            "dm": True,
            "x_chat": False,
            "mentions": True,
            "max_text_length": 280,
        }
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            brand_id="b1",
            platform=platform,
            name=f"Inbox account {suffix}",
            public_id=f"inbox-{suffix}",
            credential_bundle=encrypt_secret_bundle({"bot_token": "token"}),
            config={"delivery_mode": "direct"},
            capability=capability,
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            platform=platform,
            platform_account_id=account_id,
            external_user_id=f"user-{suffix}",
            display_name=display_name,
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            brand_id="b1",
            platform=platform,
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"{platform}:{suffix}:user",
            channel_type=channel_type,
            decision_generation=0,
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id,
            state="HANDOFF_PENDING",
            state_version=1,
            state_changed_reason="LLM_UNAVAILABLE",
        )
    )
    await session.execute(
        insert(models.Message).values(
            id=message_id,
            conversation_id=conversation_id,
            direction="inbound",
            sender_type="contact",
            text=f"Message {suffix}",
            reply_target=reply_target or {"chat_id": suffix},
            occurred_at=work_created_at,
            decision_generation=0,
        )
    )
    await session.execute(
        insert(models.HumanWorkItem).values(
            id=work_item_id,
            tenant_id="default",
            conversation_id=conversation_id,
            status="WAITING",
            reason_code="LLM_UNAVAILABLE",
            created_at=work_created_at,
            version=1,
        )
    )
    return account_id, conversation_id, message_id, work_item_id


async def test_console_pages_require_login():
    async with _app_client() as client:
        for path in (
            "/admin",
            "/admin/inbox",
            "/admin/conversations",
            "/admin/decisions",
            "/admin/knowledge",
            "/admin/delivery",
            "/admin/accounts",
            "/admin/health",
        ):
            resp = await client.get(path)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/auth/login"


async def test_console_pages_render_after_login(migrated_db):
    async with _app_client() as client:
        await _login(client)
        root_response = await client.get("/admin")
        assert root_response.status_code == 303
        assert root_response.headers["location"] == "/admin/system/overview"
        for legacy_path, canonical_path in (
            ("/admin/inbox", "/app/t/default/inbox"),
            ("/admin/conversations", "/app/t/default/conversations"),
            ("/admin/health", "/app/t/default/health"),
        ):
            legacy_response = await client.get(legacy_path)
            assert legacy_response.status_code == 303, legacy_path
            assert legacy_response.headers["location"] == canonical_path
            canonical_response = await client.get(canonical_path)
            assert canonical_response.status_code == 200, canonical_path
            assert "admin" in canonical_response.text
            assert 'href="/admin/system/overview"' in canonical_response.text
            assert canonical_response.text.count('href="/admin') == 1
        legacy_accounts = await client.get("/admin/accounts")
        legacy_knowledge = await client.get("/admin/knowledge")

    assert legacy_accounts.status_code == 303
    assert legacy_accounts.headers["location"] == "/app/t/default/channels"
    assert legacy_knowledge.status_code == 303
    assert legacy_knowledge.headers["location"] == "/app/t/default/knowledge"


async def test_representative_console_pages_render_in_english(migrated_db):
    async with _app_client() as client:
        await _login(client)
        client.cookies.set("reply_ui_locale", "en")
        for path in (
            "/app/t/default",
            "/app/t/default/inbox",
            "/app/t/default/conversations",
            "/app/t/default/knowledge",
            "/app/t/default/agents/default/instructions",
            "/app/t/default/channels",
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert '<html lang="en"' in response.text
            assert 'href="/admin/system/overview"' in response.text
            assert response.text.count('href="/admin') == 1
            for chinese_product_copy in (
                "自动回复运行状况",
                "统一工作队列",
                "按渠道浏览",
                "回复模板管理",
                "业务 Prompt 已保存",
                "添加渠道",
                "只读查看核心处理链路",
                "集中管理租户级全局急停",
            ):
                assert chinese_product_copy not in response.text, (
                    path,
                    chinese_product_copy,
                )

    async with _app_client() as client:
        await _login_superadmin(client)
        client.cookies.set("reply_ui_locale", "en")
        for path, marker in (
            ("/admin/system/health", "Read-only view"),
            ("/admin/system/safety", "Manage tenant-wide global kill switches"),
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert '<html lang="en"' in response.text
            assert marker in response.text


async def test_conversation_detail_localizes_controls_but_preserves_customer_facts(
    session,
    migrated_db,
):
    _account_id, conversation_id, _message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="localized-detail",
        display_name="双语客户事实",
        work_created_at=datetime.now(UTC),
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        client.cookies.set("reply_ui_locale", "en")
        response = await client.get(f"/app/t/default/conversations/{conversation_id}")

    assert response.status_code == 200
    assert "Send human reply" in response.text
    assert "双语客户事实" in response.text
    assert "Message localized-detail" in response.text
    assert "返回收件箱" not in response.text
    assert "消息线程" not in response.text


async def test_grouped_navigation_uses_new_information_architecture(migrated_db):
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/app/t/default/inbox")

    assert page.status_code == 200
    for system_path in (
        "/admin/system/health",
        "/admin/system/safety",
        "/admin/system/users",
    ):
        assert f'href="{system_path}"' not in page.text
    for legacy_account_path in (
        "/admin/integrations/accounts",
        "/admin/accounts",
        "/admin/feishu-handoff",
    ):
        assert f'href="{legacy_account_path}"' not in page.text
    assert 'href="#main-content">跳到主要内容</a>' in page.text
    assert 'href="/admin/system/overview"' in page.text
    assert page.text.count('href="/admin') == 1


async def test_system_console_slim_role_landings_and_legacy_get_redirects(
    session,
    migrated_db,
):
    session.add(
        models.AdminUser(
            username="default-user",
            password_hash=await hash_password("default-user-password-123"),
            tenant_id="default",
            role="USER",
            must_change_password=False,
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as admin_client:
        await _login(admin_client)
        root = await admin_client.get("/admin")
        assert root.status_code == 303
        assert root.headers["location"] == "/admin/system/overview"

        expected_redirects = {
            "/admin/inbox": "/app/t/default/inbox",
            "/admin/conversations": "/app/t/default/conversations",
            "/admin/content/knowledge": "/app/t/default/knowledge",
            "/admin/integrations/accounts": "/app/t/default/channels",
            "/admin/health": "/app/t/default/health",
        }
        for legacy_path, canonical_path in expected_redirects.items():
            response = await admin_client.get(legacy_path)
            assert response.status_code == 303, legacy_path
            assert response.headers["location"] == canonical_path

        settings = await admin_client.get("/app/t/default/settings")
        health = await admin_client.get("/app/t/default/health")

    assert settings.status_code == 200
    for canonical_link in (
        "/app/t/default/agents/default/instructions",
        "/app/t/default/knowledge",
        "/app/t/default/channels",
        "/app/t/default/channels/feishu/handoff",
        "/app/t/default/health",
        "/app/t/default/audit",
        "/app/t/default/journeys",
    ):
        assert f'href="{canonical_link}"' in settings.text
    assert "/admin/content" not in settings.text
    assert "/admin/integrations" not in settings.text
    assert "尚未持久化" in settings.text
    assert health.status_code == 200
    assert "ingestion" in health.text
    assert "/admin/content" not in health.text
    assert "/admin/integrations" not in health.text

    async with _app_client() as superadmin_client:
        await _login_superadmin(superadmin_client)
        root = await superadmin_client.get("/admin")
        legacy_health = await superadmin_client.get("/admin/system/health")
        tenant_health = await superadmin_client.get("/app/t/default/health")

    assert root.status_code == 303
    assert root.headers["location"] == "/admin/system/overview"
    assert legacy_health.status_code == 200
    assert tenant_health.status_code == 200

    async with _app_client() as user_client:
        await user_client.get("/admin/login")
        csrf = user_client.cookies["reply_admin_csrf"]
        login = await user_client.post(
            "/admin/login",
            data={
                "csrf_token": csrf,
                "username": "default-user",
                "password": "default-user-password-123",
            },
        )
        assert login.status_code == 303
        admin_root = await user_client.get("/admin")
        settings = await user_client.get("/app/t/default/settings")
        health = await user_client.get("/app/t/default/health")
        system_overview = await user_client.get("/admin/system/overview")

    for response in (admin_root, settings, health, system_overview):
        assert response.status_code == 403


async def test_new_page_routes_render_and_legacy_routes_remain_available(migrated_db):
    async with _app_client() as client:
        await _login(client)
        current_knowledge = await client.get("/admin/content/knowledge")
        legacy_knowledge = await client.get("/admin/knowledge")
        canonical_prompt = await client.get("/app/t/default/agents/default/instructions")
        legacy_prompt_responses = [
            await client.get(path)
            for path in (
                "/admin/content/reply-prompt",
                "/admin/content/brand-voice",
                "/admin/prompt",
            )
        ]
        current_accounts = await client.get("/admin/integrations/accounts")
        legacy_accounts = await client.get("/admin/accounts")
        system_health = await client.get("/admin/system/health")
        legacy_health = await client.get("/admin/health")

    assert current_knowledge.status_code == 303
    assert legacy_knowledge.status_code == 303
    assert current_knowledge.headers["location"] == "/app/t/default/knowledge"
    assert legacy_knowledge.headers["location"] == "/app/t/default/knowledge"
    assert canonical_prompt.status_code == 200
    assert "业务 Prompt" in canonical_prompt.text
    for response in legacy_prompt_responses:
        assert response.status_code == 303
        assert response.headers["location"] == ("/app/t/default/agents/default/instructions")

    for response in (current_accounts, legacy_accounts):
        assert response.status_code == 303
        assert response.headers["location"] == "/app/t/default/channels"

    assert system_health.status_code == 200
    assert "系统健康" in system_health.text
    assert legacy_health.status_code == 303
    assert legacy_health.headers["location"] == "/app/t/default/health"

    async with _app_client() as client:
        await _login_superadmin(client)
        system_health = await client.get("/admin/system/health")
        legacy_health = await client.get("/admin/health")

    assert system_health.status_code == 200
    assert "系统健康" in system_health.text
    assert legacy_health.status_code == 303
    assert legacy_health.headers["location"] == "/app/t/default/health"


async def test_account_connection_routes_are_deep_linkable(migrated_db):
    async with _app_client() as client:
        await _login(client)
        index = await client.get("/admin/integrations/accounts")
        telegram = await client.get("/admin/integrations/accounts/new/telegram")
        missing = await client.get("/admin/integrations/accounts/new/not-a-provider")

    for response in (index, telegram, missing):
        assert response.status_code == 303
        assert response.headers["location"] == "/app/t/default/channels"


async def test_navigation_does_not_poll_inbox_counts(migrated_db):
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/app/t/default/inbox")
        counts = await client.get("/admin/inbox/counts")

    assert page.status_code == 200
    assert "data-inbox-count" not in page.text
    assert "nav-queues" not in page.text
    assert "/admin/inbox/counts" not in page.text
    assert "refreshInboxCounts" not in page.text
    assert "setInterval(" not in page.text
    assert counts.status_code == 200
    assert counts.json() == {"human": 0, "drafts": 0, "delivery": 0}


async def test_legacy_decisions_and_delivery_pages_redirect_after_login(migrated_db):
    async with _app_client() as client:
        await _login(client)
        decisions = await client.get("/admin/decisions")
        delivery = await client.get("/admin/delivery")

    assert decisions.status_code == 303
    assert decisions.headers["location"] == "/admin/inbox?queue=drafts"
    assert delivery.status_code == 303
    assert delivery.headers["location"] == "/admin/inbox?queue=delivery"


async def test_health_page_is_read_only(migrated_db):
    async with _app_client() as client:
        await _login(client)
        legacy_response = await client.get("/admin/health")
        response = await client.get("/app/t/default/health")

    assert legacy_response.status_code == 303
    assert legacy_response.headers["location"] == "/app/t/default/health"
    assert response.status_code == 200
    main = re.search(r"<main[^>]*>(.*)</main>", response.text, re.DOTALL)
    assert main is not None
    assert "系统健康" in main.group(1)
    assert "<form" not in main.group(1)
    assert 'method="post"' not in main.group(1)
    assert "csrf_token" not in main.group(1)


async def test_inbox_combines_queues_and_sorts_oldest_waiting_first(session, migrated_db):
    now = datetime.now(UTC)
    old_account, old_conversation, old_message, _old_work = await _seed_inbox_conversation(
        session,
        suffix="old",
        display_name="Old customer",
        work_created_at=now - timedelta(hours=3),
    )
    _new_account, _new_conversation, _new_message, new_work = await _seed_inbox_conversation(
        session,
        suffix="new",
        display_name="New customer",
        work_created_at=now - timedelta(minutes=5),
    )
    newest_work = await session.get(models.HumanWorkItem, new_work)
    newest_work.reason_code = "RISK_WORD"
    newest_work.status = "CLAIMED"
    newest_work.assigned_actor = "user:another-agent"
    newest_work.claimed_at = now
    decision_id, outbox_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.ReplyDecision).values(
            id=decision_id,
            tenant_id="default",
            conversation_id=old_conversation,
            message_id=old_message,
            action="draft",
            reply_text="Original draft",
            original_reply_text="Original draft",
            review_action="PENDING",
            reason_codes=["INSUFFICIENT_KNOWLEDGE"],
            source="rule",
            decision_generation=0,
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="default",
            conversation_id=old_conversation,
            platform_account_id=old_account,
            destination_type="telegram_dm",
            destination_id="telegram:old:user",
            message_type="text",
            payload={"text": "uncertain", "target": {"chat_id": "old"}},
            reply_to_message_id=old_message,
            origin_kind="MANUAL_REPLY",
            actor_kind="ADMIN_HUMAN",
            actor_id="user:admin",
            idempotency_key=f"inbox-{outbox_id}",
            status="NEEDS_REVIEW",
            last_error_code="AMBIGUOUS_SEND",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        human = await client.get("/app/t/default/inbox")
        drafts = await client.get(
            f"/app/t/default/inbox?queue=drafts&item_id={decision_id}"
        )
        delivery = await client.get(
            f"/app/t/default/inbox?queue=delivery&item_id={outbox_id}"
        )
        filtered = await client.get(
            "/app/t/default/inbox?queue=human&reason=LLM_UNAVAILABLE"
        )

    assert human.status_code == 200
    assert '<meta http-equiv="refresh"' not in human.text
    assert '<meta http-equiv="refresh"' not in drafts.text
    assert human.text.index("Old customer") < human.text.index("New customer")
    assert "Original draft" in drafts.text
    assert f'action="/app/t/default/decisions/{decision_id}/approve"' in drafts.text
    assert "AMBIGUOUS_SEND" in delivery.text
    assert "Old customer" in filtered.text
    assert "New customer" in filtered.text


async def test_channel_filter_applies_to_all_inbox_queues_and_conversations(session, migrated_db):
    now = datetime.now(UTC)
    specs = (
        {
            "suffix": "channel-dm",
            "display_name": "Channel DM customer",
            "platform": "telegram",
            "channel_type": "dm",
            "reply_target": {"chat_id": "dm-user"},
            "destination_type": "telegram_dm",
        },
        {
            "suffix": "channel-comment",
            "display_name": "Channel comment customer",
            "platform": "facebook",
            "channel_type": "comment",
            "reply_target": {"kind": "comment", "comment_id": "comment-1"},
            "destination_type": "meta_public_comment",
        },
        {
            "suffix": "channel-mention",
            "display_name": "Channel mention customer",
            "platform": "x",
            "channel_type": "mention",
            "reply_target": {"kind": "reply", "in_reply_to_post_id": "post-1"},
            "destination_type": "x_post_reply",
        },
    )
    seeded: dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = {}
    for offset, spec in enumerate(specs):
        account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
            session,
            suffix=spec["suffix"],
            display_name=spec["display_name"],
            work_created_at=now - timedelta(minutes=offset + 1),
            platform=spec["platform"],
            channel_type=spec["channel_type"],
            reply_target=spec["reply_target"],
        )
        decision_id, outbox_id = uuid.uuid4(), uuid.uuid4()
        await session.execute(
            insert(models.ReplyDecision).values(
                id=decision_id,
                tenant_id="default",
                conversation_id=conversation_id,
                message_id=message_id,
                action="draft",
                reply_text=f"Draft for {spec['suffix']}",
                original_reply_text=f"Draft for {spec['suffix']}",
                review_action="PENDING",
                reason_codes=["INSUFFICIENT_KNOWLEDGE"],
                source="rule",
                decision_generation=0,
            )
        )
        await session.execute(
            insert(models.OutboxMessage).values(
                id=outbox_id,
                tenant_id="default",
                conversation_id=conversation_id,
                platform_account_id=account_id,
                destination_type=spec["destination_type"],
                destination_id=f"destination:{spec['suffix']}",
                message_type="text",
                payload={"text": "retry", "target": spec["reply_target"]},
                reply_to_message_id=message_id,
                origin_kind="MANUAL_REPLY",
                actor_kind="ADMIN_HUMAN",
                actor_id="user:admin",
                idempotency_key=f"channel-filter-{outbox_id}",
                status="FAILED",
                last_error_code="SEND_ERROR",
            )
        )
        seeded[spec["channel_type"]] = (account_id, conversation_id, message_id)
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        inbox_pages = {
            (queue, channel): await client.get(
                "/app/t/default/inbox", params={"queue": queue, "channel": channel}
            )
            for queue in ("human", "drafts", "delivery")
            for channel in ("all", "dm", "comment")
        }
        conversation_pages = {
            channel: await client.get(
                "/app/t/default/conversations", params={"channel": channel}
            )
            for channel in ("all", "dm", "comment")
        }
        count_responses = {
            channel: await client.get("/admin/inbox/counts", params={"channel": channel})
            for channel in ("all", "dm", "comment")
        }
        facebook_conversations = await client.get(
            "/app/t/default/conversations", params={"platform": "facebook"}
        )
        mention_detail = await client.get(
            f"/app/t/default/conversations/{seeded['mention'][1]}"
        )

    all_names = {spec["display_name"] for spec in specs}
    dm_names = {"Channel DM customer"}
    comment_names = {"Channel comment customer", "Channel mention customer"}
    for queue in ("human", "drafts", "delivery"):
        for channel, expected_names in (
            ("all", all_names),
            ("dm", dm_names),
            ("comment", comment_names),
        ):
            page = inbox_pages[(queue, channel)]
            assert page.status_code == 200
            for name in expected_names:
                assert name in page.text
            assert 'data-filter-text="' in page.text

    for channel, expected_names in (
        ("all", all_names),
        ("dm", dm_names),
        ("comment", comment_names),
    ):
        page = conversation_pages[channel]
        assert page.status_code == 200
        for name in expected_names:
            assert name in page.text
        assert 'data-filter-text="' in page.text

    assert "自动化状态" not in conversation_pages["all"].text
    assert count_responses["all"].json() == {"human": 3, "drafts": 3, "delivery": 3}
    assert count_responses["dm"].json() == {"human": 1, "drafts": 1, "delivery": 1}
    assert count_responses["comment"].json() == {"human": 2, "drafts": 2, "delivery": 2}
    assert "Channel comment customer" in facebook_conversations.text
    assert "Channel DM customer" in facebook_conversations.text
    assert "Channel mention customer" in facebook_conversations.text
    assert 'data-filter-text="' in facebook_conversations.text

    assert mention_detail.status_code == 200
    assert "mention" in mention_detail.text
    assert 'name="reply_to_message_id"' in mention_detail.text
    assert f'value="{seeded["mention"][2]}"' in mention_detail.text


async def test_email_platform_filter_is_available_across_inbox_and_conversations(
    session, migrated_db
):
    await _seed_inbox_conversation(
        session,
        suffix="email-filter",
        display_name="Email filter customer",
        work_created_at=datetime.now(UTC),
        platform="email",
        channel_type="dm",
        reply_target={
            "kind": "email_reply",
            "message_id": "<message@example.com>",
            "to": ["customer@example.com"],
        },
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        inbox = await client.get("/app/t/default/inbox", params={"platform": "email"})
        conversations = await client.get(
            "/app/t/default/conversations", params={"platform": "email"}
        )
        counts = await client.get("/admin/inbox/counts", params={"platform": "email"})

    assert inbox.status_code == 200
    assert conversations.status_code == 200
    assert counts.status_code == 200
    assert "Email filter customer" in inbox.text
    assert "Email filter customer" in conversations.text
    assert 'data-filter-text="' in inbox.text
    assert 'data-filter-text="' in conversations.text
    assert counts.json()["human"] == 1


async def test_draft_queue_only_includes_reviewable_drafts(session, migrated_db):
    now = datetime.now(UTC)
    account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="reviewable-draft",
        display_name="Reviewable customer",
        work_created_at=now,
    )
    outbox_id = uuid.uuid4()
    queued_message_id, empty_message_id, stale_message_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    await session.execute(
        update(models.Conversation)
        .where(models.Conversation.id == conversation_id)
        .values(decision_generation=2)
    )
    await session.execute(
        update(models.Message)
        .where(models.Message.id == message_id)
        .values(decision_generation=2)
    )
    for candidate_id, text, generation in (
        (queued_message_id, "Already queued inbound", 2),
        (empty_message_id, "Empty draft inbound", 2),
        (stale_message_id, "Stale draft inbound", 1),
    ):
        await session.execute(
            insert(models.Message).values(
                id=candidate_id,
                conversation_id=conversation_id,
                direction="inbound",
                sender_type="contact",
                text=text,
                reply_target={"chat_id": "reviewable-draft"},
                occurred_at=now,
                decision_generation=generation,
            )
        )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=outbox_id,
            tenant_id="default",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="telegram:reviewable-draft:user",
            message_type="text",
            payload={"text": "Already queued", "target": {"chat_id": "reviewable-draft"}},
            reply_to_message_id=queued_message_id,
            origin_kind="DRAFT_APPROVAL",
            actor_kind="ADMIN_HUMAN",
            actor_id="user:admin",
            idempotency_key=f"reviewable-draft-{outbox_id}",
            status="SENT",
        )
    )
    for decision_message_id, reply_text, linked_outbox, generation in (
        (message_id, "Ready for review", None, 2),
        (queued_message_id, "Already queued", outbox_id, 2),
        (empty_message_id, "   ", None, 2),
        (stale_message_id, "Stale historical draft", None, 1),
    ):
        await session.execute(
            insert(models.ReplyDecision).values(
                id=uuid.uuid4(),
                tenant_id="default",
                conversation_id=conversation_id,
                message_id=decision_message_id,
                action="draft",
                reply_text=reply_text,
                original_reply_text=reply_text,
                review_action="PENDING",
                reason_codes=["INSUFFICIENT_KNOWLEDGE"],
                source="rule",
                decision_generation=generation,
                review_outbox_id=linked_outbox,
            )
        )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        page = await client.get("/app/t/default/inbox", params={"queue": "drafts"})
        counts = await client.get("/admin/inbox/counts")

    assert page.status_code == 200
    assert counts.json()["drafts"] == 1
    assert page.text.count('class="saas-work-item"') == 1


async def test_inbox_rejects_cross_tenant_join_mismatches(session, migrated_db):
    now = datetime.now(UTC)
    account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="tenant-mismatch",
        display_name="Leaked customer",
        work_created_at=now,
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            id=uuid.uuid4(),
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text="Leaked draft",
            original_reply_text="Leaked draft",
            review_action="PENDING",
            reason_codes=["INSUFFICIENT_KNOWLEDGE"],
            source="rule",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=uuid.uuid4(),
            tenant_id="default",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="telegram:tenant-mismatch:user",
            message_type="text",
            payload={"text": "Leaked delivery", "target": {"chat_id": "tenant-mismatch"}},
            reply_to_message_id=message_id,
            origin_kind="MANUAL_REPLY",
            actor_kind="ADMIN_HUMAN",
            actor_id="user:admin",
            idempotency_key=f"tenant-mismatch-{uuid.uuid4()}",
            status="FAILED",
            last_error_code="SEND_ERROR",
        )
    )
    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(tenant_id="other")
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        pages = [
            await client.get("/admin/inbox", params={"queue": queue})
            for queue in ("human", "drafts", "delivery")
        ]
        counts = await client.get("/admin/inbox/counts")

    assert all("Leaked customer" not in page.text for page in pages)
    assert counts.json() == {"human": 0, "drafts": 0, "delivery": 0}


@pytest.mark.parametrize("mismatch", ["tenant", "account"])
async def test_canonical_lists_do_not_render_mismatched_contact(
    session,
    migrated_db,
    mismatch,
):
    account_id, conversation_id, _message_id, _work_item_id = (
        await _seed_inbox_conversation(
            session,
            suffix=f"contact-list-{mismatch}",
            display_name="Safe contact",
            work_created_at=datetime.now(UTC),
        )
    )
    contact_id = await session.scalar(
        select(models.Conversation.contact_id).where(
            models.Conversation.id == conversation_id
        )
    )
    contact_values = {"display_name": "FOREIGN-CONTACT-SECRET"}
    if mismatch == "tenant":
        contact_values["tenant_id"] = "other"
    else:
        other_account_id = uuid.uuid4()
        await session.execute(
            insert(models.PlatformAccount).values(
                id=other_account_id,
                tenant_id="default",
                brand_id="default",
                platform="telegram",
                name="Other account",
                public_id=f"other-contact-account-{other_account_id}",
                config={"delivery_mode": "direct"},
                capability={"dm": True},
                automation_default="BOT_DRAFT_ONLY",
                status="active",
            )
        )
        contact_values["platform_account_id"] = other_account_id
    await session.execute(
        update(models.Contact)
        .where(models.Contact.id == contact_id)
        .values(**contact_values)
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        inbox_page = await client.get("/app/t/default/inbox")
        conversations_page = await client.get("/app/t/default/conversations")

    assert inbox_page.status_code == 200
    assert conversations_page.status_code == 200
    assert "FOREIGN-CONTACT-SECRET" not in inbox_page.text
    assert "FOREIGN-CONTACT-SECRET" not in conversations_page.text
    assert str(account_id) not in conversations_page.text


@pytest.mark.parametrize(
    ("mismatch", "expected_relation"),
    [
        ("contact_tenant", "contact"),
        ("contact_account", "contact"),
        ("reply_decision", None),
        ("outbox_tenant", None),
        ("outbox_account", None),
        ("message_source_outbox", "message_source_outbox"),
        ("audit_log", None),
    ],
)
async def test_conversation_detail_fails_closed_on_tenant_mismatch(
    session, migrated_db, caplog, mismatch, expected_relation
):
    now = datetime.now(UTC)
    account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix=f"detail-{mismatch}",
        display_name=f"Safe customer {mismatch}",
        work_created_at=now,
    )
    local_decision_text = f"LOCAL-{mismatch}-DECISION"
    local_outbox_text = f"LOCAL-{mismatch}-OUTBOX"
    await session.execute(
        insert(models.ReplyDecision).values(
            id=uuid.uuid4(),
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text=local_decision_text,
            original_reply_text=local_decision_text,
            review_action="PENDING",
            reason_codes=[],
            source="rule",
        )
    )
    await session.execute(
        insert(models.OutboxMessage).values(
            id=uuid.uuid4(),
            tenant_id="default",
            conversation_id=conversation_id,
            platform_account_id=account_id,
            destination_type="telegram_dm",
            destination_id="telegram:local:user",
            message_type="text",
            payload={"text": local_outbox_text, "target": {"chat_id": "local"}},
            reply_to_message_id=message_id,
            origin_kind="MANUAL_REPLY",
            actor_kind="ADMIN_HUMAN",
            actor_id="user:admin",
            idempotency_key=f"local-{uuid.uuid4()}",
            status="SENT",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        healthy_detail = await client.get(
            f"/app/t/default/conversations/{conversation_id}"
        )

        foreign_secret = f"FOREIGN-{mismatch}-SECRET"
        if mismatch in {"contact_tenant", "contact_account"}:
            contact_id = await session.scalar(
                select(models.Conversation.contact_id).where(
                    models.Conversation.id == conversation_id
                )
            )
            contact_values = {"display_name": foreign_secret}
            if mismatch == "contact_tenant":
                contact_values["tenant_id"] = "other"
            else:
                other_account_id = uuid.uuid4()
                await session.execute(
                    insert(models.PlatformAccount).values(
                        id=other_account_id,
                        tenant_id="default",
                        brand_id="b1",
                        platform="telegram",
                        name="Other local account",
                        public_id=f"other-local-{uuid.uuid4()}",
                        credential_bundle=encrypt_secret_bundle({"bot_token": "token"}),
                        config={"delivery_mode": "direct"},
                        capability={"dm": True, "max_text_length": 4096},
                        automation_default="BOT_DRAFT_ONLY",
                        status="active",
                    )
                )
                contact_values["platform_account_id"] = other_account_id
            await session.execute(
                update(models.Contact)
                .where(models.Contact.id == contact_id)
                .values(**contact_values)
            )
        elif mismatch == "reply_decision":
            await session.execute(
                insert(models.ReplyDecision).values(
                    id=uuid.uuid4(),
                    tenant_id="other",
                    conversation_id=conversation_id,
                    message_id=None,
                    action="draft",
                    intent=foreign_secret,
                    reply_text=foreign_secret,
                    original_reply_text=foreign_secret,
                    review_action="PENDING",
                    reason_codes=[foreign_secret],
                    source="rule",
                )
            )
        elif mismatch in {"outbox_tenant", "outbox_account"}:
            outbox_tenant = "other" if mismatch == "outbox_tenant" else "default"
            outbox_account_id = account_id
            if mismatch == "outbox_account":
                outbox_account_id = uuid.uuid4()
                await session.execute(
                    insert(models.PlatformAccount).values(
                        id=outbox_account_id,
                        tenant_id="default",
                        brand_id="b1",
                        platform="telegram",
                        name="Other outbox account",
                        public_id=f"other-outbox-{uuid.uuid4()}",
                        credential_bundle=encrypt_secret_bundle({"bot_token": "token"}),
                        config={"delivery_mode": "direct"},
                        capability={"dm": True, "max_text_length": 4096},
                        automation_default="BOT_DRAFT_ONLY",
                        status="active",
                    )
                )
            await session.execute(
                insert(models.OutboxMessage).values(
                    id=uuid.uuid4(),
                    tenant_id=outbox_tenant,
                    conversation_id=conversation_id,
                    platform_account_id=outbox_account_id,
                    destination_type="telegram_dm",
                    destination_id="telegram:foreign:user",
                    message_type="text",
                    payload={"text": foreign_secret, "target": {"chat_id": "foreign"}},
                    reply_to_message_id=message_id,
                    origin_kind="MANUAL_REPLY",
                    actor_kind="ADMIN_HUMAN",
                    actor_id="user:foreign",
                    idempotency_key=f"foreign-{uuid.uuid4()}",
                    status="FAILED",
                    last_error_code="FOREIGN_ERROR",
                    last_error_message=foreign_secret,
                )
            )
        elif mismatch == "message_source_outbox":
            foreign_account_id, foreign_contact_id, foreign_conversation_id, foreign_outbox_id = (
                uuid.uuid4() for _ in range(4)
            )
            await session.execute(
                insert(models.PlatformAccount).values(
                    id=foreign_account_id,
                    tenant_id="other",
                    brand_id="foreign",
                    platform="telegram",
                    name="Foreign account",
                    public_id=f"foreign-{uuid.uuid4()}",
                    credential_bundle=encrypt_secret_bundle({"bot_token": "foreign-token"}),
                    config={"delivery_mode": "direct"},
                    capability={"dm": True, "max_text_length": 4096},
                    automation_default="BOT_DRAFT_ONLY",
                    status="active",
                )
            )
            await session.execute(
                insert(models.Contact).values(
                    id=foreign_contact_id,
                    tenant_id="other",
                    platform="telegram",
                    platform_account_id=foreign_account_id,
                    external_user_id=f"foreign-{uuid.uuid4()}",
                    display_name="Foreign contact",
                )
            )
            await session.execute(
                insert(models.Conversation).values(
                    id=foreign_conversation_id,
                    tenant_id="other",
                    brand_id="foreign",
                    platform="telegram",
                    platform_account_id=foreign_account_id,
                    contact_id=foreign_contact_id,
                    conversation_key=f"foreign:{uuid.uuid4()}",
                    channel_type="dm",
                )
            )
            await session.execute(
                insert(models.OutboxMessage).values(
                    id=foreign_outbox_id,
                    tenant_id="other",
                    conversation_id=foreign_conversation_id,
                    platform_account_id=foreign_account_id,
                    destination_type="telegram_dm",
                    destination_id="telegram:foreign-source:user",
                    message_type="text",
                    payload={"text": foreign_secret, "target": {"chat_id": "foreign-source"}},
                    origin_kind="MANUAL_REPLY",
                    actor_kind="ADMIN_HUMAN",
                    actor_id="user:foreign",
                    idempotency_key=f"foreign-source-{uuid.uuid4()}",
                    status="SENT",
                )
            )
            await session.execute(
                update(models.Message)
                .where(models.Message.id == message_id)
                .values(source_outbox_id=foreign_outbox_id, text=foreign_secret)
            )
        else:
            await session.execute(
                insert(models.AuditLog).values(
                    id=uuid.uuid4(),
                    tenant_id="other",
                    category="admin_action",
                    actor="user:foreign",
                    action="FOREIGN_AUDIT",
                    subject_type="conversation",
                    subject_id=str(conversation_id),
                    detail={"secret": foreign_secret},
                )
            )
        await session.commit()

        caplog.clear()
        mismatched_detail = await client.get(
            f"/app/t/default/conversations/{conversation_id}"
        )

    assert healthy_detail.status_code == 200
    assert f"Safe customer {mismatch}" in healthy_detail.text
    assert foreign_secret not in mismatched_detail.text
    if expected_relation is None:
        assert mismatched_detail.status_code == 200
    else:
        assert mismatched_detail.status_code == 404
        assert any(
            "conversation detail scope mismatch" in record.message
            and f"relation={expected_relation}" in record.message
            for record in caplog.records
        )


async def test_conversation_detail_and_manual_reply_route_use_explicit_target(
    session, migrated_db, monkeypatch
):
    now = datetime.now(UTC)
    _account_id, conversation_id, message_id, work_item_id = await _seed_inbox_conversation(
        session,
        suffix="manual",
        display_name="Manual customer",
        work_created_at=now - timedelta(minutes=20),
    )
    await session.commit()
    captured: dict = {}

    async def fake_send_human_reply(**kwargs):
        captured.update(kwargs)
        return uuid.uuid4()

    from social_reply.application.account_management import admin_console

    monkeypatch.setattr(admin_console, "send_human_reply", fake_send_human_reply)
    async with _app_client() as client:
        csrf = await _login(client)
        legacy_detail = await client.get(f"/admin/conversations/{conversation_id}")
        detail = await client.get(f"/app/t/default/conversations/{conversation_id}")
        key_match = re.search(r'name="idempotency_key" value="([^"]+)"', detail.text)
        assert key_match is not None
        invalid_csrf = await client.post(
            f"/admin/conversations/{conversation_id}/reply",
            data={
                "csrf_token": "wrong",
                "reply_to_message_id": str(message_id),
                "idempotency_key": key_match.group(1),
                "work_item_id": str(work_item_id),
                "version": "1",
                "text": "Human response",
            },
        )
        response = await client.post(
            f"/admin/conversations/{conversation_id}/reply",
            data={
                "csrf_token": csrf,
                "reply_to_message_id": str(message_id),
                "idempotency_key": key_match.group(1),
                "work_item_id": str(work_item_id),
                "version": "1",
                "text": "Human response",
            },
        )

    assert legacy_detail.status_code == 303
    assert legacy_detail.headers["location"] == (
        f"/app/t/default/conversations/{conversation_id}"
    )
    assert detail.status_code == 200
    assert "发送人工回复" in detail.text
    assert f'value="{message_id}"' in detail.text
    assert invalid_csrf.status_code == 403
    assert response.status_code == 303
    assert captured["conversation_id"] == conversation_id
    assert captured["reply_to_message_id"] == message_id
    assert captured["work_item_id"] == work_item_id
    assert captured["expected_version"] == 1
    assert captured["idempotency_key"] == key_match.group(1)


async def test_claim_and_resolve_copy_matches_one_click_handoff_lifecycle(session, migrated_db):
    now = datetime.now(UTC)
    _account_id, conversation_id, _message_id, work_item_id = await _seed_inbox_conversation(
        session,
        suffix="lifecycle-copy",
        display_name="Lifecycle customer",
        work_created_at=now - timedelta(minutes=10),
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        waiting = await client.get(f"/app/t/default/conversations/{conversation_id}")
        assert waiting.status_code == 200
        claimed_response = await client.post(
            f"/admin/work-items/{work_item_id}/claim",
            data={"csrf_token": csrf, "version": "1"},
        )
        assert claimed_response.status_code == 303
        claimed = await client.get(f"/app/t/default/conversations/{conversation_id}")
        assert claimed.status_code == 200
        resolved_response = await client.post(
            f"/admin/work-items/{work_item_id}/resolve",
            data={"csrf_token": csrf, "version": "2"},
        )
        assert resolved_response.status_code == 303
        resolved = await client.get(f"/app/t/default/conversations/{conversation_id}")

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_item_id)
    state = await session.get(models.AutomationState, conversation_id)
    assert work.status == "RESOLVED"
    assert state.state == "BOT_DRAFT_ONLY"
    assert "恢复为草稿" not in resolved.text
    assert "恢复自动" not in resolved.text


async def test_conversation_detail_uses_latest_100_messages_for_reply_target(session, migrated_db):
    now = datetime.now(UTC)
    _account_id, conversation_id, _message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="history-window",
        display_name="History customer",
        work_created_at=now - timedelta(hours=1),
    )
    message_ids = [uuid.uuid4() for _ in range(200)]
    await session.execute(
        insert(models.Message),
        [
            {
                "id": message_id,
                "conversation_id": conversation_id,
                "direction": "inbound",
                "sender_type": "contact",
                "text": f"History message {index:03d}",
                "reply_target": {"chat_id": f"history-{index:03d}"},
                "occurred_at": now + timedelta(seconds=index),
            }
            for index, message_id in enumerate(message_ids, start=1)
        ],
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        detail = await client.get(f"/app/t/default/conversations/{conversation_id}")

    assert detail.status_code == 200
    assert "Message history-window" not in detail.text
    assert "History message 001" not in detail.text
    assert detail.text.index("History message 101") < detail.text.index("History message 200")
    latest_choice = re.search(
        rf'name="reply_to_message_id" value="{message_ids[-1]}"',
        detail.text,
    )
    assert latest_choice is not None


async def test_draft_rejection_records_structured_review(session, migrated_db):
    now = datetime.now(UTC)
    _account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="reject",
        display_name="Reject customer",
        work_created_at=now,
    )
    decision_id = uuid.uuid4()
    await session.execute(
        insert(models.ReplyDecision).values(
            id=decision_id,
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text="Unsafe draft",
            original_reply_text="Unsafe draft",
            review_action="PENDING",
            reason_codes=[],
            source="llm",
            decision_generation=0,
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/discard",
            data={"csrf_token": csrf, "review_reason": "Tone is not suitable"},
        )
        reviewed = await client.get(
            f"/app/t/default/inbox?queue=drafts&status=REJECTED&item_id={decision_id}"
        )

    assert response.status_code == 303, response.text
    assert reviewed.status_code == 200
    session.expire_all()
    decision = await session.get(models.ReplyDecision, decision_id)
    assert decision.review_action == "REJECTED"
    assert decision.review_reason == "Tone is not suitable"
    assert decision.reviewed_by == "user:admin"
    assert decision.reviewed_at is not None
    assert "ADMIN_DISCARDED" in decision.reason_codes


async def test_draft_edit_records_final_text_and_outbox_provenance(
    session, migrated_db, monkeypatch
):
    now = datetime.now(UTC)
    _account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="edit",
        display_name="Edit customer",
        work_created_at=now,
    )
    decision_id = uuid.uuid4()
    await session.execute(
        insert(models.ReplyDecision).values(
            id=decision_id,
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text="Original reply",
            original_reply_text="Original reply",
            review_action="PENDING",
            reason_codes=[],
            source="llm",
            decision_generation=0,
        )
    )
    await session.commit()

    async def fake_dispatch(*_args, **_kwargs):
        return None

    from social_reply.application.reply_review import service as reply_review_service

    monkeypatch.setattr(reply_review_service, "dispatch_actor", fake_dispatch)
    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/approve",
            data={"csrf_token": csrf, "final_reply_text": "Edited human reply"},
        )

    assert response.status_code == 303, response.text
    session.expire_all()
    decision = await session.get(models.ReplyDecision, decision_id)
    outbox = await session.get(models.OutboxMessage, decision.review_outbox_id)
    assert decision.original_reply_text == "Original reply"
    assert decision.final_reply_text == "Edited human reply"
    assert decision.review_action == "EDITED"
    assert decision.reviewed_by == "user:admin"
    assert outbox.payload["text"] == "Edited human reply"
    assert outbox.reply_to_message_id == message_id
    assert outbox.origin_kind == "DRAFT_APPROVAL"
    assert outbox.actor_kind == "ADMIN_HUMAN"


async def test_approve_draft_rejects_stale_generation_before_creating_outbox(
    session, migrated_db, monkeypatch
):
    now = datetime.now(UTC)
    _account_id, conversation_id, message_id, _work_item_id = await _seed_inbox_conversation(
        session,
        suffix="stale-approval",
        display_name="Stale approval customer",
        work_created_at=now,
    )
    decision_id = uuid.uuid4()
    await session.execute(
        update(models.Conversation)
        .where(models.Conversation.id == conversation_id)
        .values(decision_generation=2)
    )
    await session.execute(
        insert(models.ReplyDecision).values(
            id=decision_id,
            tenant_id="default",
            conversation_id=conversation_id,
            message_id=message_id,
            action="draft",
            reply_text="Historical reply",
            original_reply_text="Historical reply",
            review_action="PENDING",
            reason_codes=[],
            source="llm",
            decision_generation=1,
        )
    )
    await session.commit()

    dispatched: list[uuid.UUID] = []

    async def fake_dispatch(*_args, **_kwargs):
        dispatched.append(decision_id)

    from social_reply.application.reply_review import service as reply_review_service

    monkeypatch.setattr(reply_review_service, "dispatch_actor", fake_dispatch)
    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/approve",
            data={"csrf_token": csrf},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "draft_stale_conversation_input"
    assert dispatched == []
    assert (
        await session.scalar(
            select(models.OutboxMessage.id).where(
                models.OutboxMessage.origin_kind == "DRAFT_APPROVAL",
                models.OutboxMessage.conversation_id == conversation_id,
            )
        )
        is None
    )
    session.expire_all()
    decision = await session.get(models.ReplyDecision, decision_id)
    assert decision.review_action == "PENDING"
    assert decision.review_outbox_id is None


async def test_accounts_page_renders_seven_channel_tiles(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": True,
            "facebook_messenger_enabled": True,
            "instagram_messaging_enabled": True,
            "meta_comment_reply_enabled": True,
            "meta_auto_reply_enabled": True,
            "whatsapp_enabled": True,
            "feishu_enabled": True,
            "email_enabled": True,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        legacy_response = await client.get("/admin/accounts")
        response = await client.get("/app/t/default/channels")

    assert legacy_response.status_code == 303
    assert legacy_response.headers["location"] == "/app/t/default/channels"
    assert response.status_code == 200
    html = response.text
    assert "添加渠道" in html
    assert 'role="list"' not in html
    assert 'role="listitem"' not in html
    for channel, label in (
        ("x", "X"),
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("telegram", "Telegram"),
        ("whatsapp", "WhatsApp"),
        ("feishu", "Feishu"),
        ("email", "Email"),
    ):
        assert f"/static/channel-icons/{channel}.svg" in html
        assert label in html
    assert 'id="channel-setup"' not in html
    assert 'action="/admin/oauth/x/start"' not in html
    assert 'action="/admin/connect/telegram"' not in html


async def test_accounts_page_renders_oauth_channel_panels(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console, saas_console

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
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        x_page = await client.get("/app/t/default/channels?connect=x")
        facebook_page = await client.get("/app/t/default/channels?connect=facebook")
        instagram_page = await client.get("/app/t/default/channels?connect=instagram")

    assert 'action="/app/t/default/channels/oauth/x/start"' in x_page.text
    assert "data-channel-oauth-form" in x_page.text

    assert 'action="/app/t/default/channels/oauth/meta/start"' in facebook_page.text
    assert 'name="platform" value="facebook"' in facebook_page.text

    assert 'action="/app/t/default/channels/oauth/instagram/start"' in instagram_page.text
    assert 'action="/app/t/default/channels/oauth/meta/start"' in instagram_page.text
    assert 'name="platform" value="instagram"' in instagram_page.text
    for page in (x_page, facebook_page, instagram_page):
        assert 'action="/admin' not in page.text


async def test_accounts_page_renders_manual_channel_panels(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(
        update={"whatsapp_enabled": True, "feishu_enabled": True}
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        telegram_page = await client.get("/app/t/default/channels?connect=telegram")
        whatsapp_page = await client.get("/app/t/default/channels?connect=whatsapp")
        feishu_page = await client.get("/app/t/default/channels?connect=feishu")

    assert 'action="/app/t/default/channels/accounts/telegram"' in telegram_page.text
    assert 'name="token"' in telegram_page.text
    assert 'action="/app/t/default/channels/accounts/whatsapp"' in whatsapp_page.text
    assert 'name="access_token"' in whatsapp_page.text
    assert 'name="verify_token"' in whatsapp_page.text
    assert 'action="/app/t/default/channels/accounts/feishu"' in feishu_page.text
    assert 'name="app_id"' in feishu_page.text
    assert 'name="app_secret"' in feishu_page.text
    assert 'name="verification_token"' in feishu_page.text
    assert 'name="encrypt_key"' in feishu_page.text
    assert 'name="automation_default" value="BOT_DRAFT_ONLY"' in feishu_page.text
    assert 'name="group_mode" value="mentions_only"' in feishu_page.text


async def test_accounts_page_renders_email_connection_form_and_icon(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/app/t/default/channels?connect=email")
        icon = await client.get("/static/channel-icons/email.svg")
        stylesheet = await client.get("/static/saas.css")

    assert page.status_code == 200
    assert icon.status_code == 200
    assert "<svg" in icon.text and "<path" in icon.text
    assert "/static/channel-icons/email.svg" in page.text
    assert 'action="/app/t/default/channels/accounts/email"' in page.text
    for field_name in ("email_address", "username", "password", "imap_host", "smtp_host"):
        assert f'name="{field_name}"' in page.text
    assert 'type="password"' in page.text
    assert "<style>" not in page.text
    assert stylesheet.status_code == 200
    assert ".channel-form-grid" in stylesheet.text


async def test_accounts_page_renders_email_sanitized_health_without_password(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    leaked_password = "mail-password-must-not-leak"
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="email",
            name="Support Email",
            external_account_id="support@example.com",
            public_id=f"email_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"username": "support@example.com", "password": leaked_password}
            ),
            config={
                "mailbox": "INBOX",
                "smtp_security": "ssl",
                "email_health_status": "READY",
                "email_health_checked_at": "2026-08-03T00:00:00+00:00",
                "email_health_error_code": f"AUTH_FAILED:{leaked_password}:<script>",
            },
            capability={"dm": True, "max_text_length": 10000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        response = await client.get("/app/t/default/channels")

    assert response.status_code == 200
    assert "Support Email" in response.text
    assert "support@example.com" in response.text
    assert "已连接" in response.text
    assert leaked_password not in response.text
    assert "<script>" not in response.text


def test_email_probe_timestamp_formatter_handles_invalid_iso_safely():
    from social_reply.application.account_management.admin_console import _fmt_iso_timestamp

    assert _fmt_iso_timestamp("2026-08-03T08:30:00+08:00") == "2026-08-03 00:30 UTC"
    assert _fmt_iso_timestamp("not-a-timestamp") == "—"
    assert _fmt_iso_timestamp(None) == "—"


async def test_admin_email_post_enforces_auth_validation_gate_and_secret_split(
    migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin, channel_management

    submissions = []
    disabled_settings = channel_management.get_settings().model_copy(
        update={"email_enabled": False}
    )
    enabled_settings = disabled_settings.model_copy(update={"email_enabled": True})
    monkeypatch.setattr(channel_management, "get_settings", lambda: disabled_settings)

    async def fake_submit(command):
        submissions.append(command)
        return uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    monkeypatch.setattr(admin, "submit_channel_provisioning", fake_submit)
    payload = {
        "tenant_id": "default",
        "brand_id": "default",
        "name": "",
        "email_address": " Support@Example.COM. ",
        "from_name": "",
        "username": "mail-user",
        "password": "mail-password-must-not-leak",
        "imap_host": "IMAP.LARKSUITE.COM.",
        "imap_port": "993",
        "mailbox": "INBOX",
        "smtp_host": "SMTP.LARKSUITE.COM.",
        "smtp_port": "",
        "smtp_security": "ssl",
        "internal_domain_policy": "ignore",
        "automation_default": "BOT_DRAFT_ONLY",
    }
    async with _app_client() as anonymous:
        unauthenticated = await anonymous.post(
            "/admin/connect/email", data={"csrf_token": "bad", **payload, "unexpected": "x"}
        )
    async with _app_client() as client:
        csrf = await _login(client)
        bad_csrf = await client.post("/admin/connect/email", data={"csrf_token": "bad", **payload})
        wrong_tenant = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "tenant_id": "forbidden"},
        )
        disabled = await client.post("/admin/connect/email", data={"csrf_token": csrf, **payload})
        monkeypatch.setattr(channel_management, "get_settings", lambda: enabled_settings)
        extra = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "unexpected": "x"},
        )
        active = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "automation_default": "BOT_ACTIVE"},
        )
        blank_username = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "username": ""},
        )
        blank_password = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "password": ""},
        )
        oversized_password = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "password": "x" * 513},
        )
        invalid_brand = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "brand_id": "invalid brand"},
        )
        disallowed_settings = enabled_settings.model_copy(
            update={"email_allowed_hosts": frozenset({"smtp.larksuite.com"})}
        )
        monkeypatch.setattr(channel_management, "get_settings", lambda: disallowed_settings)
        disallowed = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload},
        )
        monkeypatch.setattr(channel_management, "get_settings", lambda: enabled_settings)
        submitted = await client.post("/admin/connect/email", data={"csrf_token": csrf, **payload})
        starttls_submitted = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "smtp_security": "starttls"},
        )

    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/auth/login"
    assert bad_csrf.status_code == 403
    assert wrong_tenant.status_code == 404
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "email_integration_disabled"
    assert extra.status_code == 422
    assert active.status_code == 422
    assert blank_username.status_code == 422
    assert blank_password.status_code == 422
    assert oversized_password.status_code == 422
    assert invalid_brand.status_code == 422
    assert disallowed.status_code == 422
    assert disallowed.json()["detail"] == "invalid_email_account_request"
    for rejected in (
        extra,
        active,
        blank_username,
        blank_password,
        oversized_password,
        invalid_brand,
        disallowed,
    ):
        assert "mail-user" not in rejected.text
        assert "mail-password-must-not-leak" not in rejected.text
    assert submitted.status_code == 303
    assert submitted.headers["location"] == (
        "/admin/integrations/provisioning-jobs/dddddddd-dddd-dddd-dddd-dddddddddddd"
    )
    assert starttls_submitted.status_code == 303
    assert "mail-password-must-not-leak" not in submitted.text
    assert len(submissions) == 2
    assert submissions[0].platform == "email"
    assert submissions[0].tenant_id == "default"
    assert submissions[0].public_values == {
        "automation_default": "BOT_DRAFT_ONLY",
        "email_address": "Support@example.com",
        "imap_host": "imap.larksuite.com",
        "imap_port": 993,
        "mailbox": "INBOX",
        "smtp_host": "smtp.larksuite.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "internal_domain_policy": "ignore",
    }
    assert submissions[0].secret_values == {
        "username": "mail-user",
        "password": "mail-password-must-not-leak",
    }
    assert submissions[1].public_values["smtp_security"] == "starttls"
    assert submissions[1].public_values["smtp_port"] == 587
    assert type(submissions[1].public_values["smtp_port"]) is int
    assert not set(submissions[0].public_values) & {"username", "password"}


async def test_accounts_page_renders_feishu_sanitized_channel_health(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="feishu",
            name="Support Bot",
            external_account_id="cli_12345678",
            public_id=f"fs_{uuid.uuid4().hex}",
            config={
                "feishu_health_status": "READY",
                "feishu_health_checked_at": "2026-08-03T00:00:00+00:00",
                "feishu_bot_name": "Support Bot",
                "feishu_bot_activate_status": 2,
            },
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        response = await client.get("/app/t/default/channels")

    assert response.status_code == 200
    assert "Support Bot" in response.text
    assert "/static/channel-icons/feishu.svg" in response.text
    assert "verification-secret-value" not in response.text
    assert "encrypt-secret-value" not in response.text


async def test_feishu_account_can_be_explicitly_promoted_after_provisioning(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="feishu",
            name="Support Bot",
            external_account_id="cli_87654321",
            public_id=f"fs_{uuid.uuid4().hex}",
            config={"feishu_health_status": "READY"},
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        promoted = await client.post(
            f"/admin/accounts/{account_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE"},
        )

    assert promoted.status_code == 303
    session.expire_all()
    account = await session.get(models.PlatformAccount, account_id)
    assert account.automation_default == "BOT_ACTIVE"


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


@pytest.mark.parametrize(
    "settings_update",
    [
        {"email_enabled": False, "email_auto_reply_enabled": True},
        {"email_enabled": True, "email_auto_reply_enabled": False},
    ],
)
async def test_email_account_automation_gate_hides_promotion_and_keeps_history_fallback(
    session, migrated_db, monkeypatch, settings_update
):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    draft_id, legacy_active_id = uuid.uuid4(), uuid.uuid4()
    for account_id, policy, address in (
        (draft_id, "BOT_DRAFT_ONLY", "draft@example.com"),
        (legacy_active_id, "BOT_ACTIVE", "legacy@example.com"),
    ):
        await session.execute(
            insert(models.PlatformAccount).values(
                id=account_id,
                tenant_id="default",
                brand_id="default",
                platform="email",
                name=address,
                external_account_id=address,
                public_id=f"email_{uuid.uuid4().hex}",
                config={"mailbox": "INBOX", "smtp_security": "ssl"},
                capability={"dm": True, "max_text_length": 10000},
                automation_default=policy,
                status="active",
            )
        )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        rejected = await client.post(
            f"/admin/accounts/{draft_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE"},
        )
        converged = await client.post(
            f"/admin/accounts/{legacy_active_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_DRAFT_ONLY"},
        )

    assert rejected.status_code == 422
    assert converged.status_code == 303
    session.expire_all()
    assert (await session.get(models.PlatformAccount, draft_id)).automation_default == (
        "BOT_DRAFT_ONLY"
    )
    assert (await session.get(models.PlatformAccount, legacy_active_id)).automation_default == (
        "BOT_DRAFT_ONLY"
    )


async def test_meta_account_can_be_promoted_once_deployment_opts_in(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import (
        admin_console,
        channel_management,
        saas_console,
    )

    settings = admin_console.get_settings().model_copy(update={"meta_auto_reply_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(channel_management, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
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
                models.AuditLog.action == "SET_PLATFORM_ACCOUNT_AUTOMATION",
            )
        )
    ).scalar_one()
    assert entry.detail == {
        "previous_target": "BOT_DRAFT_ONLY",
        "target": "BOT_ACTIVE",
        "changed": True,
        "config_version": 2,
    }


async def test_accounts_page_disables_future_platform_tiles_when_flagged_off(
    migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(
        update={
            "facebook_messenger_enabled": False,
            "instagram_messaging_enabled": False,
            "whatsapp_enabled": False,
            "feishu_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/app/t/default/channels")
        disabled = await client.get("/app/t/default/channels?connect=instagram")

    assert response.status_code == 200
    assert response.text.count('disabled aria-disabled="true"') >= 4
    assert 'action="/admin/oauth/meta/start"' not in response.text
    assert 'action="/admin/oauth/instagram/start"' not in response.text
    assert 'action="/admin/connect/whatsapp"' not in response.text
    assert 'action="/admin/connect/feishu"' not in response.text
    assert 'action="/admin' not in disabled.text


async def test_accounts_page_disables_x_tile_when_all_stacks_are_off(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console, saas_console

    settings = admin_console.get_settings().model_copy(
        update={
            "x_legacy_dm_enabled": False,
            "x_activity_enabled": False,
            "xchat_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    monkeypatch.setattr(saas_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/app/t/default/channels")
        disabled = await client.get("/app/t/default/channels?connect=x")

    assert response.status_code == 200
    assert 'disabled aria-disabled="true"' in response.text
    assert 'action="/admin/oauth/x/start"' not in response.text
    assert 'action="/admin/connect/x"' not in response.text
    assert 'action="/admin' not in disabled.text


async def test_accounts_page_renders_x_oauth_result_banner(migrated_db):
    async with _app_client() as client:
        await _login(client)
        connected = await client.get(
            "/app/t/default/channels?provider=x&status=connected"
        )
        processing = await client.get(
            "/app/t/default/channels?provider=x&status=processing"
            "&code=provisioning_in_progress"
        )
        failed = await client.get(
            "/app/t/default/channels?provider=x&status=error"
            "&code=x_token_exchange_rejected"
        )
    assert 'class="saas-alert success" role="status"' in connected.text
    assert 'class="saas-alert success" role="status"' in processing.text
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
        response = await client.get("/app/t/default/channels")

    assert response.status_code == 200
    assert "@xbot" in response.text
    assert f'href="/app/t/default/channels/accounts/{account_id}"' in response.text
    assert "access_token_secret" not in response.text


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

    monkeypatch.setattr(admin_console, "repair_channel_xchat", fail_activation)

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
        legacy_page = await client.get(f"/admin/jobs/{job_id}")
        page = await client.get(f"/app/t/default/channels/jobs/{job_id}")
        retry = await client.post(
            f"/admin/jobs/{job_id}/retry",
            data={"csrf_token": csrf},
        )

    assert legacy_page.status_code == 303
    assert legacy_page.headers["location"] == f"/app/t/default/channels/jobs/{job_id}"
    assert page.status_code == 200
    assert page.json()["status"] == "NEEDS_ACTION"
    assert page.json()["last_error_code"] == "XCHAT_PIN_INVALID"
    assert "access_token" not in page.text
    assert retry.status_code == 409
    assert retry.json()["detail"] == "provisioning_secret_resubmission_required"


async def test_email_provisioning_job_requires_account_password_resubmission(session, migrated_db):
    job_id = uuid.uuid4()
    await session.execute(
        insert(models.ProvisioningJob).values(
            id=job_id,
            tenant_id="default",
            brand_id="b1",
            platform="email",
            actor="user:admin",
            idempotency_key="email-password-resubmit",
            request={"email_address": "Support@example.com"},
            staging_secret=None,
            status="NEEDS_ACTION",
            current_step="FAILED",
            result={
                "requires_secret_resubmission": True,
                "required_secret": "password",
            },
            last_error_code="imap_tls_invalid",
            last_error_message="Email IMAP protocol validation failed",
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        legacy_page = await client.get(f"/admin/jobs/{job_id}")
        page = await client.get(f"/app/t/default/channels/jobs/{job_id}")
        retry = await client.post(
            f"/admin/jobs/{job_id}/retry",
            data={"csrf_token": csrf},
        )

    assert legacy_page.status_code == 303
    assert legacy_page.headers["location"] == f"/app/t/default/channels/jobs/{job_id}"
    assert page.status_code == 200
    assert page.json()["status"] == "NEEDS_ACTION"
    assert page.json()["last_error_code"] == "imap_tls_invalid"
    assert "Email IMAP protocol validation failed" not in page.text
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
        page = await client.get(f"/app/t/default/channels/jobs/{job_id}")
        accounts = await client.get("/app/t/default/channels")

    assert page.status_code == 200
    assert page.json()["status"] == "FAILED"
    assert str(job_id) in accounts.text


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
        page = await client.get(f"/app/t/default/channels/jobs/{job_id}")
        accounts = await client.get("/app/t/default/channels")

    assert page.status_code == 200
    assert page.json()["status"] == "FAILED"
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
        detail = await client.get(f"/app/t/default/conversations/{conv_id}")
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


async def test_email_conversation_transition_gate_blocks_bot_active_not_human_active(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"email_auto_reply_enabled": False})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    account_id, contact_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="email",
            name="support@example.com",
            external_account_id="support@example.com",
            public_id=f"email_{uuid.uuid4().hex}",
            config={"mailbox": "INBOX", "smtp_security": "ssl"},
            capability={"dm": True, "max_text_length": 10000},
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.execute(
        insert(models.Contact).values(
            id=contact_id,
            tenant_id="default",
            platform="email",
            platform_account_id=account_id,
            external_user_id="customer@example.com",
            display_name="Email customer",
        )
    )
    await session.execute(
        insert(models.Conversation).values(
            id=conversation_id,
            tenant_id="default",
            brand_id="default",
            platform="email",
            platform_account_id=account_id,
            contact_id=contact_id,
            conversation_key=f"email:{account_id}:customer@example.com",
            channel_type="dm",
        )
    )
    await session.execute(
        insert(models.AutomationState).values(
            conversation_id=conversation_id,
            state="CLOSED",
            state_version=1,
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        detail = await client.get(f"/app/t/default/conversations/{conversation_id}")
        rejected = await client.post(
            f"/admin/conversations/{conversation_id}/state",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE", "expect": "CLOSED"},
        )
        takeover = await client.post(
            f"/admin/conversations/{conversation_id}/state",
            data={"csrf_token": csrf, "target": "HUMAN_ACTIVE", "expect": "CLOSED"},
        )

    assert detail.status_code == 200
    assert rejected.status_code == 422
    assert takeover.status_code == 303
    session.expire_all()
    state = await session.get(models.AutomationState, conversation_id)
    assert state.state == "HUMAN_ACTIVE"


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
                "reply": "请联系 support@example.com",
                "category": "常见",
                "is_official_contact": "true",
            },
        )
        assert resp.status_code == 303
        assert "notice=created" in resp.headers["location"]
        assert resp.headers["location"].startswith("/app/t/default/knowledge")

    docs = (await session.execute(select(models.KnowledgeDocument))).scalars().all()
    assert any(d.question == "你们几点营业" for d in docs)
    assert {d.status for d in docs} == {"draft"}
    assert {d.is_official_contact for d in docs} == {True}
    chunk = (await session.execute(select(models.KnowledgeChunk))).scalars().first()
    assert chunk.embed_text == "你们几点营业"  # 非对称嵌入：只嵌问题
    audit = (
        await session.execute(
            select(models.AuditLog).where(
                models.AuditLog.action == "SET_KNOWLEDGE_OFFICIAL_CONTACT"
            )
        )
    ).scalar_one()
    assert audit.actor == "user:admin"
    assert audit.detail["content_hash"] == chunk.content_hash


async def test_duplicate_manual_knowledge_skips_embedding(migrated_db, monkeypatch):
    from social_reply.application.reply_decision import runner
    from social_reply.domain.knowledge.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(runner, "_embedder", FakeEmbeddingClient())
    payload = {
        "tenant_id": "default",
        "question": "duplicate question",
        "reply": "duplicate reply",
    }
    async with _app_client() as client:
        csrf = await _login(client)
        added = await client.post(
            "/admin/knowledge/add",
            data={"csrf_token": csrf, **payload},
        )
        assert "notice=created" in added.headers["location"]

        class _FailIfCalledEmbeddingClient(FakeEmbeddingClient):
            async def embed(self, texts):
                raise AssertionError("duplicate knowledge must not be embedded")

        monkeypatch.setattr(runner, "_embedder", _FailIfCalledEmbeddingClient())
        duplicate = await client.post(
            "/admin/knowledge/add",
            data={"csrf_token": csrf, **payload},
        )

    assert duplicate.status_code == 303
    assert "notice=duplicate" in duplicate.headers["location"]


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
    assert {d.status for d in docs} == {"draft"}
    assert {d.is_official_contact for d in docs} == {False}

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


async def test_knowledge_explicit_publish_unpublish_is_audited_and_idempotent(session, migrated_db):
    # 用普通文档：official-contact 文档已不可发布，用它测不了发布幂等性，
    # 那条规则由 test_official_contact_knowledge_cannot_be_published 单独覆盖。
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        question="refund timing",
        reply="Refunds are processed within a few business days.",
        status="draft",
        is_official_contact=False,
    )
    session.add(doc)
    await session.flush()
    await _make_knowledge_publishable(session, doc)
    await session.commit()
    doc_id = doc.id

    async with _app_client() as client:
        csrf = await _login(client)
        published = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
        same_target = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
        unpublished = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "draft"},
        )
    assert published.status_code == same_target.status_code == unpublished.status_code == 303
    session.expire_all()
    assert (await session.get(models.KnowledgeDocument, doc_id)).status == "draft"
    audits = (
        (
            await session.execute(
                select(models.AuditLog)
                .where(models.AuditLog.subject_id == str(doc_id))
                .order_by(models.AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [audit.action for audit in audits] == [
        "PUBLISH_KNOWLEDGE",
        "UNPUBLISH_KNOWLEDGE",
    ]
    assert audits[0].detail == {
        "from": "draft",
        "to": "published",
        "brand_hash": hashlib.sha256(b"b1").hexdigest(),
        "platform_hash": hashlib.sha256(b"telegram").hexdigest(),
        "is_official_contact": False,
        "bulk": False,
    }
    assert audits[1].detail["from"] == "published"
    assert audits[1].detail["to"] == "draft"


async def test_official_contact_knowledge_cannot_be_published(session, migrated_db):
    # official-contact 事实只允许确定性 verbatim 渲染，发布路径一律拒绝，
    # 且不得留下发布审计——否则审计流水会显示一次并未发生的发布。
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        question="official contact",
        reply="support@example.com",
        status="draft",
        is_official_contact=True,
    )
    session.add(doc)
    await session.flush()
    await _make_knowledge_publishable(session, doc)
    await session.commit()
    doc_id = doc.id

    async with _app_client() as client:
        csrf = await _login(client)
        rejected = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
    assert rejected.status_code == 409
    session.expire_all()
    assert (await session.get(models.KnowledgeDocument, doc_id)).status == "draft"
    assert (
        await session.scalar(
            select(func.count())
            .select_from(models.AuditLog)
            .where(
                models.AuditLog.subject_id == str(doc_id),
                models.AuditLog.action == "PUBLISH_KNOWLEDGE",
            )
        )
    ) == 0


async def test_knowledge_bulk_publish_normal_drafts_is_tenant_scoped_audited_and_idempotent(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    monkeypatch.setattr(admin_console, "_KNOWLEDGE_BULK_PUBLISH_CHUNK_SIZE", 1)
    normal = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        platform="telegram",
        question="bulk normal",
        reply="normal reply",
        status="draft",
    )
    normal_two = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b2",
        question="bulk normal two",
        reply="second normal reply",
        status="draft",
    )
    official = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="bulk official",
        reply="support@example.com",
        status="draft",
        is_official_contact=True,
    )
    unclassified_contact = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="bulk unclassified contact",
        reply="Email support@example.com",
        status="draft",
        is_official_contact=False,
    )
    already_published = models.KnowledgeDocument(
        tenant_id="default",
        question="already published",
        reply="published reply",
        status="published",
    )
    foreign = models.KnowledgeDocument(
        tenant_id="other-tenant",
        question="foreign draft",
        reply="foreign reply",
        status="draft",
    )
    session.add_all(
        [normal, normal_two, official, unclassified_contact, already_published, foreign]
    )
    await session.flush()
    await _make_knowledge_publishable(session, normal)
    await _make_knowledge_publishable(session, normal_two)
    await session.commit()
    normal_id = normal.id
    normal_two_id = normal_two.id
    official_id = official.id
    unclassified_contact_id = unclassified_contact.id
    published_id = already_published.id
    foreign_id = foreign.id

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/app/t/default/knowledge")
        assert page.status_code == 200
        assert 'action="/app/t/default/knowledge/bulk-publish"' in page.text

        bad_csrf = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": "invalid", "tenant_id": "default"},
        )
        missing_tenant = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": csrf},
        )
        first = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": csrf, "tenant_id": "default"},
        )
        second = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": csrf, "tenant_id": "default"},
        )
        foreign_attempt = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": csrf, "tenant_id": "other-tenant"},
        )

    assert bad_csrf.status_code == 403
    assert missing_tenant.status_code == 422
    assert first.status_code == second.status_code == 303
    assert "notice=bulk_published&published=2" in first.headers["location"]
    assert "notice=bulk_published&published=0" in second.headers["location"]
    assert foreign_attempt.status_code == 403
    session.expire_all()
    assert (await session.get(models.KnowledgeDocument, normal_id)).status == "published"
    assert (await session.get(models.KnowledgeDocument, normal_two_id)).status == "published"
    assert (await session.get(models.KnowledgeDocument, official_id)).status == "draft"
    assert (await session.get(models.KnowledgeDocument, unclassified_contact_id)).status == "draft"
    assert (await session.get(models.KnowledgeDocument, published_id)).status == "published"
    assert (await session.get(models.KnowledgeDocument, foreign_id)).status == "draft"
    audits = (
        (
            await session.execute(
                select(models.AuditLog)
                .where(
                    models.AuditLog.subject_id.in_(
                        [
                            str(normal_id),
                            str(normal_two_id),
                            str(official_id),
                            str(unclassified_contact_id),
                        ]
                    ),
                    models.AuditLog.action == "PUBLISH_KNOWLEDGE",
                )
                .order_by(models.AuditLog.subject_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 2
    assert {audit.subject_id for audit in audits} == {str(normal_id), str(normal_two_id)}
    assert all(audit.tenant_id == "default" for audit in audits)
    assert all(audit.category == "admin_action" for audit in audits)
    assert all(audit.action == "PUBLISH_KNOWLEDGE" for audit in audits)
    assert all(audit.detail["bulk"] is True for audit in audits)
    assert all(audit.detail["from"] == "draft" for audit in audits)
    assert all(audit.detail["to"] == "published" for audit in audits)
    assert all(audit.detail["is_official_contact"] is False for audit in audits)


async def test_knowledge_import_batch_confirmation_and_english_publish_gate(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={"english_knowledge_only_enabled": True}
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    first_batch = uuid.uuid4()
    second_batch = uuid.uuid4()
    english = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="How long does a refund take?",
        reply="Refunds usually take 3 to 5 business days.",
        status="draft",
        source_file="knowledge.csv",
        import_batch_id=first_batch,
        detected_language="en",
        language_detection_status="english",
    )
    same_filename_other_batch = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="How can I update my profile?",
        reply="Open settings and select Profile.",
        status="draft",
        source_file="knowledge.csv",
        import_batch_id=second_batch,
        detected_language="en",
        language_detection_status="english",
    )
    mixed = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="English question",
        reply="中文答案",
        status="draft",
        source_file="mixed.csv",
        import_batch_id=uuid.uuid4(),
        detected_language="mixed",
        language_detection_status="mixed",
    )
    session.add_all([english, same_filename_other_batch, mixed])
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id="default",
            document_id=english.id,
            content=(
                "Question: How long does a refund take?\\n"
                "Approved answer: Refunds usually take 3 to 5 business days."
            ),
            embed_text="How long does a refund take?",
            content_hash="f" * 64,
            embedding_version="text-embedding-3-small",
            embedding=[0.0] * 1536,
        )
    )
    await session.commit()
    english_id = english.id
    other_id = same_filename_other_batch.id
    mixed_id = mixed.id

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/app/t/default/knowledge")
        assert str(first_batch) in page.text
        assert str(second_batch) in page.text
        confirmed = await client.post(
            "/admin/knowledge/bulk-confirm-english",
            data={"csrf_token": csrf, "import_batch_id": str(first_batch)},
        )
        mixed_confirmation = await client.post(
            f"/admin/knowledge/{mixed_id}/confirm-english",
            data={"csrf_token": csrf},
        )
        published = await client.post(
            "/admin/knowledge/bulk-publish",
            data={"csrf_token": csrf, "tenant_id": "default"},
        )

    assert confirmed.status_code == published.status_code == 303
    assert "count=1" in confirmed.headers["location"]
    assert mixed_confirmation.status_code == 409
    session.expire_all()
    assert (await session.get(models.KnowledgeDocument, english_id)).language_verified is True
    assert (await session.get(models.KnowledgeDocument, english_id)).status == "published"
    assert (await session.get(models.KnowledgeDocument, other_id)).language_verified is False
    assert (await session.get(models.KnowledgeDocument, other_id)).status == "draft"
    assert (await session.get(models.KnowledgeDocument, mixed_id)).status == "draft"
    audits = (
        (
            await session.execute(
                select(models.AuditLog).where(
                    models.AuditLog.subject_id == str(english_id),
                    models.AuditLog.action == "CONFIRM_KNOWLEDGE_ENGLISH",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].detail["import_batch_id"] == str(first_batch)
    assert audits[0].detail["confirmation_batch_id"]


async def test_runtime_mode_rejects_unverified_non_english_publish(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "multilingual_knowledge_reply_enabled": True,
            "english_knowledge_only_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="退款政策",
        reply="中文答案",
        status="draft",
        source_language="zh",
        language_verified=False,
    )
    session.add(doc)
    await session.commit()

    with pytest.raises(HTTPException, match="confirm_english_before_publish"):
        await admin_console._require_knowledge_publishable(session, doc)


async def test_runtime_publish_requires_current_embedding(session, migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "multilingual_knowledge_reply_enabled": True,
            "english_knowledge_only_enabled": False,
            "openai_embedding_model": "text-embedding-3-small",
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="What is a demo account?",
        reply="A demo account uses virtual funds.",
        status="draft",
        source_language="en",
        language_verified=True,
    )
    session.add(doc)
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id="default",
            document_id=doc.id,
            content=(
                "Question: What is a demo account?\\n"
                "Approved answer: A demo account uses virtual funds."
            ),
            embed_text="What is a demo account?",
            content_hash="d" * 64,
            embedding_version="old-model",
            embedding=[0.0] * 1536,
        )
    )
    await session.flush()

    with pytest.raises(HTTPException, match="knowledge_embedding_not_ready"):
        await admin_console._require_knowledge_publishable(session, doc)

    class NoEmbeddingSession:
        async def scalar(self, statement):
            return None

    with pytest.raises(HTTPException, match="knowledge_embedding_not_ready"):
        await admin_console._require_knowledge_publishable(NoEmbeddingSession(), doc)

    session.add(
        models.KnowledgeChunk(
            tenant_id="default",
            document_id=doc.id,
            content=(
                "Question: What is a demo account?\\n"
                "Approved answer: A demo account uses virtual funds."
            ),
            embed_text="What is a demo account?",
            content_hash="e" * 64,
            embedding_version="text-embedding-3-small",
            embedding=[0.0] * 1536,
        )
    )
    await session.flush()
    await admin_console._require_knowledge_publishable(session, doc)


async def test_single_publish_rejects_contact_like_reply(session, migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={
            "multilingual_knowledge_reply_enabled": True,
            "english_knowledge_only_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="Where can I contact support?",
        reply="Email support@example.com for help.",
        status="draft",
        source_language="en",
        language_verified=True,
        is_official_contact=False,
    )
    session.add(doc)
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id="default",
            document_id=doc.id,
            content="support contact",
            embed_text=doc.question,
            content_hash="p" * 64,
            embedding_version=settings.openai_embedding_model,
            embedding=[0.0] * 1536,
        )
    )
    await session.flush()

    with pytest.raises(HTTPException, match="official_contact_requires_review"):
        await admin_console._require_knowledge_publishable(session, doc)


async def test_draft_knowledge_official_contact_classification_is_audited(session, migrated_db):
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="historical contact",
        reply="support@example.com",
        status="draft",
    )
    session.add(doc)
    await session.flush()
    session.add(
        models.KnowledgeChunk(
            tenant_id="default",
            document_id=doc.id,
            content="问：historical contact\n答：support@example.com",
            embed_text="historical contact",
            content_hash="c" * 64,
            embedding_version="fake",
            embedding=[0.0] * 1536,
        )
    )
    await session.commit()
    doc_id = doc.id

    async with _app_client() as client:
        csrf = await _login(client)
        classified = await client.post(
            f"/admin/knowledge/{doc_id}/official-contact",
            data={"csrf_token": csrf, "target": "true"},
        )
        same_target = await client.post(
            f"/admin/knowledge/{doc_id}/official-contact",
            data={"csrf_token": csrf, "target": "true"},
        )
        # 分类成 official-contact 之后就不再可发布，原先"发布后禁止改分类"的
        # 断言对这类文档已不可达；改为直接锁死发布被拒。禁止改分类那条规则由
        # test_published_knowledge_cannot_be_reclassified 用普通文档覆盖。
        publish_attempt = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
    assert classified.status_code == same_target.status_code == 303
    assert publish_attempt.status_code == 409
    session.expire_all()
    stored = await session.get(models.KnowledgeDocument, doc_id)
    assert stored.is_official_contact is True
    assert stored.status == "draft"
    audits = (
        (
            await session.execute(
                select(models.AuditLog).where(
                    models.AuditLog.subject_id == str(doc_id),
                    models.AuditLog.action == "SET_KNOWLEDGE_OFFICIAL_CONTACT",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].detail == {
        "from": False,
        "to": True,
        "brand_hash": hashlib.sha256(b"b1").hexdigest(),
        "platform_hash": None,
        "status": "draft",
        "content_hash": "c" * 64,
    }


async def test_published_knowledge_cannot_be_reclassified(session, migrated_db):
    # 已发布文档禁止改 official-contact 分类（必须先下架）。这条规则原先由
    # test_draft_knowledge_official_contact_classification_is_audited 顺带覆盖，
    # 但 official-contact 文档已不可发布，改用普通文档保住这份覆盖。
    doc = models.KnowledgeDocument(
        tenant_id="default",
        brand_id="b1",
        question="refund timing",
        reply="Refunds are processed within a few business days.",
        status="draft",
        is_official_contact=False,
    )
    session.add(doc)
    await session.flush()
    await _make_knowledge_publishable(session, doc)
    await session.commit()
    doc_id = doc.id

    async with _app_client() as client:
        csrf = await _login(client)
        published = await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
        reclassify = await client.post(
            f"/admin/knowledge/{doc_id}/official-contact",
            data={"csrf_token": csrf, "target": "true"},
        )
    assert published.status_code == 303
    assert reclassify.status_code == 409
    session.expire_all()
    stored = await session.get(models.KnowledgeDocument, doc_id)
    assert stored.status == "published"
    assert stored.is_official_contact is False


async def test_knowledge_status_is_tenant_scoped_and_target_is_explicit(session, migrated_db):
    foreign = models.KnowledgeDocument(
        tenant_id="other-tenant",
        question="q",
        reply="r",
        status="draft",
    )
    session.add(foreign)
    await session.commit()
    foreign_id = foreign.id
    async with _app_client() as client:
        csrf = await _login(client)
        invalid = await client.post(
            f"/admin/knowledge/{foreign_id}/status",
            data={"csrf_token": csrf, "target": "toggle"},
        )
        cross_tenant = await client.post(
            f"/admin/knowledge/{foreign_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
    assert invalid.status_code == 422
    assert cross_tenant.status_code == 404
    session.expire_all()
    assert (await session.get(models.KnowledgeDocument, foreign_id)).status == "draft"


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
    assert page is not None and page.status_code == 303
    assert page.headers["location"] == ("/app/t/default/knowledge?notice=import_bad_csv")


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


async def test_global_killswitch_has_separate_safety_page(migrated_db):
    async with _app_client() as client:
        await _login(client)
        accounts = await client.get("/admin/integrations/accounts")
        safety = await client.get("/admin/system/safety")

    assert accounts.status_code == 303
    assert accounts.headers["location"] == "/app/t/default/channels"
    assert "自动回复总开关" not in accounts.text
    assert safety.status_code == 200

    async with _app_client() as client:
        await _login_superadmin(client)
        safety = await client.get("/admin/system/safety")

    assert safety.status_code == 200
    assert "安全控制" in safety.text
    assert 'name="scope" value="global"' in safety.text
    assert 'name="enabled" value="true"' in safety.text
    assert 'name="bootstrap_password"' in safety.text


async def test_killswitch_toggle_sets_flag(migrated_db):
    import redis.asyncio as aioredis

    from social_reply.shared.config import get_settings

    settings = get_settings()
    key = f"killswitch:global:{settings.tenant_id}"
    redis = aioredis.from_url(settings.redis_url)
    await redis.delete(key)  # 清理前置状态
    try:
        async with _app_client() as client:
            csrf = await _login_superadmin(client)
            resp = await client.post(
                "/admin/killswitch/toggle",
                data={
                    "csrf_token": csrf,
                    "scope": "global",
                    "tenant_id": settings.tenant_id,
                    "enabled": "true",
                    "bootstrap_password": "test-admin-password",
                },
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/admin/system/safety"
        assert await redis.get(key) is not None  # 已置急停
        # 再次切换应解除
        async with _app_client() as client:
            csrf = await _login_superadmin(client)
            await client.post(
                "/admin/killswitch/toggle",
                data={
                    "csrf_token": csrf,
                    "scope": "global",
                    "tenant_id": settings.tenant_id,
                    "enabled": "false",
                    "bootstrap_password": "test-admin-password",
                },
            )
        assert await redis.get(key) is None
    finally:
        await redis.delete(key)
        await redis.aclose()
