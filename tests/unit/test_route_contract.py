from apps.api.main import create_app

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _route_methods() -> dict[str, frozenset[str]]:
    return {
        path: frozenset(method.upper() for method in operations if method in _HTTP_METHODS)
        for path, operations in create_app().openapi()["paths"].items()
    }


def test_external_protocol_and_admin_routes_remain_stable() -> None:
    routes = _route_methods()

    assert routes["/healthz"] == {"GET"}
    assert routes["/webhooks/chatwoot"] == {"POST"}
    assert routes["/webhooks/telegram/{public_id}"] == {"POST"}
    assert routes["/webhooks/meta/{app_public_id}"] == {"GET", "POST"}
    assert routes["/webhooks/x/{public_id}"] == {"GET", "POST"}
    assert routes["/webhooks/feishu/{public_id}"] == {"POST"}
    assert routes["/webhooks/feishu/{public_id}/card-actions"] == {"POST"}

    assert routes["/admin/oauth/x/callback"] == {"GET"}
    assert routes["/admin/oauth/meta/callback"] == {"GET"}
    assert routes["/admin/oauth/instagram/callback"] == {"GET"}
    assert not any(path.startswith("/callbacks/") for path in routes)

    current_pages = {
        "/admin/content/knowledge",
        "/admin/content/brand-voice",
        "/admin/integrations/accounts",
        "/admin/integrations/accounts/new/{provider}",
        "/admin/integrations/provisioning-jobs/{job_id}",
        "/admin/integrations/feishu/handoff",
        "/admin/system/health",
        "/admin/system/safety",
        "/admin/system/users",
    }
    legacy_pages = {
        "/admin/knowledge",
        "/admin/prompt",
        "/admin/accounts",
        "/admin/jobs/{job_id}",
        "/admin/feishu-handoff",
        "/admin/health",
    }
    for path in current_pages | legacy_pages:
        assert routes[path] == {"GET"}
    assert routes["/admin/users"] == {"GET", "POST"}


def test_control_api_v1_routes_remain_stable() -> None:
    routes = _route_methods()
    expected = {
        "/api/v1/platform-accounts": {"GET"},
        "/api/v1/platform-accounts/telegram": {"POST"},
        "/api/v1/platform-accounts/meta": {"POST"},
        "/api/v1/platform-accounts/whatsapp": {"POST"},
        "/api/v1/platform-accounts/email": {"POST"},
        "/api/v1/platform-accounts/feishu": {"POST"},
        "/api/v1/platform-accounts/x": {"POST"},
        "/api/v1/platform-accounts/jobs/{job_id}": {"GET"},
        "/api/v1/platform-accounts/jobs/{job_id}/retry": {"POST"},
        "/api/v1/platform-accounts/{account_id}/disable": {"POST"},
        "/api/v1/platform-accounts/{account_id}/enable": {"POST"},
    }

    for path, methods in expected.items():
        assert routes[path] == methods
