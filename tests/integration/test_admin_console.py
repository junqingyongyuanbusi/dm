import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import insert, select, update

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
            assert resp.headers["location"] == "/admin/login"


async def test_console_pages_render_after_login(migrated_db):
    async with _app_client() as client:
        await _login(client)
        for path, marker in (
            ("/admin", "总览"),
            ("/admin/inbox", "收件箱"),
            ("/admin/conversations", "对话"),
            ("/admin/knowledge", "知识库"),
            ("/admin/accounts", "账号"),
            ("/admin/health", "系统健康"),
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert marker in resp.text
            if path == "/admin/accounts":
                assert "账号自动化策略" in resp.text
                assert "新会话默认" not in resp.text


async def test_grouped_navigation_uses_new_information_architecture(migrated_db):
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/admin/inbox")

    assert page.status_code == 200
    assert '<div class="app-shell app-shell-nav"><aside class="sidebar">' in page.text
    for group in ("运营", "内容与策略", "集成", "系统"):
        assert group in page.text
    for path, label in (
        ("/admin/content/knowledge", "知识库"),
        ("/admin/content/brand-voice", "品牌语气"),
        ("/admin/integrations/accounts", "平台账号"),
        ("/admin/integrations/feishu/handoff", "Feishu 人工通知"),
        ("/admin/system/health", "系统健康"),
        ("/admin/system/safety", "安全控制"),
        ("/admin/system/users", "用户管理"),
    ):
        assert f'href="{path}"' in page.text
        assert label in page.text
    assert "aria-current='page'" in page.text
    assert 'href="#main-content">跳到主内容</a>' in page.text
    assert '<span class="nav-heading">运营</span>' in page.text


async def test_new_page_routes_render_and_legacy_routes_remain_available(migrated_db):
    async with _app_client() as client:
        await _login(client)
        pairs = (
            ("/admin/content/knowledge", "/admin/knowledge", "知识库"),
            ("/admin/content/brand-voice", "/admin/prompt", "品牌语气"),
            ("/admin/integrations/accounts", "/admin/accounts", "平台账号"),
            ("/admin/system/health", "/admin/health", "系统健康"),
        )
        for current, legacy, marker in pairs:
            current_response = await client.get(current)
            legacy_response = await client.get(legacy)
            assert current_response.status_code == 200
            assert legacy_response.status_code == 200
            assert marker in current_response.text
            assert marker in legacy_response.text


async def test_account_connection_routes_are_deep_linkable(migrated_db):
    async with _app_client() as client:
        await _login(client)
        index = await client.get("/admin/integrations/accounts")
        telegram = await client.get("/admin/integrations/accounts/new/telegram")
        missing = await client.get("/admin/integrations/accounts/new/not-a-provider")

    assert index.status_code == 200
    assert 'href="/admin/integrations/accounts/new/telegram"' in index.text
    assert telegram.status_code == 200
    assert 'action="/admin/connect/telegram"' in telegram.text
    assert "aria-current='page'" in telegram.text
    assert missing.status_code == 404


async def test_navigation_does_not_poll_inbox_counts(migrated_db):
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/admin/inbox")
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
        response = await client.get("/admin/health")

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
        human = await client.get("/admin/inbox")
        drafts = await client.get("/admin/inbox?queue=drafts")
        delivery = await client.get("/admin/inbox?queue=delivery")
        filtered = await client.get("/admin/inbox?queue=human&reason=LLM_UNAVAILABLE")

    assert human.status_code == 200
    assert '<meta http-equiv="refresh" content="20">' in human.text
    assert '<meta http-equiv="refresh" content="20">' not in drafts.text
    assert "<strong>2</strong><span>待人工" in human.text
    assert "待人工 · 最老 3 小时" in human.text
    assert "待审核 · 最老" in human.text
    assert "投递异常 · 最老" in human.text
    assert human.text.index("Old customer") < human.text.index("New customer")
    assert "Original draft" in drafts.text
    assert f'action="/admin/decisions/{decision_id}/approve"' in drafts.text
    assert "发送结果不确定" in delivery.text
    assert "Old customer" in filtered.text and "New customer" not in filtered.text


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
                "/admin/inbox", params={"queue": queue, "channel": channel}
            )
            for queue in ("human", "drafts", "delivery")
            for channel in ("all", "dm", "comment")
        }
        conversation_pages = {
            channel: await client.get("/admin/conversations", params={"channel": channel})
            for channel in ("all", "dm", "comment")
        }
        count_responses = {
            channel: await client.get("/admin/inbox/counts", params={"channel": channel})
            for channel in ("all", "dm", "comment")
        }
        facebook_conversations = await client.get(
            "/admin/conversations", params={"platform": "facebook"}
        )
        mention_detail = await client.get(f"/admin/conversations/{seeded['mention'][1]}")

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
            for name in all_names - expected_names:
                assert name not in page.text

    for channel, expected_names in (
        ("all", all_names),
        ("dm", dm_names),
        ("comment", comment_names),
    ):
        page = conversation_pages[channel]
        assert page.status_code == 200
        for name in expected_names:
            assert name in page.text
        for name in all_names - expected_names:
            assert name not in page.text

    assert "自动化状态" not in conversation_pages["all"].text
    assert count_responses["all"].json() == {"human": 3, "drafts": 3, "delivery": 3}
    assert count_responses["dm"].json() == {"human": 1, "drafts": 1, "delivery": 1}
    assert count_responses["comment"].json() == {"human": 2, "drafts": 2, "delivery": 2}
    assert "Channel comment customer" in facebook_conversations.text
    assert "Channel DM customer" not in facebook_conversations.text
    assert "Channel mention customer" not in facebook_conversations.text

    assert mention_detail.status_code == 200
    assert "公开评论回复" in mention_detail.text
    assert 'name="reply_to_message_id"' in mention_detail.text
    assert f'value="{seeded["mention"][2]}"' in mention_detail.text
    assert "in_reply_to_post_id" in mention_detail.text
    assert "post-1" in mention_detail.text


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
        inbox = await client.get("/admin/inbox", params={"platform": "email"})
        conversations = await client.get("/admin/conversations", params={"platform": "email"})
        counts = await client.get("/admin/inbox/counts", params={"platform": "email"})

    assert inbox.status_code == 200
    assert conversations.status_code == 200
    assert counts.status_code == 200
    assert '<option value="email" selected>email</option>' in inbox.text
    assert '<option value="email" selected>email</option>' in conversations.text
    assert "Email filter customer" in inbox.text
    assert "Email filter customer" in conversations.text
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
    queued_message_id, empty_message_id = uuid.uuid4(), uuid.uuid4()
    for candidate_id, text in (
        (queued_message_id, "Already queued inbound"),
        (empty_message_id, "Empty draft inbound"),
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
    for decision_message_id, reply_text, linked_outbox in (
        (message_id, "Ready for review", None),
        (queued_message_id, "Already queued", outbox_id),
        (empty_message_id, "   ", None),
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
                outbox_id=linked_outbox,
            )
        )
    await session.commit()

    async with _app_client() as client:
        await _login(client)
        page = await client.get("/admin/inbox", params={"queue": "drafts"})
        counts = await client.get("/admin/inbox/counts")

    assert page.status_code == 200
    assert "Ready for review" in page.text
    assert "Already queued" not in page.text
    assert counts.json()["drafts"] == 1


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


