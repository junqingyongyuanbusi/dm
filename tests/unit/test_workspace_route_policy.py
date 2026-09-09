import uuid

import pytest
from fastapi import HTTPException

from social_reply.application.account_management.auth import Principal
from social_reply.application.account_management.workspace_route_policy import (
    require_workspace_route,
)


@pytest.mark.parametrize("takeover_enabled", [False, True])
def test_transfer_uses_takeover_capability_instead_of_reply(takeover_enabled):
    principal = Principal(
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="operator",
        actor="user:operator",
        allowed_tenants=frozenset({"default"}),
        tenant_id="default",
        role="OPERATOR",
        operator_takeover_enabled=takeover_enabled,
        operator_reply_enabled=not takeover_enabled,
    )
    path = f"/app/t/default/work-items/{uuid.uuid4()}/transfer"
    if takeover_enabled:
        require_workspace_route(principal, path, "POST", "default")
    else:
        with pytest.raises(HTTPException) as failure:
            require_workspace_route(principal, path, "POST", "default")
        assert failure.value.status_code == 403
