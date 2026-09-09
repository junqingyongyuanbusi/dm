import uuid

import pytest
from sqlalchemy import insert, select

from social_reply.application.account_management.provisioning import provision_platform_app
from social_reply.infrastructure.database import models
from social_reply.infrastructure.secret_crypto import (
    decrypt_secret_bundle,
    encrypt_secret_bundle,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("external_app_id", "consumer_secret", "expected_error"),
    [
        ("old-key", "old-secret", None),
        ("old-key", "new-secret", "platform_app_rotation_required"),
        ("new-key", "new-secret", "platform_app_public_id_external_id_mismatch"),
    ],
)
async def test_x_platform_app_reuse_cannot_implicitly_rotate_identity_or_credentials(
    session, tmp_path, external_app_id, consumer_secret, expected_error
):
    app_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await session.execute(
        insert(models.PlatformApp).values(
            id=app_id,
            tenant_id="default",
            platform_family="x",
            name="Old X App",
            external_app_id="old-key",
            public_id="x_oauth_default",
            credential_bundle=encrypt_secret_bundle(
                {"consumer_key": "old-key", "consumer_secret": "old-secret"}
            ),
            config={"api_base_url": "https://api.x.com"},
            config_version=3,
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
            name="@account",
            external_account_id="user-1",
            public_id="x_account",
            credential_bundle=encrypt_secret_bundle(
                {
                    "consumer_key": "old-key",
                    "consumer_secret": "old-secret",
                    "access_token": "token",
                    "access_token_secret": "token-secret",
                }
            ),
            config={},
            capability={},
            status="active",
        )
    )
    await session.commit()

    provision_values = dict(
        platform_family="x",
        external_app_id=external_app_id,
        tenant_id="default",
        name="X OAuth App",
        public_id="x_oauth_default",
        public_id_prefix="xapp",
        secrets_root=tmp_path,
        credential_bundle={"consumer_key": external_app_id, "consumer_secret": consumer_secret},
        config={"api_base_url": "https://api.x.com"},
        allow_external_app_id_rotation=True,
    )
    if expected_error is None:
        persisted_id, public_id = await provision_platform_app(**provision_values)
        assert persisted_id == app_id
        assert public_id == "x_oauth_default"
    else:
        with pytest.raises(ValueError, match=expected_error):
            await provision_platform_app(**provision_values)

    session.expire_all()
    app = await session.get(models.PlatformApp, app_id)
    account = await session.get(models.PlatformAccount, account_id)
    assert app is not None
    assert app.external_app_id == "old-key"
    assert app.public_id == "x_oauth_default"
    assert app.config_version == (4 if expected_error is None else 3)
    assert app.name == ("X OAuth App" if expected_error is None else "Old X App")
    assert decrypt_secret_bundle(app.credential_bundle) == {
        "consumer_key": "old-key",
        "consumer_secret": "old-secret",
    }
    assert account is not None
    assert account.platform_app_id == app_id
    assert decrypt_secret_bundle(account.credential_bundle) == {
        "consumer_key": "old-key",
        "consumer_secret": "old-secret",
        "access_token": "token",
        "access_token_secret": "token-secret",
    }
    rows = (await session.scalars(select(models.PlatformApp))).all()
    assert len(rows) == 1


async def test_platform_app_external_identity_remains_immutable(session, tmp_path):
    await session.execute(
        insert(models.PlatformApp).values(
            id=uuid.uuid4(),
            tenant_id="default",
            platform_family="meta",
            name="Meta App",
            external_app_id="old-app",
            public_id="meta_oauth_default",
            credential_bundle=encrypt_secret_bundle({"app_secret": "old-secret"}),
            config={},
            status="active",
        )
    )
    await session.commit()

    with pytest.raises(ValueError, match="platform_app_public_id_external_id_mismatch"):
        await provision_platform_app(
            platform_family="meta",
            external_app_id="new-app",
            tenant_id="default",
            name="Meta App",
            public_id="meta_oauth_default",
            public_id_prefix="metaapp",
            secrets_root=tmp_path,
            credential_bundle={"app_secret": "new-secret"},
            config={},
        )

    rows = (
        (
            await session.execute(
                select(models.PlatformApp).where(models.PlatformApp.platform_family == "meta")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].external_app_id == "old-app"
