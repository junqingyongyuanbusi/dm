import uuid

from sqlalchemy import insert

from social_reply.application.account_management import meta_credentials
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import encrypt_secret_bundle


async def test_facebook_credentials_prefer_environment(monkeypatch):
    class Settings:
        facebook_app_credentials = ("env-app", "env-secret")

        class Token:
            @staticmethod
            def get_secret_value():
                return "env-verify"

        meta_verify_token = Token()

    monkeypatch.setattr(meta_credentials, "get_settings", lambda: Settings())
    credentials = await meta_credentials.facebook_app_credentials("default")
    assert credentials is not None
    assert credentials.app_id == "env-app"
    assert credentials.public_id == "meta_oauth_default"


async def test_environment_meta_app_public_id_is_tenant_scoped(monkeypatch):
    class Settings:
        facebook_app_credentials = ("env-app", "env-secret")

        class Token:
            @staticmethod
            def get_secret_value():
                return "env-verify"

        meta_verify_token = Token()

    monkeypatch.setattr(meta_credentials, "get_settings", lambda: Settings())
    tenant_a = await meta_credentials.facebook_app_credentials("tenant-a")
    tenant_b = await meta_credentials.facebook_app_credentials("tenant-b")
    assert tenant_a is not None and tenant_b is not None
    assert tenant_a.public_id == "meta_oauth_tenant-a"
    assert tenant_b.public_id == "meta_oauth_tenant-b"


async def test_facebook_credentials_fall_back_to_tenant_platform_app(
    session, monkeypatch
):
    class EmptySettings:
        facebook_app_credentials = None

        class Token:
            @staticmethod
            def get_secret_value():
                return ""

        meta_verify_token = Token()

    monkeypatch.setattr(meta_credentials, "get_settings", lambda: EmptySettings())
    await session.execute(
        insert(models.PlatformApp).values(
            id=uuid.uuid4(),
            tenant_id="tenant-a",
            platform_family="meta",
            name="Stored Meta",
            external_app_id="stored-app",
            public_id="stored-public",
            credential_bundle=encrypt_secret_bundle(
                {"app_secret": "stored-secret", "verify_token": "stored-verify"}
            ),
            config={},
            status="active",
        )
    )
    await session.commit()

    credentials = await meta_credentials.facebook_app_credentials("tenant-a")
    assert credentials is not None
    assert credentials.app_id == "stored-app"
    assert credentials.app_secret == "stored-secret"
    assert credentials.verify_token == "stored-verify"
    assert credentials.public_id == "stored-public"