@pytest.mark.parametrize(
    ("mismatch", "expected_relation"),
    [
        ("contact_tenant", "contact"),
        ("contact_account", "contact"),
        ("reply_decision", "reply_decision"),
        ("outbox_tenant", "outbox_message"),
        ("outbox_account", "outbox_message"),
        ("message_source_outbox", "message_source_outbox"),
        ("audit_log", "audit_log"),
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
        healthy_detail = await client.get(f"/admin/conversations/{conversation_id}")

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
        mismatched_detail = await client.get(f"/admin/conversations/{conversation_id}")

    assert healthy_detail.status_code == 200
    assert f"Safe customer {mismatch}" in healthy_detail.text
    assert local_decision_text in healthy_detail.text
    assert local_outbox_text in healthy_detail.text
    assert mismatched_detail.status_code == 404
    assert foreign_secret not in mismatched_detail.text
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
        detail = await client.get(f"/admin/conversations/{conversation_id}")
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

    assert detail.status_code == 200
    assert "私信回复" in detail.text
    assert "telegram_dm" in detail.text
    assert "4096 字符" in detail.text
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
        waiting = await client.get(f"/admin/conversations/{conversation_id}")
        assert "认领并接管" in waiting.text
        claimed_response = await client.post(
            f"/admin/work-items/{work_item_id}/claim",
            data={"csrf_token": csrf, "version": "1"},
        )
        assert claimed_response.status_code == 303
        claimed = await client.get(f"/admin/conversations/{conversation_id}")
        assert "解决并恢复草稿模式" in claimed.text
        resolved_response = await client.post(
            f"/admin/work-items/{work_item_id}/resolve",
            data={"csrf_token": csrf, "version": "2"},
        )
        assert resolved_response.status_code == 303
        resolved = await client.get(f"/admin/conversations/{conversation_id}")

    session.expire_all()
    work = await session.get(models.HumanWorkItem, work_item_id)
    state = await session.get(models.AutomationState, conversation_id)
    assert work.status == "RESOLVED"
    assert state.state == "BOT_DRAFT_ONLY"
    assert "恢复为草稿" not in resolved.text
    assert "恢复自动" not in resolved.text


async def test_conversation_detail_uses_latest_200_messages_for_reply_target(session, migrated_db):
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
        detail = await client.get(f"/admin/conversations/{conversation_id}")

    assert detail.status_code == 200
    assert "Message history-window" not in detail.text
    assert detail.text.index("History message 001") < detail.text.index("History message 200")
    latest_choice = re.search(
        rf'name="reply_to_message_id" value="{message_ids[-1]}" checked required',
        detail.text,
    )
    assert latest_choice is not None
    assert "history-200" in detail.text


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
        )
    )
    await session.commit()

    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/discard",
            data={"csrf_token": csrf, "review_reason": "Tone is not suitable"},
        )
        reviewed = await client.get("/admin/inbox?queue=drafts&status=REJECTED")
        detail = await client.get(f"/admin/conversations/{conversation_id}")

    assert response.status_code == 303
    assert reviewed.status_code == 200
    assert "REJECTED" in reviewed.text
    assert "Unsafe draft" in reviewed.text
    assert "Tone is not suitable" in reviewed.text
    assert "user:admin" in reviewed.text
    assert f'action="/admin/decisions/{decision_id}/approve"' not in reviewed.text
    assert "REJECT_DRAFT" in detail.text
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
        )
    )
    await session.commit()

    async def fake_dispatch(*_args, **_kwargs):
        return None

    from social_reply.application.account_management import admin_console

    monkeypatch.setattr(admin_console, "dispatch_actor", fake_dispatch)
    async with _app_client() as client:
        csrf = await _login(client)
        response = await client.post(
            f"/admin/decisions/{decision_id}/approve",
            data={"csrf_token": csrf, "final_reply_text": "Edited human reply"},
        )

    assert response.status_code == 303
    session.expire_all()
    decision = await session.get(models.ReplyDecision, decision_id)
    outbox = await session.get(models.OutboxMessage, decision.outbox_id)
    assert decision.original_reply_text == "Original reply"
    assert decision.final_reply_text == "Edited human reply"
    assert decision.review_action == "EDITED"
    assert decision.reviewed_by == "user:admin"
    assert outbox.payload["text"] == "Edited human reply"
    assert outbox.reply_to_message_id == message_id
    assert outbox.origin_kind == "DRAFT_APPROVAL"
    assert outbox.actor_kind == "ADMIN_HUMAN"


