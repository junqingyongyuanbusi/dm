import asyncio
import uuid

import pytest
from sqlalchemy import insert, update

from social_reply.connectors import registry
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle

pytestmark = pytest.mark.integration


async def test_meta_sender_cache_replaces_client_after_app_secret_rotation(session, monkeypatch):
    app_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="meta",
            name="Meta",
            external_app_id="app-1",
            public_id=f"meta_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "secret-1", "verify_token": "verify"}
            ),
            config={},
            config_version=1,
            status="active",
        )
    )
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="facebook",
            platform_app_id=app_id,
            name="Page",
            external_account_id="page-1",
            public_id=f"fb_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle({"access_token": "page-token"}),
            config={"delivery_mode": "direct", "instagram_login_mode": "facebook_login"},
            capability={"dm": True, "comments": False, "max_text_length": 2000},
            config_version=1,
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    created = []

    class FakeMetaClient:
        def __init__(self, **kwargs):
            self.app_secret = kwargs["app_secret"]
            self.closed = False
            created.append(self)

        async def send_text(self, *, target, text):
            return f"{target}:{text}"

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(registry, "MetaGraphClient", FakeMetaClient)
    registry._senders.clear()

    first = await registry.get_platform_sender(account_id)
    assert first.app_secret == "secret-1"

    await session.execute(
        update(models.PlatformApp)
        .where(models.PlatformApp.id == app_id)
        .values(
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "secret-2", "verify_token": "verify"}
            ),
            config_version=2,
        )
    )
    await session.commit()

    second = await registry.get_platform_sender(account_id)
    assert second.app_secret == "secret-2"
    assert second is not first
    assert first.closed is True
    await registry.close_platform_senders()


async def test_feishu_sender_cache_rotates_on_account_config_version(session, monkeypatch):
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="feishu",
            name="Feishu",
            external_account_id="cli_12345678",
            public_id=f"fs_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"app_id": "cli_12345678", "app_secret": "secret-1"}
            ),
            config={"delivery_mode": "direct", "api_base_url": "https://attacker.example"},
            capability={"dm": True, "mentions": True, "max_text_length": 4000},
            config_version=1,
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    created = []

    class FakeFeishuClient:
        def __init__(self, **kwargs):
            self.app_secret = kwargs["app_secret"]
            self.api_base_url = kwargs["api_base_url"]
            self.closed = False
            self.close_calls = 0
            created.append(self)

        async def send_text(self, *, target, text):
            return f"{target}:{text}"

        async def aclose(self):
            self.close_calls += 1
            self.closed = True

    monkeypatch.setattr(registry, "FeishuClient", FakeFeishuClient)
    registry._senders.clear()

    first, cached = await asyncio.gather(
        registry.get_platform_sender(account_id),
        registry.get_platform_sender(account_id),
    )
    assert cached is first
    assert len(created) == 1
    assert first.app_secret == "secret-1"
    assert first.api_base_url == "https://open.feishu.cn"

    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(
            credential_bundle=encrypt_secret_bundle(
                {"app_id": "cli_12345678", "app_secret": "secret-2"}
            ),
            config_version=2,
        )
    )
    await session.commit()

    second, rotated_cached = await asyncio.gather(
        registry.get_platform_sender(account_id),
        registry.get_platform_sender(account_id),
    )
    assert rotated_cached is second
    assert second is not first
    assert second.app_secret == "secret-2"
    assert first.closed is True
    assert first.close_calls == 1
    assert len(created) == 2
    await asyncio.gather(
        registry.close_platform_senders(),
        registry.close_platform_senders(),
    )
    assert second.closed is True
    assert second.close_calls == 1
    assert registry._senders == {}

    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(external_account_id="cli_other123", config_version=3)
    )
    await session.commit()
    with pytest.raises(LookupError, match="feishu_app_id_scope_mismatch"):
        await registry.get_platform_sender(account_id)


async def test_email_sender_cache_rotates_on_account_config_version(session, monkeypatch):
    account_id = uuid.uuid4()
    config = {
        "smtp_host": " SMTP.Example.COM. ",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "self_address": " Support@Example.COM ",
        "from_name": "Customer Support",
    }
    await session.execute(
        insert(models.PlatformAccount).values(
            id=account_id,
            tenant_id="default",
            brand_id="default",
            platform="email",
            name="Email",
            external_account_id="Support@example.com",
            public_id=f"email_{uuid.uuid4().hex}",
            credential_bundle=encrypt_secret_bundle(
                {"username": "smtp-user", "password": "password-1"}
            ),
            config=config,
            capability={"dm": True, "max_text_length": 4000},
            config_version=1,
            automation_default="BOT_DRAFT_ONLY",
            status="active",
        )
    )
    await session.commit()

    created = []

    class FakeEmailClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        async def send_text(self, *, target, text):
            return f"{target}:{text}"

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(registry, "EmailClient", FakeEmailClient)
    registry._senders.clear()

    first = await registry.get_platform_sender(account_id)
    assert first.kwargs["password"] == "password-1"
    assert await registry.get_platform_sender(account_id) is first
    assert len(created) == 1

    await session.execute(
        update(models.PlatformAccount)
        .where(models.PlatformAccount.id == account_id)
        .values(
            credential_bundle=encrypt_secret_bundle(
                {"username": "smtp-user", "password": "password-2"}
            ),
            config_version=2,
        )
    )
    await session.commit()

    second = await registry.get_platform_sender(account_id)
    assert second is not first
    assert second.kwargs["password"] == "password-2"
    assert first.closed is True
    await registry.close_platform_senders()
