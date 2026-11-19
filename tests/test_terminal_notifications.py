"""Terminal observers cannot change an already saved execution outcome."""

import asyncio
import threading
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import DecompositionResult
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


def make_session(tmp_path, outcome, *, seconds=5, store=None):
    model = MockLLMProvider(
        decompose_result=DecompositionResult(
            [Subtask("inspect", "inspect the report")] if outcome == "success" else [],
            "report inspected" if outcome == "success" else "no valid plan",
        )
    )
    session = TaskSession(
        llm=model,
        display=FakeDisplayBackend(),
        store=store or TaskStore(tmp_path),
        vm_id="desk-terminal",
        decision=None,
        limits=ExecutionLimits(max_seconds=seconds),
    )
    session._capture_screenshot = AsyncMock(return_value=session.display.capture_screen())
    return session


async def saved_detail(tmp_path, session, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        result = await client.get(f"/tasks/{session.id}")
    assert result.status_code == 200
    return result.json()


@pytest.mark.parametrize("outcome", ["success", "planning-failure"])
@pytest.mark.parametrize("phase", ["state", "complete"])
@pytest.mark.parametrize("mode", ["direct", "background"])
async def test_http_stop_during_terminal_delivery_preserves_the_saved_result_and_one_completion(
    tmp_path, monkeypatch, outcome, phase, mode, session_retry_sleep
):
    session = make_session(tmp_path, outcome)
    success = outcome == "success"
    state = "completed" if success else "failed"
    reason = None if success else "planning_failed"
    entered, released = asyncio.Event(), asyncio.Event()
    states, completions = [], []

    async def changed(current, context):
        if current in {AgentState.COMPLETED, AgentState.FAILED}:
            states.append(current.value)
            if phase == "state":
                entered.set()
                await released.wait()

    async def complete(result):
        saved = TaskStore(tmp_path).load(session.id)
        completions.append((result, saved.state, saved.success, saved.stop_reason))
        if phase == "complete":
            entered.set()
            await released.wait()

    session.on_state_change(changed)
    session.on_complete(complete)
    monkeypatch.setattr(server, "_store", session._store)
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session)
    if mode == "background":
        await session.start_async("inspect without entering text")
        task = session._task
    else:
        task = asyncio.create_task(session.run("inspect without entering text"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = TaskStore(tmp_path).load(session.id)
        assert (before.state, before.success, before.stop_reason) == (state, success, reason)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            result = await client.post(
                "/tasks/current/stop",
                params={"vm_id": session.vm_id, "session_id": session.id},
            )
        assert result.status_code == 200
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert states == [state]
        assert completions == [(success, state, success, reason)]
        detail = await saved_detail(tmp_path, session, monkeypatch)
        assert (detail["state"], detail["success"], detail["stop_reason"]) == (
            state,
            success,
            reason,
        )
        assert not session.display.text_inputs and not session.display.is_connected
        assert not session.is_running
    finally:
        released.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["success", "planning-failure"])
async def test_external_cancellation_of_completion_delivery_does_not_emit_another_result(
    tmp_path, monkeypatch, outcome, session_retry_sleep
):
    session = make_session(tmp_path, outcome)
    entered, released = asyncio.Event(), asyncio.Event()
    completions = []

    async def complete(success):
        completions.append(success)
        entered.set()
        await released.wait()

    session.on_complete(complete)
    await session.start_async("inspect without entering text")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        session._task.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        expected = outcome == "success"
        assert completions == [expected]
        detail = await saved_detail(tmp_path, session, monkeypatch)
        assert detail["success"] is expected
        assert detail["state"] == ("completed" if expected else "failed")
        assert detail["stop_reason"] == (None if expected else "planning_failed")
        assert not session.display.is_connected
    finally:
        released.set()
        if not session._task.done():
            session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome,phase",
    [
        ("success", "state"),
        ("success", "complete"),
        ("planning-failure", "state"),
        ("planning-failure", "error"),
        ("planning-failure", "complete"),
    ],
)
async def test_terminal_notifications_outliving_the_execution_deadline_preserve_the_outcome(
    tmp_path, monkeypatch, outcome, phase, session_retry_sleep
):
    session = make_session(tmp_path, outcome, seconds=1)
    entered, released = asyncio.Event(), asyncio.Event()
    states, errors, completions = [], [], []

    async def changed(state, context):
        if state in {AgentState.COMPLETED, AgentState.FAILED}:
            states.append(state.value)
            if phase == "state":
                entered.set()
                await released.wait()

    async def error(message):
        errors.append(message)
        if phase == "error":
            entered.set()
            await released.wait()

    async def complete(success):
        completions.append(success)
        if phase == "complete":
            entered.set()
            await released.wait()

    session.on_state_change(changed)
    session.on_error(error)
    session.on_complete(complete)
    await session.start_async("inspect without entering text")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(1.1)
        released.set()
        expected = outcome == "success"
        assert await asyncio.wait_for(asyncio.shield(session._task), 2) is expected
        state = "completed" if expected else "failed"
        assert states == [state] and completions == [expected]
        assert len(errors) == (0 if expected else 1)
        detail = await saved_detail(tmp_path, session, monkeypatch)
        assert detail["state"] == state and detail["success"] is expected
        assert detail["stop_reason"] == (None if expected else "planning_failed")
        assert 0 < detail["execution_usage"]["elapsed_seconds"] < 1
        assert session.snapshot().execution_usage["elapsed_seconds"] < 1
        assert not session.display.is_connected
    finally:
        released.set()
        if not session._task.done():
            session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["stop", "deadline"])
async def test_terminal_write_is_still_part_of_execution_and_must_finish_before_success(
    tmp_path, monkeypatch, interruption, session_retry_sleep
):
    entered, released = threading.Event(), threading.Event()

    class SlowStore(TaskStore):
        def save(self, record):
            if record.state == "completed":
                entered.set()
                if not released.wait(3):
                    raise RuntimeError("test did not release the terminal write")
            return super().save(record)

    session = make_session(tmp_path, "success", seconds=1, store=SlowStore(tmp_path))
    completions = []
    session.on_complete(completions.append)
    await session.start_async("inspect without entering text")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if interruption == "stop":
            session.stop()
        else:
            await asyncio.sleep(1.1)
        released.set()
        if interruption == "stop":
            with pytest.raises(asyncio.CancelledError):
                await session._task
        else:
            assert await session._task is False
        assert completions == [False]
        detail = await saved_detail(tmp_path, session, monkeypatch)
        assert detail["state"] == "failed" and detail["success"] is False
        assert detail["stop_reason"] == (
            "cancelled" if interruption == "stop" else "duration_limit"
        )
        assert not session.display.is_connected
    finally:
        released.set()
        if not session._task.done():
            session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_failed_terminal_save_cannot_publish_success(
    tmp_path, monkeypatch, session_retry_sleep
):
    class FailingStore(TaskStore):
        def save(self, record):
            if record.state == "completed":
                raise StorageError("injected terminal write failure")
            return super().save(record)

    session = make_session(tmp_path, "success", store=FailingStore(tmp_path))
    states, completions = [], []
    session.on_state_change(lambda state, context: states.append(state.value))
    session.on_complete(completions.append)
    assert await session.run("inspect without entering text") is False
    assert "completed" not in states and states[-1] == "failed"
    assert completions == [False]
    detail = await saved_detail(tmp_path, session, monkeypatch)
    assert detail["state"] == "failed" and detail["stop_reason"] == "storage_error"
    assert not session.display.is_connected
