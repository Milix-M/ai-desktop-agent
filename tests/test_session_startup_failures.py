"""Public session admission and cleanup stay bounded even before execution begins."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, DecompositionResult
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.mark.parametrize("instruction", [None, {"instruction": "save"}, "bad\ud800text"])
@pytest.mark.parametrize("mode", ["direct", "background"])
async def test_invalid_instruction_is_rejected_before_admission_and_can_be_corrected(
    tmp_path, instruction, mode
):
    llm = MockLLMProvider()
    llm.understand_instruction = AsyncMock(wraps=llm.understand_instruction)
    llm.decompose_task = AsyncMock(return_value=DecompositionResult([], "no executable plan"))
    display = FakeDisplayBackend()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    started = asyncio.create_task(
        session.start_async(instruction) if mode == "background" else session.run(instruction)
    )
    try:
        with pytest.raises(ValueError):
            await asyncio.wait_for(asyncio.shield(started), 0.2)
        assert session._task is None and not session.is_running
        assert session.loop.state == AgentState.IDLE and store.list() == []
        assert display.is_connected
        llm.understand_instruction.assert_not_awaited()
        if mode == "background":
            await session.start_async("文書を保存する")
            assert await session._task is False
        else:
            assert await session.run("文書を保存する") is False
        record = store.load(session.id)
        assert record.instruction == "文書を保存する" and record.stop_reason == "planning_failed"
    finally:
        started.cancel()
        await asyncio.gather(started, return_exceptions=True)
        if session._task is not None:
            await asyncio.gather(session._task, return_exceptions=True)
        await session.aclose()


async def test_public_stop_before_the_background_run_enters_still_saves_and_closes(tmp_path):
    llm = MockLLMProvider()
    llm.aclose = AsyncMock()
    display = FakeDisplayBackend()
    display.disconnect = Mock(wraps=display.disconnect)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    completed = Mock()
    session.on_complete(completed)
    admission = asyncio.create_task(session.start_async("元の文書を保持する"))
    # start_async creates the run task and yields at its admission event. The
    # next ready turn belongs to this caller, before that newly queued run.
    await asyncio.sleep(0)
    session.stop()
    await asyncio.wait_for(admission, 2)
    with pytest.raises(asyncio.CancelledError):
        await session._task
    await session.aclose()
    record = store.load(session.id)
    assert record.instruction == "元の文書を保持する"
    assert record.state == "failed" and record.stop_reason == "cancelled"
    assert record.success is False and not session.is_running
    completed.assert_called_once_with(False)
    display.disconnect.assert_called_once()
    llm.aclose.assert_awaited_once()


@pytest.mark.parametrize("mode", ["direct", "background", "before-run"])
async def test_closing_an_active_session_stops_and_drains_its_run_before_returning(tmp_path, mode):
    entered = asyncio.Event()
    llm = MockLLMProvider()

    async def understand(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    llm.understand_instruction = understand
    llm.aclose = AsyncMock()
    display = FakeDisplayBackend()
    display.request_stop = Mock()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    if mode == "direct":
        run = asyncio.create_task(session.run("文書を保持する"))
        await asyncio.wait_for(entered.wait(), 1)
        admission = None
    else:
        admission = asyncio.create_task(session.start_async("文書を保持する"))
        await asyncio.sleep(0)
        run = session._task
        if mode == "background":
            await asyncio.wait_for(entered.wait(), 1)
    try:
        async with asyncio.timeout(2):
            await session.aclose()
        assert run.done() and not session.is_running
        assert not display.is_connected
        assert store.load(session.id).state == "failed"
        assert store.load(session.id).stop_reason == "cancelled"
        llm.aclose.assert_awaited_once()
        display.request_stop.assert_called_once()
        if mode == "before-run":
            assert not entered.is_set()
    finally:
        session.stop()
        await asyncio.gather(run, return_exceptions=True)
        if admission is not None:
            await asyncio.gather(admission, return_exceptions=True)
        await session.aclose()


@pytest.mark.parametrize("failure", ["display", "llm", "decision"])
async def test_a_cleanup_failure_does_not_skip_other_owned_connections(failure):
    llm = MockLLMProvider()
    llm.aclose = AsyncMock(side_effect=OSError("model close failed") if failure == "llm" else None)
    decision = Mock()
    decision.aclose = AsyncMock(
        side_effect=OSError("decision close failed") if failure == "decision" else None
    )
    display = FakeDisplayBackend()
    display.disconnect = Mock(
        side_effect=OSError("display close failed") if failure == "display" else None,
        wraps=display.disconnect,
    )
    session = TaskSession(llm=llm, display=display, decision=decision)
    await session.aclose()
    await session.aclose()
    display.disconnect.assert_called_once()
    llm.aclose.assert_awaited_once()
    decision.aclose.assert_awaited_once()


async def test_invalid_optional_decision_configuration_preserves_the_main_model(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "unsupported-provider")
    llm = MockLLMProvider()
    llm.aclose = AsyncMock()
    session = TaskSession(llm=llm, display=FakeDisplayBackend())
    assert session.decision is None and session.llm is llm
    await session.aclose()
    llm.aclose.assert_awaited_once()


def test_a_missing_display_configuration_is_reported_before_a_session_can_start(monkeypatch):
    monkeypatch.delenv("VNC_HOST", raising=False)
    with pytest.raises(ValueError, match="VNC_HOST"):
        TaskSession(llm=MockLLMProvider(), decision=None)


async def test_cancelling_close_waits_for_provider_cleanup_and_preserves_the_terminal_record(
    tmp_path,
):
    executing, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    llm = MockLLMProvider()

    async def understand(*args, **kwargs):
        executing.set()
        await asyncio.Event().wait()

    async def close():
        closing.set()
        await release.wait()

    llm.understand_instruction = understand
    llm.aclose = AsyncMock(side_effect=close)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), store=store, decision=None)
    await session.start_async("保存済みの文書を確認する")
    await asyncio.wait_for(executing.wait(), 1)
    cleanup = asyncio.create_task(session.aclose())
    try:
        await asyncio.wait_for(closing.wait(), 1)
        for _ in range(2):
            cleanup.cancel()
            await asyncio.sleep(0)
        assert not cleanup.done() and session.is_running
        assert store.load(session.id).stop_reason == "cancelled"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(cleanup), 2)
        assert not session.is_running and not session.display.is_connected
        llm.aclose.assert_awaited_once()
        assert store.load(session.id).state == "failed"
    finally:
        release.set()
        session.stop()
        await asyncio.gather(session._task, cleanup, return_exceptions=True)
        await session.aclose()


@pytest.mark.parametrize("success", [False, True])
async def test_closing_after_an_outcome_is_saved_preserves_the_original_run_result(
    tmp_path, success, session_retry_sleep
):
    entered, release = asyncio.Event(), asyncio.Event()
    llm = MockLLMProvider()
    if success:
        llm.decide_next_action = AsyncMock(
            side_effect=[
                ActionDecision(
                    Action(ActionType.MOUSE_MOVE, {"x": 20, "y": 30}), "inspect", 1, "inspect"
                ),
                ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "saved", 1, "saved"),
            ]
        )
    else:
        llm.decompose_task = AsyncMock(return_value=DecompositionResult([], "no executable plan"))

    async def close():
        entered.set()
        await release.wait()

    llm.aclose = AsyncMock(side_effect=close)
    display = FakeDisplayBackend()
    display.request_stop = Mock()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    completed = Mock()
    session.on_complete(completed)
    run = asyncio.create_task(session.run("文書を保持する"))
    external = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        record = store.load(session.id)
        assert record.success is success
        external = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(run, external, return_exceptions=True)
        assert results == [success, None]
        display.request_stop.assert_not_called()
        assert store.load(session.id).to_dict() == record.to_dict()
        completed.assert_called_once_with(success)
    finally:
        release.set()
        if external is not None:
            await asyncio.gather(run, external, return_exceptions=True)
        else:
            await asyncio.gather(run, return_exceptions=True)
        await session.aclose()
