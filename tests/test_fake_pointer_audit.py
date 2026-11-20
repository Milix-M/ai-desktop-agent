"""Primitive pointer events remain auditable independently of composite fake gestures."""

import pytest

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_pointer_audit_records_button_identity_order_and_can_start_a_new_log():
    class PrimitivePointer(FakeDisplayBackend):
        mouse_click = DisplayBackend.mouse_click
        mouse_double_click = DisplayBackend.mouse_double_click
        mouse_drag = DisplayBackend.mouse_drag

    display = PrimitivePointer(96, 64)
    executor = ActionExecutor(display)
    assert await executor.execute(Action(ActionType.RIGHT_CLICK, {"x": 12, "y": 18}))
    assert await executor.execute(Action(ActionType.MIDDLE_CLICK, {"x": 24, "y": 30}))
    assert display.button_events == [("down", 3), ("up", 3), ("down", 2), ("up", 2)]
    assert display.mouse_moves == [(12, 18), (24, 30)]
    assert display.total_actions == 4
    display.reset_logs()
    assert display.button_events == display.mouse_moves == [] and display.total_actions == 0
    assert await executor.execute(
        Action(ActionType.DRAG, {"start_x": 1, "start_y": 2, "end_x": 30, "end_y": 40})
    )
    assert display.button_events == [("down", 1), ("up", 1)]
    assert display.mouse_moves == [(1, 2), (30, 40)] and display.total_actions == 2


@pytest.mark.parametrize("kind", [ActionType.LEFT_CLICK, ActionType.DRAG])
async def test_a_failed_primitive_press_records_the_attempt_and_release_without_a_completed_gesture(
    kind,
):
    class LostPressAcknowledgement(FakeDisplayBackend):
        mouse_click = DisplayBackend.mouse_click
        mouse_drag = DisplayBackend.mouse_drag

        def mouse_down(self, button=1):
            super().mouse_down(button)
            raise OSError("pointer press acknowledgement lost")

    display = LostPressAcknowledgement(96, 64)
    executor = ActionExecutor(display)
    params = (
        {"x": 12, "y": 18}
        if kind == ActionType.LEFT_CLICK
        else {"start_x": 12, "start_y": 18, "end_x": 30, "end_y": 40}
    )
    assert not await executor.execute(Action(kind, params))
    assert "acknowledgement lost" in executor.last_error
    assert display.button_events == [("down", 1), ("up", 1)]
    assert display.mouse_moves == [(12, 18)] and display.total_actions == 2
    assert display.clicks == display.drags == []
