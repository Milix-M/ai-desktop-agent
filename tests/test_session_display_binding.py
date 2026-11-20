"""A session keeps one display target from admission through resource cleanup."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, DecompositionResult
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.server import session as module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.submissions import fingerprint, key_digest
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class CountedDisplay(FakeDisplayBackend):
    def __init__(self):
        super().__init__(32, 24)
        self.disconnects = 0
        self.close_failure = False

    def disconnect(self):
        self.disconnects += 1
        if self.close_failure:
            self.close_failure = False
            raise OSError("display disconnect acknowledgement lost")
        super().disconnect()


async def test_idle_replacement_releases_the_old_display_and_executes_only_on_the_new_one():
    old, new = CountedDisplay(), CountedDisplay()
    session = TaskSession(llm=MockLLMProvider(), display=old, decision=None)
    try:
        session.set_display(new)
        assert session.display is new and old.disconnects == 1 and not old.is_connected
        assert await session.executor.execute(Action(ActionType.MOUSE_MOVE, {"x": 7, "y": 9}))
        assert old.mouse_moves == [] and new.mouse_moves == [(7, 9)]
    finally:
        await session.aclose()
    assert new.disconnects == 1 and not new.is_connected


async def test_same_display_is_a_noop_and_keeps_the_executor_diagnostic(monkeypatch):
    display = CountedDisplay()
    session = TaskSession(llm=MockLLMProvider(), display=display, decision=None)

    def failed_key(key):
        raise OSError("input acknowledgement lost")

    monkeypatch.setattr(display, "key_press", failed_key)
    try:
        assert not await session.executor.execute(Action(ActionType.KEY_PRESS, {"key": "Return"}))
        executor, error = session.executor, session.executor.last_error
        session.set_display(display)
        assert session.executor is executor and session.executor.last_error == error
        assert display.disconnects == 0
    finally:
        await session.aclose()


async def test_disconnect_failure_keeps_the_old_owner_until_replacement_can_be_retried():
    old, new = CountedDisplay(), CountedDisplay()
    old.close_failure = True
    session = TaskSession(llm=MockLLMProvider(), display=old, decision=None)
    executor = session.executor
    try:
        with pytest.raises(OSError, match="acknowledgement"):
            session.set_display(new)
        assert session.display is old and session.executor is executor
        assert old.is_connected and new.disconnects == 0
        session.set_display(new)
        assert session.display is new and old.disconnects == 2 and not old.is_connected
    finally:
        await session.aclose()
        if old.is_connected:
            old.disconnect()
        if new.is_connected:
            new.disconnect()
    assert new.disconnects == 1


async def test_a_missing_replacement_cannot_disconnect_the_current_display():
    old = CountedDisplay()
    session = TaskSession(llm=MockLLMProvider(), display=old, decision=None)
    try:
        with pytest.raises(ValueError):
            session.set_display(None)
        assert session.display is old and old.is_connected and old.disconnects == 0
    finally:
        await session.aclose()


@pytest.mark.parametrize("phase", ["queued", "running", "closing", "closed"])
async def test_admitted_or_closing_sessions_reject_a_different_display_without_adopting_it(phase):
    old, new = CountedDisplay(), CountedDisplay()
    entered, release = asyncio.Event(), asyncio.Event()
    llm = MockLLMProvider()

    async def wait(*args, **kwargs):
        entered.set()
        await release.wait()

    if phase in ("queued", "running"):
        llm.understand_instruction = wait
    elif phase == "closing":
        llm.aclose = AsyncMock(side_effect=wait)
    session = TaskSession(llm=llm, display=old, decision=None)
    admission, closing = None, None
    try:
        if phase in ("queued", "running"):
            admission = asyncio.create_task(session.start_async("元の画面を操作する"))
            await asyncio.sleep(0)
            if phase == "running":
                await asyncio.wait_for(entered.wait(), 1)
        elif phase == "closing":
            closing = asyncio.create_task(session.aclose())
            await asyncio.wait_for(entered.wait(), 1)
        else:
            await session.aclose()
        executor = session.executor
        with pytest.raises(RuntimeError):
            session.set_display(new)
        assert session.display is old and session.executor is executor and new.is_connected
        session.set_display(old)
        assert session.executor is executor
    finally:
        session.stop()
        release.set()
        if closing is not None:
            await closing
        await session.aclose()
        if admission is not None:
            await asyncio.gather(admission, return_exceptions=True)
        new.disconnect()


async def test_a_held_model_call_cannot_redirect_the_next_action_to_another_display(tmp_path):
    old, new = CountedDisplay(), CountedDisplay()
    entered, release = asyncio.Event(), asyncio.Event()
    llm = MockLLMProvider(
        decompose_result=DecompositionResult([Subtask("s1", "move pointer")], "one step")
    )
    understand = llm.understand_instruction

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await understand(*args, **kwargs)

    llm.understand_instruction = held
    llm.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(Action(ActionType.MOUSE_MOVE, {"x": 7, "y": 9}), "move", 1, "move"),
            ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "done", 1, "done"),
        ]
    )
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=old, store=store, vm_id="desk-original", decision=None)
    run = asyncio.create_task(session.run("元の画面でポインターを移動する"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rejected = False
        try:
            session.set_display(new)
        except RuntimeError:
            rejected = True
        release.set()
        assert await asyncio.wait_for(run, 3)
        assert rejected and old.mouse_moves == [(7, 9)] and new.mouse_moves == []
        record = store.load(session.id)
        assert record.vm_id == "desk-original" and record.state == "completed"
    finally:
        release.set()
        session.stop()
        await asyncio.gather(run, return_exceptions=True)
        await session.aclose()
        old.disconnect()
        new.disconnect()


async def test_rejected_prestart_input_still_allows_selecting_a_display_before_retry():
    old, new = CountedDisplay(), CountedDisplay()
    session = TaskSession(llm=MockLLMProvider(), display=old, decision=None)
    try:
        with pytest.raises(ValueError):
            await session.run(None)
        session.set_display(new)
        assert session.display is new and old.disconnects == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("phase", ["running", "closed"])
async def test_rejected_background_admission_preserves_the_original_session(phase, tmp_path):
    entered = asyncio.Event()
    llm = MockLLMProvider()
    requests = []

    async def held(goal, *args, **kwargs):
        requests.append(goal.description)
        entered.set()
        await asyncio.Event().wait()

    llm.understand_instruction = held
    store = TaskStore(tmp_path)
    display = CountedDisplay()
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    first = None
    try:
        if phase == "running":
            first = asyncio.create_task(session.start_async("元の指示"))
            await asyncio.wait_for(entered.wait(), 1)
        else:
            await session.aclose()
        with pytest.raises(RuntimeError):
            await session.start_async("割り込む指示")
        assert session.display is display
        if phase == "running":
            assert requests == ["元の指示"] and session.is_running
            assert store.load(session.id).instruction == "元の指示"
        else:
            assert not requests and not session.is_running and store.load(session.id) is None
    finally:
        session.stop()
        await session.aclose()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("mode", ["direct", "background", "close", "overlap", "submission"])
async def test_a_pending_display_switch_cannot_race_session_admission_or_cleanup(mode, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class PendingDisconnect(CountedDisplay):
        def disconnect(self):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test controller did not finish the pending switch")
            super().disconnect()

    old, new = PendingDisconnect(), CountedDisplay()
    llm = MockLLMProvider()
    model_entered = asyncio.Event()

    async def wait_for_stop(*args, **kwargs):
        model_entered.set()
        await asyncio.Event().wait()

    llm.understand_instruction = wait_for_stop
    session = TaskSession(llm=llm, display=old, decision=None)
    binding = asyncio.create_task(asyncio.to_thread(session.set_display, new))
    closing = None
    admission = None
    fallback = None
    claim = None
    third = CountedDisplay()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if mode == "submission":
            store = TaskStore(tmp_path)
            claim = store.claim_submission(
                key_digest("pending-display-switch"), fingerprint({"instruction": "画面を確認する"})
            )
            original_id = session.id
            with pytest.raises(RuntimeError):
                session.bind_submission(claim, store)
            assert session.id == original_id and claim.session is None
            release.set()
            await binding
            session.bind_submission(claim, store)
            assert session.id == claim.task_id and claim.session is session
            assert session.display is new
        elif mode == "overlap":
            fallback = threading.Timer(0.2, release.set)
            fallback.start()
            with pytest.raises(RuntimeError):
                session.set_display(third)
            assert third.disconnects == 0
            release.set()
            await binding
        elif mode == "close":
            closing = asyncio.create_task(session.aclose())
            await asyncio.sleep(0.01)
            assert not closing.done()
            release.set()
            with pytest.raises(RuntimeError):
                await binding
            await closing
            assert session.display is old and not old.is_connected and new.is_connected
        else:
            admission = asyncio.create_task(
                session.run("元の画面を確認する")
                if mode == "direct"
                else session.start_async("元の画面を確認する")
            )
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(asyncio.shield(admission), 0.2)
            assert not model_entered.is_set()
            release.set()
            await binding
            assert session.display is new and not old.is_connected
    finally:
        release.set()
        if fallback is not None:
            fallback.cancel()
            fallback.join()
        session.stop()
        await asyncio.gather(binding, return_exceptions=True)
        if admission is not None:
            admission.cancel()
            await asyncio.gather(admission, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await session.aclose()
        if claim is not None:
            claim.close()
        old.disconnect()
        new.disconnect()
        third.disconnect()


async def test_executor_construction_failure_does_not_release_or_replace_the_old_display(
    monkeypatch,
):
    old, new = CountedDisplay(), CountedDisplay()
    session = TaskSession(llm=MockLLMProvider(), display=old, decision=None)
    previous = session.executor
    create = module.ActionExecutor

    def fail(display):
        raise OSError("executor initialization failed")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(module, "ActionExecutor", fail)
            with pytest.raises(OSError):
                session.set_display(new)
        assert session.display is old and session.executor is previous and old.is_connected
        assert old.disconnects == 0 and new.disconnects == 0
        session.set_display(new)
        assert isinstance(session.executor, create) and not old.is_connected
    finally:
        await session.aclose()
        if old.is_connected:
            old.disconnect()
        if new.is_connected:
            new.disconnect()


@pytest.mark.parametrize("phase", ["closed", "already-bound", "display-bound"])
async def test_submission_identity_and_display_target_cannot_be_reassigned_after_admission(
    tmp_path, phase
):
    class TaggedLLM(MockLLMProvider):
        session_id = None

    store = TaskStore(tmp_path)
    payload = fingerprint({"instruction": "元の画面を確認する", "vm_id": "desk-original"})
    first = store.claim_submission(key_digest("display-binding-first-claim"), payload)
    second = store.claim_submission(key_digest("display-binding-second-claim"), payload)
    old, new = CountedDisplay(), CountedDisplay()
    llm = TaggedLLM()
    session = TaskSession(llm=llm, display=old, decision=None)
    original_id = session.id
    try:
        if phase == "closed":
            await session.aclose()
            with pytest.raises(RuntimeError):
                session.bind_submission(first, store)
            assert session.id == original_id and first.session is None
        else:
            session.bind_submission(first, store)
            assert session.id == first.task_id and llm.session_id == first.task_id
            if phase == "already-bound":
                with pytest.raises(RuntimeError):
                    session.bind_submission(second, store)
                assert session.id == first.task_id and second.session is None
            else:
                executor = session.executor
                with pytest.raises(RuntimeError):
                    session.set_display(new)
                session.set_display(old)
                assert session.display is old and session.executor is executor
            assert first.session is session
    finally:
        await session.aclose()
        first.close()
        second.close()
        old.disconnect()
        new.disconnect()
