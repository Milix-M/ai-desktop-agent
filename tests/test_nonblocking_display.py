import asyncio
import threading
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.server.app import app
from ai_desktop_agent.vm.vnc_client import VNCClient


async def test_stop_is_served_while_typing_and_worker_drains():
    entered = threading.Event()
    release = threading.Event()
    sent = []
    display = VNCClient()
    display._connected = True
    display._client = Mock()

    def press(key):
        sent.append(key)
        entered.set()
        assert release.wait(5)

    display._client.keyPress.side_effect = press
    task = asyncio.create_task(
        ActionExecutor(display).execute(Action(ActionType.TYPE, {"text": "a" * 100}))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert response.status_code == 200
        display.request_stop()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # no new owner until the in-flight key finishes
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sent == ["a"]
