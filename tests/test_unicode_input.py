from unittest.mock import Mock

import pytest

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.vnc_client import VNCClient


@pytest.mark.parametrize("text", ["こんにちは", "hello世界", "ok\x00bad"])
async def test_unsupported_text_fails_before_partial_input(text):
    display = VNCClient()
    display._connected = True
    display._client = Mock()
    result = await ActionExecutor(display).execute(Action(ActionType.TYPE, {"text": text}))
    assert result is False
    assert display._client.mock_calls == []


def test_supported_text_still_types(monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.vm.vnc_client.time.sleep", lambda _: None)
    display = VNCClient()
    display._connected = True
    display._client = Mock()
    display.type_text("hello!")
    assert display._client.keyPress.call_count == 6
