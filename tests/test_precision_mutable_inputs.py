"""Public parameter dictionaries can change after Action construction.

Exercise those aliasing boundaries using normal constructors and public dict
operations; never bypass frozen fields or manufacture private session state.
"""

import io
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import TaskLimitError
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot


def response(kind, params):
    return ActionDecision(Action(kind, params), "inspect target", 1.0, "visible target")


def executing(replies):
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(side_effect=replies)
    display = FakeDisplayBackend(screen_width=640, screen_height=480)
    session = TaskSession(llm=model, display=display, decision=None)
    session.loop.start(Goal("inspect target"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("inspect", "inspect target")])
    return session


@pytest.mark.parametrize("damage", ["missing", "invalid"])
async def test_changed_click_parameters_fail_without_sending_any_pointer_input(damage):
    params = {"x": 200, "y": 180}
    chosen = response(ActionType.LEFT_CLICK, params)
    if damage == "missing":
        del params["x"]
    else:
        params["x"] = None
    session = executing([chosen])
    session.executor.wait_for_still = AsyncMock()
    try:
        await session._execute_phase()
        assert session.display.clicks == [] and session.display.mouse_moves == []
        assert session.llm.decide_next_action.await_count == 1
        record = session.loop.context.action_history[-1]
        assert record.action is chosen.action and not record.success and record.error_message
        await session._verify_phase()
        assert session.loop.state == AgentState.RECOVERING
        assert session._pending_error.action is chosen.action
    finally:
        await session.aclose()


@pytest.mark.parametrize("damage", ["missing", "invalid"])
async def test_changed_historical_coordinates_are_not_used_to_synthesize_a_zoom(damage):
    params = {"x": 200, "y": 180}
    previous = Action(ActionType.LEFT_CLICK, params)
    session = executing([response(ActionType.LEFT_CLICK, {"x": 200, "y": 180})])
    session.executor.wait_for_still = AsyncMock()
    try:
        assert await session.executor.execute(previous)
        session.loop.record_action(previous, True)
        if damage == "missing":
            del params["y"]
        else:
            params["y"] = "not-a-coordinate"
        await session._execute_phase()
        assert session.llm.decide_next_action.await_count == 1
        assert session.display.clicks == [{"button": 1, "x": 200, "y": 180}] * 2
        assert session.loop.context.action_history[-1].success
        assert session.loop.state == AgentState.VERIFYING
    finally:
        await session.aclose()


@pytest.mark.parametrize("damage", ["region-size", "zoom-x", "zoom-y", "zoom-key"])
async def test_changed_precision_parameters_reach_recovery_without_dispatch(damage):
    region_params = {"x": 100, "y": 80, "width": 200, "height": 180}
    initial = response(ActionType.REGION_SELECT, region_params)
    if damage == "region-size":
        region_params["width"] = None
        replies = [initial]
    else:
        params = {"key": "escape"} if damage == "zoom-key" else {"x": 20, "y": 20}
        if damage == "zoom-y":
            params["x"] = -10
        refined = response(
            ActionType.KEY_PRESS if damage == "zoom-key" else ActionType.MOUSE_MOVE, params
        )
        params[{"zoom-x": "x", "zoom-y": "y", "zoom-key": "key"}[damage]] = (
            "" if damage == "zoom-key" else None
        )
        replies = [initial, refined]
    session = executing(replies)
    try:
        await session._execute_phase()
        assert session.loop.state == AgentState.RECOVERING
        assert session.loop.context.action_history == []
        assert (
            session.display.clicks
            == session.display.mouse_moves
            == session.display.key_presses
            == []
        )
        assert session.llm.decide_next_action.await_count == len(replies)
        assert session._pending_error.action is initial.action
        assert "操作位置" in session._pending_error.error_message
    finally:
        await session.aclose()


class LegacyScreenshot(Screenshot):
    """An older display adapter whose overlay method predates grid_spacing."""

    def crop_with_meta(self, *args, **kwargs):
        cropped, metadata = super().crop_with_meta(*args, **kwargs)
        return LegacyScreenshot(**asdict(cropped)), metadata

    def with_overlay(self):
        return super().with_overlay()


async def test_legacy_overlay_signature_keeps_real_crop_geometry_and_input_coordinates():
    image = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(image, format="PNG")
    raw = LegacyScreenshot(image.getvalue(), 640, 480)
    session = executing(
        [
            response(ActionType.REGION_SELECT, {"x": 100, "y": 80, "width": 200, "height": 180}),
            response(ActionType.LEFT_CLICK, {"x": 140, "y": 140}),
        ]
    )
    session.display.capture_raw = lambda: raw
    session.executor.wait_for_still = AsyncMock()
    try:
        await session._execute_phase()
        assert session.display.clicks == [{"button": 1, "x": 170, "y": 150}]
        call = session.llm.decide_next_action.call_args
        frame = call.kwargs["screenshot"]
        assert frame.to_pil().size == (400, 360)
        assert call.kwargs["zoom_origin"] == (100, 80) and call.kwargs["zoom_scale"] == 2
        assert session.loop.context.action_history[-1].success
    finally:
        await session.aclose()


async def test_an_invalid_low_confidence_point_gets_a_fresh_center_crop_before_any_input():
    params = {"x": 100, "y": 80}
    initial = ActionDecision(
        Action(ActionType.LEFT_CLICK, params), "inspect target", 0.2, "uncertain"
    )
    params["x"] = None
    session = executing([initial, response(ActionType.MOUSE_MOVE, {"x": 20, "y": 20})])
    try:
        await session._execute_phase()
        assert session.display.clicks == []
        assert session.display.mouse_moves == [(210, 130)]
        assert session.llm.decide_next_action.await_count == 2
        crop = session.llm.decide_next_action.call_args.kwargs
        assert crop["is_zoomed"] and crop["zoom_origin"] == (200, 120)
        assert (crop["screenshot"].width, crop["screenshot"].height) == (480, 480)
        assert session.loop.context.action_history[-1].action.action_type == ActionType.MOUSE_MOVE
        assert session.loop.context.action_history[-1].success
    finally:
        await session.aclose()


async def test_a_display_reported_task_limit_is_preserved_instead_of_retried_as_precision_failure():
    region = response(ActionType.REGION_SELECT, {"x": 100, "y": 80, "width": 200, "height": 180})
    session = executing([region])
    limit = TaskLimitError("duration_limit", "display capture deadline exceeded")

    def limited_capture():
        raise limit

    session.display.capture_raw = limited_capture
    try:
        with pytest.raises(TaskLimitError) as caught:
            await session._execute_phase()
        assert caught.value is limit
        assert session.llm.decide_next_action.await_count == 1
        assert session.display.clicks == session.display.mouse_moves == []
        assert session.loop.context.action_history == []
        assert session.loop.state == AgentState.EXECUTING
    finally:
        await session.aclose()
