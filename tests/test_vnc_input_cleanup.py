"""A failed transport must not leave keys or buttons held in the guest."""

from unittest.mock import Mock

import pytest

from ai_desktop_agent.vm.vnc_client import VNCClient


@pytest.fixture
def display(monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.vm.vnc_client.time.sleep", lambda _: None)
    client = VNCClient()
    client._connected = True
    client._client = Mock()
    return client


@pytest.mark.parametrize("failed_key", ["ctrl", "shift", "s"])
def test_failed_combo_releases_every_key_that_may_have_been_pressed(display, failed_key):
    pressed = []
    released = []

    def down(key):
        pressed.append(key)
        if key == failed_key:
            raise RuntimeError("key acknowledgement lost")

    display._client.keyDown.side_effect = down
    display._client.keyUp.side_effect = released.append
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        display.key_combo(["ctrl", "shift", "s"])
    assert released == list(reversed(pressed))


def test_failed_release_does_not_skip_other_modifier_keys(display):
    released = []

    def up(key):
        released.append(key)
        if key == "s":
            raise RuntimeError("release failed")

    display._client.keyUp.side_effect = up
    with pytest.raises(RuntimeError, match="release failed"):
        display.key_combo(["ctrl", "shift", "s"])
    assert released == ["s", "shift", "ctrl"]


def test_combo_stops_before_pressing_the_next_key_and_releases_previous_keys(display):
    def down(_):
        display.request_stop()

    display._client.keyDown.side_effect = down
    with pytest.raises(RuntimeError, match="停止"):
        display.key_combo(["ctrl", "s"])
    display._client.keyDown.assert_called_once_with("ctrl")
    display._client.keyUp.assert_called_once_with("ctrl")


def test_key_press_releases_on_an_uncertain_press(display):
    display._client.keyDown.side_effect = RuntimeError("acknowledgement lost")
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        display.key_press("shift")
    display._client.keyUp.assert_called_once_with("shift")


@pytest.mark.parametrize("operation", ["click", "drag", "scroll"])
def test_mouse_press_failure_still_attempts_release(display, operation):
    display._client.mouseDown.side_effect = RuntimeError("acknowledgement lost")
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        if operation == "click":
            display.mouse_click(20, 30)
        elif operation == "drag":
            display.mouse_drag(20, 30, 80, 90)
        else:
            display.mouse_scroll("down", 2)
    display._client.mouseUp.assert_called_once_with(5 if operation == "scroll" else 1)


def test_release_failure_keeps_the_original_press_error(display):
    display._client.keyDown.side_effect = RuntimeError("press failed")
    display._client.keyUp.side_effect = ValueError("release failed")
    with pytest.raises(RuntimeError, match="press failed"):
        display.key_combo(["ctrl", "s"])


@pytest.mark.parametrize("method", ["mouse_click", "mouse_double_click"])
@pytest.mark.parametrize("coordinates", [(0, None), (None, 0)])
def test_partial_click_coordinates_cannot_click_an_unintended_position(
    display, method, coordinates
):
    with pytest.raises(ValueError, match="xとy"):
        getattr(display, method)(*coordinates)
    display._client.mouseDown.assert_not_called()
    display._client.mouseMove.assert_not_called()
