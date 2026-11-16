import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server import app as server
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_task_uses_selected_connection_without_connecting_default(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("VNC_HOST", "stopped-default")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda _: SimpleNamespace(id="selected"))
    display = FakeDisplayBackend()
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: display)
    default = Mock(side_effect=AssertionError("must not connect default VM"))
    monkeypatch.setattr("ai_desktop_agent.server.session.VNCClient", default)
    await server.create_task(server.CreateTaskRequest(instruction="test", vm_id="selected"))
    session = server._active_session
    assert session.display is display
    default.assert_not_called()
    session.stop()
    await asyncio.gather(session._task, return_exceptions=True)
    if session._cleanup_task:
        await session._cleanup_task


async def test_session_creation_failure_disconnects_selected_display(monkeypatch):
    monkeypatch.setattr(server, "_resolve_vm", lambda _: SimpleNamespace(id="selected"))
    display = FakeDisplayBackend()
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: display)

    def fail(display):
        raise ValueError("invalid provider")

    monkeypatch.setattr(server, "_create_session", fail)
    with pytest.raises(ValueError, match="invalid provider"):
        await server.create_task(server.CreateTaskRequest(instruction="test", vm_id="selected"))
    assert not display.is_connected
