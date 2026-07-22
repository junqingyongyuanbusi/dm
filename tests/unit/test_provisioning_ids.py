from social_reply.application.account_management.provisioning import tenant_public_id


def test_tenant_public_id_is_stable_and_url_safe():
    assert tenant_public_id("meta_oauth", "tenant-a") == "meta_oauth_tenant-a"
    assert tenant_public_id("meta_oauth", "客户 A") == "meta_oauth_A_ca642480"
