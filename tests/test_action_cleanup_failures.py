"""Uncertain key release preserves failure/cancellation and absent targets stay absent."""

import asyncio
import io
import threading
from unittest.mock import Mock

import pytest
from PIL import Image

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.mark.parametrize("outcome", ["down-response-lost", "cancelled-after-down"])
async def test_failed_key_release_keeps_the_original_failure_or_cancellation(outcome, caplog):
    display = FakeDisplayBackend()
    accepted = threading.Event()

    def down(key):
        accepted.set()
        if outcome == "down-response-lost":
            raise OSError("key-down acknowledgement lost")

    display.key_down = Mock(side_effect=down)
    display.key_up = Mock(side_effect=OSError("release connection lost"))
    executor = ActionExecutor(display)
    operation = asyncio.create_task(
        executor.execute(Action(ActionType.KEY_HOLD, {"key": "shift", "duration_ms": 10000}))
    )
    assert await asyncio.to_thread(accepted.wait, 2)
    if outcome == "cancelled-after-down":
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
    else:
        assert await asyncio.wait_for(operation, 2) is False
        assert executor.last_error == "key-down acknowledgement lost"
    display.key_down.assert_called_once_with("shift")
    display.key_up.assert_called_once_with("shift")
    assert "長押しキーの解放に失敗" in caplog.text


def test_a_legacy_capture_backend_does_not_invent_coordinates_for_an_absent_template():
    class LegacyDisplay(FakeDisplayBackend):
        capture_raw = None

    display = LegacyDisplay(screen_width=32, screen_height=32)
    image = Image.new("RGB", (4, 4), "black")
    image.putpixel((0, 0), (255, 255, 255))
    image.putpixel((1, 2), (80, 120, 200))
    png = io.BytesIO()
    image.save(png, format="PNG")
    executor = ActionExecutor(display)
    assert executor.locate(png.getvalue(), threshold=0.99) is None
    assert display.screenshots_taken == 1 and display.mouse_moves == [] and display.clicks == []
