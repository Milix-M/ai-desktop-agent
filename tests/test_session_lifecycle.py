"""Public run and background start must obey the same ownership and stop rules."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import DecompositionResult
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def blocked_session(tmp_path, mode):
    entered = asyncio.Event()
    llm = MockLLMProvider()

    async def understand(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    llm.understand_instruction = understand
    display = FakeDisplayBackend()
    display.request_stop = Mock()
    session = TaskSession(llm=llm, display=display, store=TaskStore(tmp_path), decision=None)
    if mode == "background":
        await session.start_async("元の文書を保存する")
        task = session._task
    else:
        task = asyncio.create_task(session.run("元の文書を保存する"))
    await asyncio.wait_for(entered.wait(), 2)
    return session, task


async def finish(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["direct", "background"])
async def test_stop_signals_the_display_and_drains_the_current_run(tmp_path, mode):
    session, task = await blocked_session(tmp_path, mode)
    try:
        assert session.is_running
        session.stop()
        session.display.request_stop.assert_called_once()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert session.loop.state == AgentState.FAILED
        record = session._store.load(session.id)
        assert record.state == "failed" and record.success is False
        assert not session.is_running and not session.display.is_connected
        assert session._run_owner is None
    finally:
        await finish(task)


@pytest.mark.parametrize("mode", ["direct", "background"])
async def test_rejected_second_run_cannot_rewrite_the_active_instruction_or_its_owner(
    tmp_path, mode
):
    session, task = await blocked_session(tmp_path, mode)
    goal = session.loop.context.goal
    try:
        with pytest.raises((RuntimeError, ValueError)):
            await session.run("別の文書を削除する")
        assert session.loop.context.goal is goal
        assert session._instruction == goal.description == "元の文書を保存する"
        assert session._run_owner is task
        assert not task.done()
        session.stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        saved = session._store.load(session.id)
        assert (
            saved.instruction == "元の文書を保存する"
            and saved.goal["description"] == "元の文書を保存する"
        )
    finally:
        await finish(task)


@pytest.mark.parametrize("state", ["failed", "closed-before-start"])
async def test_a_closed_session_cannot_be_reused_or_acquire_a_new_owner(tmp_path, state):
    llm = MockLLMProvider()
    llm.decompose_task = AsyncMock(return_value=DecompositionResult([], "no plan"))
    session = TaskSession(
        llm=llm, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    if state == "failed":
        assert await session.run("元の文書を保存する") is False
    else:
        await session.aclose()
    previous = session.snapshot()
    with pytest.raises((RuntimeError, ValueError)):
        await session.run("別の文書を削除する")
    assert session._run_owner is None and not session.is_running
    assert session.snapshot().instruction == previous.instruction
    assert session.snapshot().goal == previous.goal
    assert not session.display.is_connected


async def test_a_start_transition_hook_failure_is_finalized_and_releases_ownership(tmp_path):
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store, decision=None
    )
    session.loop.on_enter(AgentState.UNDERSTANDING, Mock(side_effect=RuntimeError("hook failed")))
    completed = Mock()
    session.on_complete(completed)
    assert await session.run("文書を保存する") is False
    assert session._run_owner is None and not session.is_running
    assert not session.display.is_connected
    assert store.load(session.id).state == "failed"
    completed.assert_called_once_with(False)
