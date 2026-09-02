import inspect

from social_reply.application.account_management import saas_console


def test_system_audit_contract_only_allows_security_categories() -> None:
    assert saas_console.SYSTEM_AUDIT_CATEGORIES == frozenset(
        {
            "authentication",
            "session_management",
            "user_management",
            "role_change",
            "global_safety",
            "security_configuration",
        }
    )


def test_system_audit_detail_redacts_secrets_recursively() -> None:
    detail = {
        "username": "visible-user",
        "password": "must-not-render",
        "authorization": "Bearer must-not-render-either",
        "cookie": "must-not-render-cookie",
        "api_key": "must-not-render-api-key",
        "state": "must-not-render-oauth-state",
        "outcome": "success",
    }

    redacted = saas_console._redact_system_audit_detail(detail)

    assert redacted == {
        "username": "visible-user",
        "password": "[REDACTED]",
        "authorization": "[REDACTED]",
        "cookie": "[REDACTED]",
        "api_key": "[REDACTED]",
        "state": "[REDACTED]",
        "outcome": "success",
    }


def test_system_overview_source_cannot_reference_tenant_business_models() -> None:
    source = inspect.getsource(saas_console._load_system_overview)

    for forbidden_model in (
        "PlatformAccount",
        "Conversation",
        "OutboxMessage",
        "KnowledgeDocument",
        "ReplyBusinessPromptVersion",
    ):
        assert forbidden_model not in source
