import uuid

import pytest

from social_reply.application.account_management.provisioning import provision_platform_app

pytestmark = pytest.mark.integration


async def test_meta_webhook_public_id_cannot_cross_app_families(migrated_db, tmp_path):
    public_id = f"meta_shared_{uuid.uuid4().hex}"
    await provision_platform_app(
        platform_family="meta",
        external_app_id=f"fb-app-{uuid.uuid4().hex}",
        tenant_id="tenant-a",
        name="Facebook App",
        public_id=public_id,
        public_id_prefix="meta",
        secrets_root=tmp_path,
        credential_bundle={"app_secret": "secret", "verify_token": "verify"},
        config={"api_version": "v23.0"},
    )

    with pytest.raises(ValueError, match="meta_webhook_public_id_collision"):
        await provision_platform_app(
            platform_family="instagram",
            external_app_id=f"ig-app-{uuid.uuid4().hex}",
            tenant_id="tenant-b",
            name="Instagram App",
            public_id=public_id,
            public_id_prefix="instagram",
            secrets_root=tmp_path,
            credential_bundle={"app_secret": "secret", "verify_token": "verify"},
            config={"api_version": "v23.0"},
        )
