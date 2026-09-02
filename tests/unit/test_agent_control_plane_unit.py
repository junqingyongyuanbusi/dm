import pytest

from social_reply.application.account_management.agent_control_plane import (
    AgentControlPlaneValidationError,
    normalize_agent_description,
    normalize_agent_name,
    normalize_agent_slug,
)


def test_agent_identity_normalization_preserves_stable_scope() -> None:
    assert normalize_agent_slug("  indonesia_support  ") == "indonesia_support"
    assert normalize_agent_name("  Indonesia   Support  ") == "Indonesia Support"
    assert normalize_agent_description("  Handles support.  ") == "Handles support."
    assert normalize_agent_description("   ") is None


@pytest.mark.parametrize(
    "slug",
    ("", "support team", "support.example", "客服", "a" * 65),
)
def test_agent_scope_rejects_ambiguous_or_unaddressable_values(slug: str) -> None:
    with pytest.raises(AgentControlPlaneValidationError, match="invalid_agent_slug"):
        normalize_agent_slug(slug)


def test_agent_identity_enforces_bounded_copy() -> None:
    with pytest.raises(AgentControlPlaneValidationError, match="invalid_agent_name"):
        normalize_agent_name("a" * 129)
    with pytest.raises(AgentControlPlaneValidationError, match="invalid_agent_description"):
        normalize_agent_description("a" * 1001)
