import asyncio
from unittest.mock import Mock

import pytest

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_cancel_releases_held_key():
    display = FakeDisplayBackend()
    display.key_down = Mock()
    display.key_up = Mock()
    task = asyncio.create_task(
        ActionExecutor(display).execute(
            Action(ActionType.KEY_HOLD, {"key": "shift", "duration_ms": 1000})
        )
    )
    await asyncio.sleep(0)
    display.key_down.assert_called_once_with("shift")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    display.key_up.assert_called_once_with("shift")


async def test_failed_hold_releases_key(monkeypatch):
    display = FakeDisplayBackend()
    display.key_down = Mock()
    display.key_up = Mock()

    async def fail(_):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(asyncio, "sleep", fail)
    assert not await ActionExecutor(display).execute(
        Action(ActionType.KEY_HOLD, {"key": "shift", "duration_ms": 1000})
    )
    display.key_up.assert_called_once_with("shift")
