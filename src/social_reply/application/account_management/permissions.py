"""Fail-closed workspace capabilities, independent of HTTP and account scope."""

from types import MappingProxyType

WORKSPACE_ROLES = frozenset({"WORKSPACE_ADMIN", "MANAGER", "OPERATOR", "AGENT", "VIEWER"})
SUPPORTED_ROLES = WORKSPACE_ROLES | {"USER"}
PAGE_CAPABILITIES = frozenset(
    f"{page}.read"
    for page in (
        "home",
        "inbox",
        "contacts",
        "agents",
        "flows",
        "knowledge",
        "playground",
        "channels",
        "reports",
        "team",
        "audit",
        "settings",
    )
)
ALL_CAPABILITIES = PAGE_CAPABILITIES | {"reply", "takeover", "connect", "configure"}
_AGENT_CAPABILITIES = frozenset(
    {"inbox.read", "contacts.read", "knowledge.read", "reply", "takeover"}
)
ROLE_CAPABILITIES = MappingProxyType(
    {
        "WORKSPACE_ADMIN": ALL_CAPABILITIES,
        "MANAGER": (PAGE_CAPABILITIES - {"team.read", "audit.read", "settings.read"})
        | {"reply", "takeover", "connect"},
        "OPERATOR": frozenset({"inbox.read", "channels.read", "connect"}),
        "AGENT": _AGENT_CAPABILITIES,
        "USER": _AGENT_CAPABILITIES,
        "VIEWER": frozenset({"inbox.read", "reports.read", "audit.read"}),
    }
)


def role_has_capability(
    role: str,
    capability: str,
    *,
    operator_reply_enabled: bool = False,
    operator_takeover_enabled: bool = False,
) -> bool:
    if capability in ROLE_CAPABILITIES.get(role, frozenset()):
        return True
    return role == "OPERATOR" and (
        (capability == "reply" and operator_reply_enabled is True)
        or (capability == "takeover" and operator_takeover_enabled is True)
    )


def user_has_capability(user, capability: str) -> bool:
    return (
        user.status == "active"
        and not user.must_change_password
        and role_has_capability(
            user.role,
            capability,
            operator_reply_enabled=user.operator_reply_enabled,
            operator_takeover_enabled=user.operator_takeover_enabled,
        )
    )
