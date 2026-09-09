"""Capability checks shared by existing workspace HTTP handlers.

These checks supplement, rather than replace, transaction-level authorization.
"""

from social_reply.application.account_management.auth import Principal

_SECTION_CAPABILITIES = {
    "inbox": "inbox.read",
    "conversations": "inbox.read",
    "work-items": "inbox.read",
    "contacts": "contacts.read",
    "agents": "agents.read",
    "flows": "flows.read",
    "knowledge": "knowledge.read",
    "knowledge-query": "knowledge.read",
    "playground": "playground.read",
    "channels": "channels.read",
    "reports": "reports.read",
    "audit": "audit.read",
    "settings": "settings.read",
    "health": "settings.read",
    "journeys": "settings.read",
    "activity": "inbox.read",
    "profile": "inbox.read",
    "decisions": "configure",
    "delivery": "configure",
}


def require_workspace_route(principal: Principal, path: str, method: str, tenant_id: str) -> None:
    root = f"/app/t/{tenant_id}"
    relative_path = path.removeprefix(root).strip("/")
    if not relative_path:
        # The landing handler redirects staff without overview access into Inbox.
        principal.require_capability("inbox.read")
        return
    segments = relative_path.split("/")
    section = segments[0]
    capability = _SECTION_CAPABILITIES.get(section, "configure")
    if section == "agents" and len(segments) == 3 and segments[2] == "test":
        capability = "playground.read"
    if method not in {"GET", "HEAD"}:
        if section == "knowledge":
            capability = "configure"
        elif section == "agents" and segments[-1] != "test":
            capability = "configure"
        elif section in {"conversations", "work-items"}:
            action = segments[-1]
            capability = (
                "takeover"
                if action in {"start-reception", "claim", "resolve", "transfer"}
                else "reply"
            )
    principal.require_capability(capability)
