"""A public verification observer can request another decision after a successful action."""

import asyncio
from unittest.mock import AsyncMock

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_observer_requested_retry_keeps_successful_history_and_makes_a_fresh_decision(
    tmp_path, session_retry_sleep
):
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(
                Action(ActionType.KEY_PRESS, {"key": "escape"}), "inspect", 1, "inspect"
            ),
            ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "finished", 1, "finished"),
        ]
    )
    model.recover_from_error = AsyncMock(wraps=model.recover_from_error)
    display = FakeDisplayBackend(screen_width=96, screen_height=64)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    retried = False
    completions = []

    def verify(state, context):
        nonlocal retried
        if state == AgentState.VERIFYING and not retried:
            # An independent observer requests a new decision using the public
            # state-machine event, without inventing a failed action/error context.
            assert len(context.action_history) == 1 and context.action_history[-1].success
            retried = True
            session.loop.verify_failed()

    session.on_state_change(verify)
    session.on_complete(completions.append)
    try:
        assert await asyncio.wait_for(session.run("保存済み文書を確認する"), 5)
        saved = TaskStore(tmp_path).load(session.id)
        assert retried and saved.state == "completed" and saved.success is True
        assert saved.stop_reason is None and not saved.error_message
        assert [action.action_type for action in saved.actions] == ["key_press", "subtask_complete"]
        assert all(action.success and not action.error_message for action in saved.actions)
        assert display.key_presses == ["escape"] and not display.is_connected
        assert model.decide_next_action.await_count == 2 and completions == [True]
        model.recover_from_error.assert_not_awaited()
    finally:
        await session.aclose()
