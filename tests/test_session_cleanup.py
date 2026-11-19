import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.mark.parametrize("outcome", ["cancel", "error", "no_plan", "complete"])
async def test_every_session_exit_closes_connections(outcome):
    display = FakeDisplayBackend()
    display.disconnect = Mock(wraps=display.disconnect)
    llm = MockLLMProvider()
    llm.aclose = AsyncMock()
    if outcome == "error":
        llm.understand_instruction = AsyncMock(side_effect=RuntimeError("failure"))
    elif outcome == "cancel":

        async def wait(*args, **kwargs):
            await asyncio.Event().wait()

        llm.understand_instruction = wait
    elif outcome == "no_plan":
        from ai_desktop_agent.agent.llm.types import DecompositionResult

        llm.decompose_task = AsyncMock(
            return_value=DecompositionResult(subtasks=[], reasoning="empty")
        )
    session = TaskSession(llm=llm, display=display)
    if outcome == "complete":
        llm.decide_next_action = AsyncMock(
            side_effect=[
                ActionDecision(Action(ActionType.MOUSE_MOVE, {"x": 7, "y": 9}), "move", 1, "move"),
                ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "done", 1, "done"),
            ]
        )
    await session.start_async("test")
    await asyncio.sleep(0)
    if outcome == "cancel":
        session.stop()
    (result,) = await asyncio.gather(session._task, return_exceptions=True)
    if outcome == "complete":
        assert result is True and display.mouse_moves == [(7, 9)]
    elif outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is False
    assert not display.is_connected
    display.disconnect.assert_called_once()
    llm.aclose.assert_awaited_once()
    await session.aclose()
    display.disconnect.assert_called_once()


async def test_cancel_before_run_starts_still_closes():
    display = FakeDisplayBackend()
    session = TaskSession(llm=MockLLMProvider(), display=display)
    await session.start_async("test")
    session.stop()
    await asyncio.gather(session._task, return_exceptions=True)
    await asyncio.sleep(0)
    if session._cleanup_task:
        await session._cleanup_task
    assert not display.is_connected


@pytest.mark.parametrize("started", [False, True])
async def test_server_shutdown_closes_sessions_before_the_shared_vnc_runtime(
    tmp_path, monkeypatch, started
):
    from ai_desktop_agent.server import app as server
    from ai_desktop_agent.server.android_provision import AndroidProvisioner
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.server.vm_pool import VmPool
    from ai_desktop_agent.vm.vnc_client import VNCClient
    from tests.test_vm_pool import _FakeDocker

    entered = asyncio.Event()
    llm = MockLLMProvider()
    llm.aclose = AsyncMock()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    llm.understand_instruction = wait
    display = FakeDisplayBackend()
    session = TaskSession(llm=llm, display=display)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        server, "_pool", VmPool(client=_FakeDocker(), state_file=tmp_path / "vms.json")
    )
    monkeypatch.setattr(server, "_vm_creations", None)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session)
    monkeypatch.setattr(AndroidProvisioner, "start", AsyncMock())
    monkeypatch.setattr(AndroidProvisioner, "stop", AsyncMock())

    def stop_runtime():
        assert not display.is_connected
        llm.aclose.assert_awaited_once()

    shutdown = Mock(side_effect=stop_runtime)
    monkeypatch.setattr(VNCClient, "shutdown_runtime", shutdown)
    async with server.lifespan(server.app):
        await session.start_async("文書を保存する")
        if started:
            await entered.wait()
    shutdown.assert_called_once()
    assert not server._sessions
    assert server._active_session is None
