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
