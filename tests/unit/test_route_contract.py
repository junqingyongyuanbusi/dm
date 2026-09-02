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
        "/admin/content/reply-prompt",
        "/admin/integrations/accounts",
        "/admin/integrations/accounts/new/{provider}",
        "/admin/integrations/provisioning-jobs/{job_id}",
        "/admin/integrations/feishu/handoff",
        "/admin/system/health",
        "/admin/system/safety",
    }
    legacy_pages = {
        "/admin/knowledge",
        "/admin/content/brand-voice",
        "/admin/prompt",
        "/admin/accounts",
        "/admin/jobs/{job_id}",
        "/admin/feishu-handoff",
        "/admin/health",
    }
    for path in current_pages | legacy_pages:
        assert routes[path] == {"GET"}
    assert routes["/admin/users"] == {"GET", "POST"}
    assert routes["/admin/system/users"] == {"GET", "POST"}


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


def test_saas_workspace_and_system_admin_routes_are_mounted() -> None:
    routes = _route_methods()
    auth_pages = {
        "/auth/login": {"GET", "POST"},
        "/auth/logout": {"GET", "POST"},
        "/auth/change-password": {"GET", "POST"},
    }
    tenant_pages = {
        "/app": {"GET"},
        "/app/t/{tenant_id}": {"GET"},
        "/app/t/{tenant_id}/agents": {"GET"},
        "/app/t/{tenant_id}/agents/{agent_id}": {"GET"},
        "/app/t/{tenant_id}/agents/{agent_id}/instructions": {"GET"},
        "/app/t/{tenant_id}/agents/{agent_id}/instructions/save": {"POST"},
        "/app/t/{tenant_id}/agents/{agent_id}/instructions/trial": {"POST"},
        "/app/t/{tenant_id}/agents/{agent_id}/instructions/versions/{version_id}/rollback": {
            "POST"
        },
        "/app/t/{tenant_id}/agents/{agent_id}/{section}": {"GET"},
        "/app/t/{tenant_id}/inbox": {"GET"},
        "/app/t/{tenant_id}/decisions/{decision_id}/approve": {"POST"},
        "/app/t/{tenant_id}/decisions/{decision_id}/discard": {"POST"},
        "/app/t/{tenant_id}/delivery/{outbox_id}/retry": {"POST"},
        "/app/t/{tenant_id}/delivery/{outbox_id}/resolve": {"POST"},
        "/app/t/{tenant_id}/knowledge": {"GET"},
        "/app/t/{tenant_id}/knowledge/documents": {"POST"},
        "/app/t/{tenant_id}/knowledge/import": {"POST"},
        "/app/t/{tenant_id}/knowledge/bulk-confirm-english": {"POST"},
        "/app/t/{tenant_id}/knowledge/bulk-publish": {"POST"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}": {"GET"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}/confirm-english": {"POST"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}/official-contact": {"POST"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}/publish": {"POST"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}/unpublish": {"POST"},
        "/app/t/{tenant_id}/knowledge/documents/{document_id}/delete": {"POST"},
        "/app/t/{tenant_id}/audit": {"GET"},
        "/app/t/{tenant_id}/audit/{audit_id}": {"GET"},
        "/app/t/{tenant_id}/journeys": {"GET"},
        "/app/t/{tenant_id}/journeys/{journey_id}": {"GET"},
        "/app/t/{tenant_id}/health": {"GET"},
        "/app/t/{tenant_id}/settings": {"GET"},
        "/app/t/{tenant_id}/channels": {"GET"},
        "/app/t/{tenant_id}/channels/accounts/{platform}": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}": {"GET"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/rename": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/status": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/automation": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/kill-switch": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/owner": {"POST"},
        "/app/t/{tenant_id}/channels/accounts/{account_id}/xchat/repair": {"POST"},
        "/app/t/{tenant_id}/channels/jobs/{job_id}": {"GET"},
        "/app/t/{tenant_id}/channels/jobs/{job_id}/retry": {"POST"},
        "/app/t/{tenant_id}/channels/feishu/handoff": {"GET"},
        "/app/t/{tenant_id}/channels/feishu/handoff/config": {"POST"},
        "/app/t/{tenant_id}/channels/feishu/handoff/operators": {"POST"},
        "/app/t/{tenant_id}/channels/feishu/handoff/operators/{operator_id}/status": {"POST"},
        "/app/t/{tenant_id}/channels/feishu/handoff/test": {"POST"},
        "/app/t/{tenant_id}/channels/oauth/x/start": {"POST"},
        "/app/t/{tenant_id}/channels/oauth/meta/start": {"POST"},
        "/app/t/{tenant_id}/channels/oauth/meta/select": {"POST"},
        "/app/t/{tenant_id}/channels/oauth/instagram/start": {"POST"},
        "/app/t/{tenant_id}/profile": {"GET"},
        "/app/t/{tenant_id}/profile/accounts/{platform}": {"POST"},
    }
    system_pages = {
        "/admin/system/overview": {"GET"},
        "/admin/system/audit": {"GET"},
        "/admin/system/users": {"GET", "POST"},
        "/admin/system/users/{user_id}/status": {"POST"},
        "/admin/system/users/{user_id}/password-reset": {"POST"},
        "/admin/system/users/{user_id}/sessions/revoke": {"POST"},
        "/help": {"GET"},
    }

    for path, methods in (auth_pages | tenant_pages | system_pages).items():
        assert routes[path] == methods

    assert "/admin/system/users/{user_id}/role" not in routes
