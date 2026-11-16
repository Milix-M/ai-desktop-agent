import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
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
        # Empty loop after planning represents a completed run without real GUI waits.
        async def finish():
            from ai_desktop_agent.agent.state import AgentState

            session.loop.context.state = AgentState.COMPLETED

        session._execute_phase = finish
    await session.start_async("test")
    await asyncio.sleep(0)
    if outcome == "cancel":
        session.stop()
    await asyncio.gather(session._task, return_exceptions=True)
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
