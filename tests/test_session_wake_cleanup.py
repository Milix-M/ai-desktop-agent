"""Interrupted screen wake must not leave the guest holding Escape or Menu."""

import asyncio
import io
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest
from PIL import Image

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class LegacyDisplay(FakeDisplayBackend):
    wake_android = None

    def __init__(self, fail_key=None, block_key=None):
        super().__init__()
        self.pressed = set()
        self.fail_key = fail_key
        self.block_key = block_key
        self.entered = threading.Event()
        self.release = threading.Event()

    def key_down(self, key):
        super().key_down(key)
        self.pressed.add(key)
        if key == self.fail_key:
            raise ConnectionError("key acknowledgement lost")
        if key == self.block_key:
            self.entered.set()
            assert self.release.wait(5), "wake worker was not released by the test"

    def key_up(self, key):
        super().key_up(key)
        self.pressed.discard(key)


@pytest.mark.parametrize("key", ["escape", "menu"])
async def test_uncertain_wake_press_still_releases_the_affected_key(monkeypatch, key):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay(fail_key=key)
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    await session._wake_android_display()
    assert display.pressed == set(), display.key_presses
    assert display.key_presses[-2:] == [f"down:{key}", f"up:{key}"]
    assert display.clicks == []


@pytest.mark.parametrize("key", ["escape", "menu"])
async def test_repeated_cancellation_drains_the_wake_worker_and_releases_the_key(monkeypatch, key):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay(block_key=key)
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    task = asyncio.create_task(session._wake_android_display())
    try:
        assert await asyncio.to_thread(display.entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancelled wake released display ownership too early"
        display.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert display.pressed == set(), display.key_presses
        assert display.key_presses[-2:] == [f"down:{key}", f"up:{key}"]
        assert display.clicks == []
    finally:
        display.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("native_fails", [False, True])
async def test_native_wake_is_preferred_and_legacy_keys_are_only_used_after_failure(
    monkeypatch, native_fails
):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay()
    display.wake_android = Mock(
        side_effect=ConnectionError("native wake failed") if native_fails else None
    )
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    await session._wake_android_display()
    display.wake_android.assert_called_once_with()
    assert display.pressed == set()
    assert display.key_presses == (
        ["down:escape", "up:escape", "down:menu", "up:menu"] if native_fails else []
    )
    assert display.clicks == ([{"button": 1, "x": 512, "y": 384}] if native_fails else [])


@pytest.mark.parametrize(
    "dimensions, center",
    [
        ({"_screen_width": 1280, "_screen_height": 800}, (640, 400)),
        ({"_screen_width": 0, "_screen_height": 0, "_width": 640, "_height": 480}, (320, 240)),
        ({"_screen_width": None, "_screen_height": None}, (512, 384)),
    ],
)
async def test_legacy_wake_releases_both_keys_before_tapping_the_display_center(
    monkeypatch, dimensions, center
):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay()
    for name, value in dimensions.items():
        setattr(display, name, value)
    original_click = display.mouse_click

    def click(x, y):
        assert display.pressed == set()
        original_click(x, y)

    display.mouse_click = click
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    await session._wake_android_display()
    assert display.key_presses == ["down:escape", "up:escape", "down:menu", "up:menu"]
    assert display.clicks == [{"button": 1, "x": center[0], "y": center[1]}]


async def test_failed_wake_tap_does_not_press_keys_again(monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay()
    display.mouse_click = Mock(side_effect=ConnectionError("tap acknowledgement lost"))
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    await session._wake_android_display()
    assert display.pressed == set()
    assert display.key_presses == ["down:escape", "up:escape", "down:menu", "up:menu"]
    display.mouse_click.assert_called_once_with(512, 384)


async def test_failed_release_does_not_replace_the_original_uncertain_press(monkeypatch, caplog):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    caplog.set_level("DEBUG", logger="ai_desktop_agent.server.session")
    display = LegacyDisplay(fail_key="escape")
    display.key_up = Mock(side_effect=ValueError("release acknowledgement lost"))
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    await session._wake_android_display()
    display.key_up.assert_called_once_with("escape")
    wake_error = next(r for r in caplog.records if r.name == "ai_desktop_agent.server.session")
    assert wake_error.exc_text.endswith("ConnectionError: key acknowledgement lost\n")
    assert display.clicks == []


async def test_cancellation_during_release_keeps_ownership_until_the_key_is_released(monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.server.session.ANDROID_WAKE_HOLD_SECONDS", 0)
    display = LegacyDisplay()
    entered, release = threading.Event(), threading.Event()
    original_up = display.key_up

    def up(key):
        entered.set()
        assert release.wait(5)
        original_up(key)

    display.key_up = up
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    task = asyncio.create_task(session._wake_android_display())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and display.pressed == {"escape"}
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert display.pressed == set()
        assert display.key_presses == ["down:escape", "up:escape"]
        assert display.clicks == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("vm_id", ["android", "and-" + "a" * 24])
async def test_black_android_frame_uses_native_wake_and_recaptures_once(vm_id):
    display = FakeDisplayBackend()
    original = display.capture_screen
    black = io.BytesIO()
    Image.new("RGB", (1024, 768), color="black").save(black, format="PNG")

    def capture():
        shot = original()
        return (
            replace(shot, image_bytes=black.getvalue()) if display.screenshots_taken == 1 else shot
        )

    display.capture_screen = capture
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)
    session.vm_id = vm_id
    screenshot = await session._capture_screenshot()
    assert screenshot.frame_number == display.screenshots_taken == 2
    assert screenshot.image_bytes != black.getvalue()
    assert display.key_presses == ["down:escape", "up:escape", "down:menu", "up:menu"]
    assert display.clicks == [{"button": 1, "x": 512, "y": 384}]
