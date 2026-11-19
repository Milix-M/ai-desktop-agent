"""Exercise the default display operations through the public action executor."""

import pytest

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class PrimitiveDisplay(FakeDisplayBackend):
    """A transport implementing primitive events and inheriting composite operations."""

    capture_raw = DisplayBackend.capture_raw
    mouse_click = DisplayBackend.mouse_click
    mouse_double_click = DisplayBackend.mouse_double_click
    mouse_drag = DisplayBackend.mouse_drag
    key_combo = DisplayBackend.key_combo

    def __init__(self, fail=None, release_failure=False):
        super().__init__()
        self.buttons = set()
        self.keys = set()
        self.fail = fail
        self.release_failure = release_failure
        self.releases = []

    def mouse_down(self, button=1):
        super().mouse_down(button)
        self.buttons.add(button)
        if self.fail == "press":
            raise OSError("lost press acknowledgement")

    def mouse_up(self, button=1):
        super().mouse_up(button)
        self.buttons.discard(button)
        self.releases.append(button)
        if self.release_failure:
            raise OSError("release failed")

    def mouse_move(self, x, y):
        if self.buttons and self.fail == "drag":
            raise OSError("drag interrupted")
        super().mouse_move(x, y)

    def key_down(self, key):
        self.keys.add(key)
        if key == self.fail:
            raise OSError("lost press acknowledgement")

    def key_up(self, key):
        self.keys.discard(key)
        self.releases.append(key)
        if self.release_failure:
            raise OSError("release failed")


@pytest.mark.parametrize(
    "kind,params,moves,releases",
    [
        (ActionType.LEFT_CLICK, {"x": 12, "y": 34}, [(12, 34)], [1]),
        (ActionType.DOUBLE_CLICK, {"x": 12, "y": 34}, [(12, 34)], [1, 1]),
        (
            ActionType.DRAG,
            {"start_x": 12, "start_y": 34, "end_x": 56, "end_y": 78},
            [(12, 34), (56, 78)],
            [1],
        ),
        (ActionType.KEY_COMBO, {"keys": ["ctrl", "shift", "s"]}, [], ["s", "shift", "ctrl"]),
    ],
)
async def test_default_composite_actions_deliver_the_requested_input(kind, params, moves, releases):
    display = PrimitiveDisplay()
    assert await ActionExecutor(display).execute(Action(kind, params))
    assert display.mouse_moves == moves
    assert display.releases == releases
    if kind != ActionType.KEY_COMBO:
        assert display.button_events == [
            (event, button) for button in releases for event in ("down", "up")
        ]
    assert not display.buttons and not display.keys


@pytest.mark.parametrize(
    "kind,params,failure",
    [
        (ActionType.LEFT_CLICK, {"x": 12, "y": 34}, "press"),
        (ActionType.DRAG, {"start_x": 12, "start_y": 34, "end_x": 56, "end_y": 78}, "drag"),
        (ActionType.KEY_COMBO, {"keys": ["ctrl", "s"]}, "s"),
    ],
)
@pytest.mark.parametrize("release_failure", [False, True])
async def test_transport_failure_does_not_leave_inputs_pressed(
    kind, params, failure, release_failure
):
    display = PrimitiveDisplay(failure, release_failure)
    executor = ActionExecutor(display)
    assert not await executor.execute(Action(kind, params))
    assert not display.buttons and not display.keys
    assert "release failed" not in executor.last_error
    if kind == ActionType.KEY_COMBO:
        assert display.releases == ["s", "ctrl"]
    else:
        assert display.button_events == [("down", 1), ("up", 1)]


async def test_a_failed_key_release_does_not_skip_the_remaining_modifiers():
    display = PrimitiveDisplay(release_failure=True)
    executor = ActionExecutor(display)
    assert not await executor.execute(Action(ActionType.KEY_COMBO, {"keys": ["ctrl", "s"]}))
    assert display.releases == ["s", "ctrl"]
    assert not display.keys
    assert executor.last_error == "release failed"


def test_default_raw_capture_preserves_the_transport_image_and_size():
    display = PrimitiveDisplay()
    image = display.capture_raw()
    assert display.screenshots_taken == 1
    with image.to_pil() as pixels:
        assert pixels.size == (image.width, image.height) == (1024, 768)


def test_default_click_without_coordinates_preserves_the_cursor_position():
    display = PrimitiveDisplay()
    display.mouse_move(12, 34)
    display.mouse_click(button=3)
    assert display.mouse_moves == [(12, 34)]
    assert display.releases == [3] and not display.buttons


@pytest.mark.parametrize("coordinates", [(0, None), (None, 0)])
def test_a_partial_point_is_rejected_before_any_pointer_input(coordinates):
    display = PrimitiveDisplay()
    with pytest.raises(ValueError, match="xとy"):
        display.mouse_click(*coordinates)
    assert display.mouse_moves == display.releases == []
    assert not display.buttons