async def test_accounts_page_renders_seven_channel_tiles(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

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
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")

    assert response.status_code == 200
    html = response.text
    assert "添加渠道" in html
    assert f"{len(admin_console._CHANNEL_LABELS)} 个平台" in html
    assert html.count('class="channel-tile"') == len(admin_console._CHANNEL_LABELS)
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
    assert 'name="automation_default" value="BOT_DRAFT_ONLY"' in facebook_page.text
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
    assert 'name="automation_default" value="BOT_DRAFT_ONLY"' in instagram_page.text


async def test_accounts_page_renders_manual_channel_panels(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(
        update={"whatsapp_enabled": True, "feishu_enabled": True}
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        telegram_page = await client.get("/admin/accounts?connect=telegram")
        whatsapp_page = await client.get("/admin/accounts?connect=whatsapp")
        feishu_page = await client.get("/admin/accounts?connect=feishu")

    assert 'action="/admin/connect/telegram"' in telegram_page.text
    assert "@BotFather" in telegram_page.text
    assert 'name="token"' in telegram_page.text
    assert 'action="/admin/connect/whatsapp"' in whatsapp_page.text
    assert "Phone Number ID" in whatsapp_page.text
    assert 'name="access_token"' in whatsapp_page.text
    assert 'name="verify_token"' in whatsapp_page.text
    assert 'action="/admin/connect/feishu"' in feishu_page.text
    assert 'name="app_id"' in feishu_page.text
    assert 'name="app_secret"' in feishu_page.text
    assert 'name="verification_token"' in feishu_page.text
    assert 'name="encrypt_key"' in feishu_page.text
    assert 'name="automation_default" value="BOT_DRAFT_ONLY"' in feishu_page.text
    assert 'name="group_mode" value="mentions_only"' in feishu_page.text


async def test_accounts_page_renders_email_connection_form_and_icon(migrated_db, monkeypatch):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        page = await client.get("/admin/accounts?connect=email")
        icon = await client.get("/static/channel-icons/email.svg")

    assert page.status_code == 200
    assert icon.status_code == 200
    assert "<svg" in icon.text and "<path" in icon.text
    assert 'data-channel="email"' in page.text
    assert "/static/channel-icons/email.svg" in page.text
    assert 'action="/admin/connect/email"' in page.text
    for label in (
        "Email Address",
        "From Name（可选）",
        "Username",
        "Password",
        "IMAP Host",
        "IMAP Port",
        "SMTP Host",
        "SMTP Port（留空按加密方式默认）",
        "SMTP Security",
        "Mailbox",
        "同域内部邮件",
    ):
        assert label in page.text
    assert 'data-channel="email" aria-current="true"' in page.text
    assert 'aria-current="page"' not in page.text
    assert 'role="listitem"' not in page.text
    assert re.search(
        r'<input[^>]+type="email"[^>]+name="email_address"[^>]+autocomplete="email"[^>]*>',
        page.text,
    )
    assert 'name="username" required autocomplete="username"' in page.text
    assert 'name="imap_host" value="imap.larksuite.com"' in page.text
    assert re.search(
        r'<input[^>]+type="number"[^>]+name="imap_port"[^>]+value="993"[^>]+'
        r'inputmode="numeric"[^>]+min="1"[^>]+max="65535"[^>]*>',
        page.text,
    )
    assert 'name="smtp_host" value="smtp.larksuite.com"' in page.text
    smtp_port_input = re.search(r'<input[^>]+name="smtp_port"[^>]*>', page.text)
    assert smtp_port_input is not None
    assert 'type="number"' in smtp_port_input.group(0)
    assert 'inputmode="numeric"' in smtp_port_input.group(0)
    assert 'min="1"' in smtp_port_input.group(0)
    assert 'max="65535"' in smtp_port_input.group(0)
    assert "value=" not in smtp_port_input.group(0)
    assert '<option value="ssl" selected>SSL（默认 465）</option>' in page.text
    assert '<option value="starttls">STARTTLS（默认 587）</option>' in page.text
    assert 'name="mailbox" value="INBOX"' in page.text
    assert '<option value="ignore" selected>忽略（推荐）</option>' in page.text
    assert '<option value="allow">允许进入处理流程</option>' in page.text
    assert "默认忽略同域来信，以降低自动回复循环风险。" in page.text
    assert 'name="automation_default" value="BOT_DRAFT_ONLY"' in page.text
    password_input = re.search(r'<input[^>]+type="password"[^>]+name="password"[^>]*>', page.text)
    assert password_input is not None
    assert 'autocomplete="current-password"' in password_input.group(0)
    assert "value=" not in password_input.group(0)
    assert "channel-form-grid" in page.text
    assert "@media (max-width:720px)" in page.text
    assert ".channel-form-grid{grid-template-columns:1fr}" in page.text
    assert ".channel-form-grid .span-2{grid-column:auto}" in page.text


async def test_accounts_page_renders_email_sanitized_health_without_password(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"email_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
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
        response = await client.get("/admin/accounts")

    assert response.status_code == 200
    assert "Support Email" in response.text
    assert "接入探测" in response.text
    assert "通过" in response.text
    assert "Mailbox INBOX" in response.text
    assert "Security ssl" in response.text
    assert "探测时间 2026-08-03 00:00 UTC" in response.text
    assert "2026-08-03T00:00:00+00:00" not in response.text
    assert "仅表示最近一次凭证接入验证，不是持续监控" in response.text
    assert "Health" not in response.text
    assert "AUTH_FAILEDmail-password-must-not-leakscript" not in response.text
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
    from social_reply.application.account_management import admin

    captured = {}
    submissions = []
    disabled_settings = admin.get_settings().model_copy(update={"email_enabled": False})
    enabled_settings = disabled_settings.model_copy(update={"email_enabled": True})
    monkeypatch.setattr(admin, "get_settings", lambda: disabled_settings)

    async def fake_submit(**kwargs):
        submissions.append(kwargs)
        captured.update(kwargs)
        return uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    async def fake_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "submit_provisioning_job", fake_submit)
    monkeypatch.setattr(admin, "dispatch_actor", fake_dispatch)
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
        monkeypatch.setattr(admin, "get_settings", lambda: enabled_settings)
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
        monkeypatch.setattr(admin, "get_settings", lambda: disallowed_settings)
        disallowed = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload},
        )
        monkeypatch.setattr(admin, "get_settings", lambda: enabled_settings)
        submitted = await client.post("/admin/connect/email", data={"csrf_token": csrf, **payload})
        starttls_submitted = await client.post(
            "/admin/connect/email",
            data={"csrf_token": csrf, **payload, "smtp_security": "starttls"},
        )

    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/admin/login"
    assert bad_csrf.status_code == 403
    assert wrong_tenant.status_code == 403
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "email_integration_disabled"
    assert extra.status_code == 422
    assert active.status_code == 422
    assert blank_username.status_code == 422
    assert blank_password.status_code == 422
    assert oversized_password.status_code == 422
    assert invalid_brand.status_code == 422
    assert disallowed.status_code == 422
    assert disallowed.json()["detail"] == "email_hostname_not_allowed"
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
    assert submissions[0]["platform"] == "email"
    assert submissions[0]["tenant_id"] == "default"
    assert submissions[0]["request"] == {
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
    assert submissions[0]["secrets"] == {
        "username": "mail-user",
        "password": "mail-password-must-not-leak",
    }
    assert submissions[1]["request"]["smtp_security"] == "starttls"
    assert submissions[1]["request"]["smtp_port"] == 587
    assert type(submissions[1]["request"]["smtp_port"]) is int
    assert captured == submissions[1]
    assert not set(submissions[0]["request"]) & {"username", "password"}


async def test_accounts_page_renders_feishu_sanitized_channel_health(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
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
        response = await client.get("/admin/accounts")

    assert response.status_code == 200
    assert "Health" in response.text
    assert "READY" in response.text
    assert "Support Bot" in response.text
    assert "2026-08-03T00:00:00+00:00" in response.text
    assert "verification_token" not in response.text
    assert "encrypt_key" not in response.text


async def test_feishu_account_can_be_explicitly_promoted_after_provisioning(
    session, migrated_db, monkeypatch
):
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update={"feishu_enabled": True})
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
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
    from social_reply.application.account_management import admin_console

    settings = admin_console.get_settings().model_copy(update=settings_update)
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
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
        page = await client.get("/admin/accounts")
        rejected = await client.post(
            f"/admin/accounts/{draft_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE"},
        )
        converged = await client.post(
            f"/admin/accounts/{legacy_active_id}/automation",
            data={"csrf_token": csrf, "target": "BOT_DRAFT_ONLY"},
        )

    assert f'action="/admin/accounts/{draft_id}/automation"' not in page.text
    assert f'action="/admin/accounts/{legacy_active_id}/automation"' in page.text
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
            "feishu_enabled": False,
        }
    )
    monkeypatch.setattr(admin_console, "get_settings", lambda: settings)
    async with _app_client() as client:
        await _login(client)
        response = await client.get("/admin/accounts")
        disabled = await client.get("/admin/accounts?connect=instagram")

    assert response.status_code == 200
    for channel in ("facebook", "instagram", "whatsapp", "feishu"):
        assert f'data-channel="{channel}" aria-disabled="true"' in response.text
        assert f'href="/admin/accounts?connect={channel}' not in response.text
    assert 'role="listitem"' not in response.text
    assert 'action="/admin/oauth/meta/start"' not in response.text
    assert 'action="/admin/oauth/instagram/start"' not in response.text
    assert 'action="/admin/connect/whatsapp"' not in response.text
    assert 'action="/admin/connect/feishu"' not in response.text
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
    assert 'data-channel="x" aria-disabled="true"' in response.text
    assert 'role="listitem"' not in response.text
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
        page = await client.get(f"/admin/jobs/{job_id}")
        retry = await client.post(
            f"/admin/jobs/{job_id}/retry",
            data={"csrf_token": csrf},
        )

    assert page.status_code == 200
    assert "返回账号页重新提交 Email 账号和密码" in page.text
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
        detail = await client.get(f"/admin/conversations/{conversation_id}")
        rejected = await client.post(
            f"/admin/conversations/{conversation_id}/state",
            data={"csrf_token": csrf, "target": "BOT_ACTIVE", "expect": "CLOSED"},
        )
        takeover = await client.post(
            f"/admin/conversations/{conversation_id}/state",
            data={"csrf_token": csrf, "target": "HUMAN_ACTIVE", "expect": "CLOSED"},
        )

    assert detail.status_code == 200
    assert 'name="target" value="BOT_ACTIVE"' not in detail.text
    assert 'name="target" value="HUMAN_ACTIVE"' in detail.text
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
        assert "notice=added" in resp.headers["location"]

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
        assert "notice=added" in added.headers["location"]

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
        "brand": "b1",
        "platform": "telegram",
        "is_official_contact": True,
    }
    assert audits[1].detail["from"] == "published"
    assert audits[1].detail["to"] == "draft"


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
    await session.commit()
    normal_id = normal.id
    normal_two_id = normal_two.id
    official_id = official.id
    unclassified_contact_id = unclassified_contact.id
    published_id = already_published.id
    foreign_id = foreign.id

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/knowledge")
        assert page.status_code == 200
        assert 'action="/admin/knowledge/bulk-publish"' in page.text
        assert "批量发布普通草稿" in page.text

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
    assert "notice=bulk_published&count=2" in first.headers["location"]
    assert "notice=bulk_published&count=0" in second.headers["location"]
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
    await session.commit()
    english_id = english.id
    other_id = same_filename_other_batch.id
    mixed_id = mixed.id

    async with _app_client() as client:
        csrf = await _login(client)
        page = await client.get("/admin/knowledge")
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
        await client.post(
            f"/admin/knowledge/{doc_id}/status",
            data={"csrf_token": csrf, "target": "published"},
        )
        published_change = await client.post(
            f"/admin/knowledge/{doc_id}/official-contact",
            data={"csrf_token": csrf, "target": "false"},
        )
    assert classified.status_code == same_target.status_code == 303
    assert published_change.status_code == 409
    session.expire_all()
    stored = await session.get(models.KnowledgeDocument, doc_id)
    assert stored.is_official_contact is True
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
        "brand": "b1",
        "platform": None,
        "status": "draft",
        "content_hash": "c" * 64,
    }


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


async def test_global_killswitch_has_separate_safety_page(migrated_db):
    async with _app_client() as client:
        await _login(client)
        accounts = await client.get("/admin/integrations/accounts")
        safety = await client.get("/admin/system/safety")

    assert accounts.status_code == 200
    assert "自动回复总开关" not in accounts.text
    assert safety.status_code == 200
    assert "安全控制" in safety.text
    assert 'name="scope" value="global"' in safety.text


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
            assert resp.headers["location"] == "/admin/system/safety"
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
