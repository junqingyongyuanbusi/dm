import pytest

from social_reply.domain.automation.state_machine import (
    AutomationStateEnum,
    can_transition,
)


def test_bot_active_to_human_active_on_agent_reply():
    assert can_transition(AutomationStateEnum.BOT_ACTIVE, AutomationStateEnum.HUMAN_ACTIVE)


def test_draft_only_to_human_active_on_agent_reply():
    assert can_transition(AutomationStateEnum.BOT_DRAFT_ONLY, AutomationStateEnum.HUMAN_ACTIVE)


def test_closed_is_terminal_except_reopen():
    assert can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.HUMAN_ACTIVE)
    assert can_transition(AutomationStateEnum.CLOSED, AutomationStateEnum.BOT_ACTIVE)


@pytest.mark.parametrize("state", list(AutomationStateEnum))
def test_no_self_transition(state):
    assert not can_transition(state, state)
