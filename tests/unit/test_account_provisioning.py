from social_reply.application.account_management.provisioning import make_public_id


def test_make_public_id_is_platform_scoped_and_url_safe():
    value = make_public_id("ig")
    assert value.startswith("ig_")
    assert "-" not in value
