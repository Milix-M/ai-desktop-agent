import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda _: SimpleNamespace(id="desktop"))
    monkeypatch.setattr(server, "_default_vm_id", lambda: "desktop")


@pytest.mark.parametrize("phase", ["connect", "session"])
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cancelled_submission_closes_resources_created_by_its_worker(
    isolated_server, monkeypatch, phase, cancel_count
):
    entered, release = threading.Event(), threading.Event()
    display = FakeDisplayBackend()
    display.disconnect = Mock(wraps=display.disconnect)
    llm = MockLLMProvider()
    llm.aclose = AsyncMock()
    decision = SimpleNamespace(aclose=AsyncMock())
    made = []

    def block():
        entered.set()
        assert release.wait(3)

    def connect(_):
        if phase == "connect":
            block()
        return display

    def create(display):
        session = TaskSession(llm=llm, decision=decision, display=display)
        made.append(session)
        if phase == "session":
            block()
        return session

    monkeypatch.setattr(server, "_connect_vm_display", connect)
    monkeypatch.setattr(server, "_create_session", create)
    request = asyncio.create_task(
        server.create_task(server.CreateTaskRequest(instruction="画面を確認する"))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(cancel_count):
            request.cancel()
            await asyncio.sleep(0)
        assert not request.done()
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()
    assert not display.is_connected
    display.disconnect.assert_called_once()
    assert not server._sessions
    assert server._active_session is None
    if phase == "session":
        assert len(made) == 1
        assert made[0]._task is None
        llm.aclose.assert_awaited_once()
        decision.aclose.assert_awaited_once()
    else:
        assert not made
        llm.aclose.assert_not_called()


async def test_cancelled_connection_is_closed_before_another_submission_starts(
    isolated_server, monkeypatch
):
    connecting, finish_connect = threading.Event(), threading.Event()
    closing, finish_close = threading.Event(), threading.Event()
    first = FakeDisplayBackend()
    second = FakeDisplayBackend()
    attempts = []

    def disconnect():
        closing.set()
        assert finish_close.wait(3)
        FakeDisplayBackend.disconnect(first)

    first.disconnect = disconnect

    def connect(_):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            connecting.set()
            assert finish_connect.wait(3)
            return first
        return second

    class WaitingLLM(MockLLMProvider):
        async def understand_instruction(self, *args, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(server, "_connect_vm_display", connect)
    monkeypatch.setattr(
        server, "_create_session", lambda display: TaskSession(llm=WaitingLLM(), display=display)
    )
    first_request = asyncio.create_task(
        server.create_task(server.CreateTaskRequest(instruction="画面を確認する"))
    )
    second_request = None
    try:
        assert await asyncio.to_thread(connecting.wait, 1)
        first_request.cancel()
        finish_connect.set()
        assert await asyncio.to_thread(closing.wait, 1)
        second_request = asyncio.create_task(
            server.create_task(server.CreateTaskRequest(instruction="画面を確認する"))
        )
        first_request.cancel()
        await asyncio.sleep(0.02)
        assert attempts == [0]
        assert not first_request.done()
        assert not second_request.done()
    finally:
        finish_connect.set()
        finish_close.set()
        await asyncio.gather(first_request, return_exceptions=True)
        if second_request:
            await second_request
        session = server._active_session
        if session:
            session.stop()
            await asyncio.gather(session._task, return_exceptions=True)
    assert not first.is_connected
    assert not second.is_connected


async def test_repeated_cancellation_waits_for_provider_cleanup(isolated_server, monkeypatch):
    constructing, finish_construction = threading.Event(), threading.Event()
    closing, finish_close = asyncio.Event(), asyncio.Event()
    display = FakeDisplayBackend()
    provider = MockLLMProvider()

    async def close():
        closing.set()
        await finish_close.wait()

    provider.aclose = AsyncMock(side_effect=close)

    def create(display):
        session = TaskSession(llm=provider, display=display, decision=None)
        constructing.set()
        assert finish_construction.wait(3)
        return session

    monkeypatch.setattr(server, "_connect_vm_display", lambda _: display)
    monkeypatch.setattr(server, "_create_session", create)
    request = asyncio.create_task(
        server.create_task(server.CreateTaskRequest(instruction="画面を確認する"))
    )
    try:
        assert await asyncio.to_thread(constructing.wait, 1)
        request.cancel()
        finish_construction.set()
        await asyncio.wait_for(closing.wait(), 1)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert server._vm_operations.locked("desktop")
    finally:
        finish_construction.set()
        finish_close.set()
        await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()
    assert not display.is_connected
    provider.aclose.assert_awaited_once()
    assert not server._vm_operations.locked("desktop")
