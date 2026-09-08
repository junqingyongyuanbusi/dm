import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
ACCOUNT_MANAGEMENT = ROOT / "src/social_reply/application/account_management"


def _source(name: str) -> str:
    return (ACCOUNT_MANAGEMENT / name).read_text()


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name), filename=name)


def _function_names(name: str) -> list[str]:
    return [
        node.name
        for node in ast.walk(_tree(name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _calls_with_keyword(name: str, function_name: str, keyword: str) -> int:
    count = 0
    for node in ast.walk(_tree(name)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == function_name:
            count += any(argument.arg == keyword for argument in node.keywords)
    return count


def test_channel_management_has_scoped_role_and_reauthorization_surfaces() -> None:
    source = _source("channel_management.py")
    functions = _function_names("channel_management.py")

    assert functions.count("submit_channel_provisioning") == 1
    assert "set_channel_account_support_visibility" in functions
    assert "set_channel_reauthorization_grant" in functions
    assert "await require_reauthorization(" in source
    assert "additional_user_ids" in source
    assert "actor.is_admin" in source


def test_saas_console_wires_targeted_channel_and_human_routes() -> None:
    source = _source("saas_console.py")

    for route in (
        "/support-visibility",
        "/reauthorization-grants/{user_id}",
        "/start-reception",
        "/work-items/{work_item_id}/transfer",
    ):
        assert route in source
    assert 'operation = "REAUTHORIZE_ACCOUNT"' in source
    assert "target_account_id=target_account_id" in source
    assert "expected_config_version=expected_config_version" in source
    assert "_account_scope_condition(principal, tenant_id)" in source
    assert _calls_with_keyword("saas_console.py", "claim_human_work_item", "principal") == 1
    assert _calls_with_keyword("saas_console.py", "resolve_human_work_item", "principal") == 1
    assert _calls_with_keyword("saas_console.py", "send_human_reply", "principal") == 1
    assert _calls_with_keyword("saas_console.py", "approve_draft_review", "principal") == 1


def test_legacy_console_preserves_system_gate_and_business_scope() -> None:
    admin_console = _source("admin_console.py")
    admin = _source("admin.py")
    users = _source("users.py")

    assert "account_read_condition" in admin_console
    assert "show_users=principal.is_admin" in admin_console
    assert "allow_override=principal.is_superadmin" not in admin_console
    assert "principal.is_workspace_admin and not principal.is_superadmin" in users
    assert "principal.require_superadmin()" in admin
    assert "principal.require_tenant_admin()" in admin_console
    assert "principal.require_superadmin()" in admin_console


def test_reauthorization_grant_does_not_revoke_human_work() -> None:
    source = _source("channel_management.py")
    grant_start = source.index("async def set_channel_reauthorization_grant")
    grant_end = source.index("async def set_channel_account_kill_switch", grant_start)
    grant_body = source[grant_start:grant_end]

    assert "_release_ineligible_account_work" not in grant_body
    assert "account.config_version += 1" in grant_body
