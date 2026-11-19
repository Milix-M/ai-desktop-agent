"""Public control races preserve durable failure causes and resource ownership."""

import asyncio
import threading
from dataclasses import asdict

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import DecompositionResult
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_stop_while_saving_a_business_failure_preserves_its_original_cause(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class DelayedFailureStore(TaskStore):
        def save(self, record):
            if record.state == "failed" and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().save(record)

    store = DelayedFailureStore(tmp_path)
    model = MockLLMProvider(decompose_result=DecompositionResult([], "no valid plan"))
    display = FakeDisplayBackend(screen_width=96, screen_height=64)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    completions = []
    session.on_complete(completions.append)
    run = asyncio.create_task(session.run("保存済み文書を確認する"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        session.stop()
        assert not run.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(run), 3)
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == "failed" and saved.success is False
        assert saved.stop_reason == "planning_failed" and "no valid plan" in saved.error_message
        assert completions == [False]
        assert model.decide_call_count == 0 and not display.is_connected
    finally:
        release.set()
        session.stop()
        await asyncio.gather(run, return_exceptions=True)
        await session.aclose()


async def test_concurrent_control_notifications_stop_after_the_first_storage_failure(tmp_path):
    entered = asyncio.Event()

    class FailingControlStore(TaskStore):
        fail_next = False
        failed_writes = 0

        def save(self, record):
            if self.fail_next:
                self.fail_next = False
                self.failed_writes += 1
                raise OSError("disk temporarily unavailable")
            return super().save(record)

    model = MockLLMProvider()

    async def decide(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    model.decide_next_action = decide
    store = FailingControlStore(tmp_path)
    display = FakeDisplayBackend(screen_width=96, screen_height=64)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    completions = []
    session.on_complete(completions.append)
    run = asyncio.create_task(session.run("文書を確認する"))
    notifications = []
    try:
        await asyncio.wait_for(entered.wait(), 3)
        store.fail_next = True
        # Both notifications enter through synchronous public control methods.
        notifications = [session.pause(), session.resume()]
        results = await asyncio.wait_for(asyncio.gather(*notifications, return_exceptions=True), 3)
        assert all(isinstance(result, StorageError) for result in results)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(run), 3)
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == "failed" and saved.success is False
        assert saved.stop_reason == "storage_error" and "保存" in saved.error_message
        assert store.failed_writes == 1 and completions == [False]
        assert not display.is_connected and not display.clicks and not display.text_inputs
    finally:
        session.stop()
        await asyncio.gather(run, *notifications, return_exceptions=True)
        await session.aclose()


async def test_duplicate_or_inactive_controls_do_not_emit_events_or_rewrite_saved_completion(
    tmp_path,
):
    session = TaskSession(
        llm=MockLLMProvider(),
        display=FakeDisplayBackend(screen_width=96, screen_height=64),
        store=TaskStore(tmp_path),
        decision=None,
    )
    states, notifications = [], []
    exercised = False

    def changed(state, context):
        nonlocal exercised
        states.append(state)
        if state == AgentState.EXECUTING and not exercised:
            exercised = True
            notifications.append(session.pause())
            assert session.pause() is None
            notifications.append(session.resume())
            assert session.resume() is None

    session.on_state_change(changed)
    try:
        assert session.pause() is None and session.resume() is None
        assert await asyncio.wait_for(session.run("文書を確認する"), 5)
        await asyncio.gather(*notifications)
        saved = asdict(TaskStore(tmp_path).load(session.id))
        observed = list(states)
        assert exercised and saved["state"] == "completed"
        assert session.pause() is None and session.resume() is None
        await asyncio.sleep(0)
        assert states == observed and asdict(TaskStore(tmp_path).load(session.id)) == saved
    finally:
        await session.aclose()
