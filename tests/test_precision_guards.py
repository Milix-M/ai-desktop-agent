"""Precision uses recent matching clicks and still verifies zoomed completion."""

from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, VerificationResult
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def decision(kind, **params):
    return ActionDecision(Action(kind, params or None), "inspect the target", 1.0, "visible target")


@pytest.mark.parametrize(
    "history,zoomed",
    [
        ([Action(ActionType.LEFT_CLICK, {"x": 192, "y": 172})], True),
        ([Action(ActionType.LEFT_CLICK, {"x": 191, "y": 172})], False),
        ([Action(ActionType.RIGHT_CLICK, {"x": 200, "y": 180})], False),
        (
            [
                Action(ActionType.LEFT_CLICK, {"x": 200, "y": 180}),
                Action(ActionType.KEY_PRESS, {"key": "escape"}),
                Action(ActionType.TYPE, {"text": "quarterly report"}),
                Action(ActionType.SCROLL, {"direction": "down", "amount": 1}),
            ],
            False,
        ),
    ],
    ids=["inclusive-radius", "outside-radius", "different-button", "outside-history-window"],
)
async def test_only_a_recent_matching_click_triggers_a_second_image_decision(history, zoomed):
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(
        side_effect=[
            decision(ActionType.LEFT_CLICK, x=200, y=180),
            decision(ActionType.LEFT_CLICK, x=20, y=20),
        ]
    )
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    session = TaskSession(llm=model, display=display, decision=None)
    session.executor.wait_for_still = AsyncMock()
    session.loop.start(Goal("inspect the target"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("inspect", "inspect the target")])
    for action in history:
        session.loop.record_action(action, True)
    try:
        await session._execute_phase()
        assert model.decide_next_action.await_count == (2 if zoomed else 1)
        assert [
            call.kwargs.get("is_zoomed", False) for call in model.decide_next_action.call_args_list
        ] == ([False, True] if zoomed else [False])
        assert display.clicks == [
            {"button": 1, "x": 90 if zoomed else 200, "y": 70 if zoomed else 180}
        ]
        assert session.loop.context.action_history[-1].success
        assert session.loop.state == AgentState.VERIFYING
    finally:
        await session.aclose()


@pytest.mark.parametrize("verified", [False, True])
async def test_completion_chosen_from_a_zoomed_image_requires_fresh_visual_verification(verified):
    model = MockLLMProvider()
    completed = decision(ActionType.SUBTASK_COMPLETE)
    model.decide_next_action = AsyncMock(
        side_effect=[
            decision(ActionType.REGION_SELECT, x=100, y=80, width=200, height=180),
            completed,
        ]
    )
    model.verify_result = AsyncMock(
        return_value=VerificationResult(
            success=verified, reasoning="checked the visible result", evidence="report state"
        )
    )
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    session = TaskSession(llm=model, display=display, decision=None)
    session.loop.start(Goal("inspect the saved report"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("inspect", "inspect", expected_outcome="report is saved")])
    try:
        await session._execute_phase()
        assert session.loop.state == AgentState.VERIFYING
        model.verify_result.assert_not_awaited()
        assert display.clicks == []
        assert session.loop.context.action_history[-1].action is completed.action
        await session._verify_phase()
        model.verify_result.assert_awaited_once()
        frame = model.verify_result.call_args.kwargs["screenshot"]
        assert (frame.width, frame.height) == (640, 480) and frame.image_bytes
        assert session.loop.state == (AgentState.COMPLETED if verified else AgentState.RECOVERING)
        assert session.loop.context.current_subtask_index == int(verified)
        assert display.clicks == []
    finally:
        await session.aclose()
