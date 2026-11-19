"""A subtask deadline covers all phases and cannot authorize a late operation."""

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.decision.types import DecisionRecovery, DecisionVerify
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


def bounded_model(seconds=0.2):
    return MockLLMProvider(
        decompose_result=DecompositionResult(
            [
                Subtask(
                    "s1",
                    "inspect the report",
                    expected_outcome="report visible",
                    timeout_seconds=seconds,
                )
            ],
            "inspect",
        )
    )


@pytest.mark.parametrize(
    "phase", ["decision", "verification", "recovery", "replan", "paused", "manual"]
)
async def test_each_phase_expires_with_readable_history_and_no_late_input(
    tmp_path, monkeypatch, phase, session_retry_sleep
):
    model = bounded_model()
    display = FakeDisplayBackend()
    entered = asyncio.Event()

    async def late(value):
        entered.set()
        await asyncio.sleep(0.4)
        return value

    if phase == "decision":
        model.decide_next_action = lambda *a, **kw: late(
            ActionDecision(Action(ActionType.TYPE, {"text": "late input"}), "", 1, "inspect")
        )
    elif phase in {"verification", "recovery", "replan"}:
        if phase == "verification":
            model.verify_result = lambda *a, **kw: late(
                VerificationResult(True, "visible", "visible")
            )
        else:
            model._verify_result = VerificationResult(False, "not visible", "missing")
            plan = RecoveryPlan(
                RecoveryStrategy.REPLAN_SUBTASK
                if phase == "replan"
                else RecoveryStrategy.ALTERNATIVE_APPROACH,
                [Action(ActionType.TYPE, {"text": "late input"})] if phase == "recovery" else [],
                "inspect again",
                True,
            )
            if phase == "recovery":
                model.recover_from_error = lambda *a, **kw: late(plan)
            else:
                model._recover_result = plan
                first = model._decompose_result
                calls = 0

                async def decompose(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    return first if calls == 1 else await late(first)

                model.decompose_task = decompose
    elif phase == "manual":
        model._decide_result = ActionDecision(
            Action(ActionType.REQUEST_USER_INPUT, {"reason": "authenticate"}), "", 1, "authenticate"
        )
    session = TaskSession(
        llm=model,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=2),
    )
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    if phase == "paused":

        async def pause():
            entered.set()
            session.pause()
            await session._wait_if_paused()

        session._execute_phase = pause
    if phase == "manual":

        async def resume_later(state, context):
            if state == AgentState.AWAITING_USER:
                entered.set()
                await asyncio.sleep(0.4)
                if session.manual_input:
                    session.resume(
                        manual_input_id=session.manual_input["id"], allow_screen_share=True
                    )

        session.on_state_change(resume_later)
    completed = []
    session.on_complete(completed.append)
    assert await session.run("inspect the report") is False
    assert entered.is_set(), "the intended phase was not reached"
    assert display.text_inputs == [] and not display.is_connected
    saved = TaskStore(tmp_path).load(session.id)
    assert saved.state == "failed" and saved.success is False
    assert saved.stop_reason == "subtask_timeout" and "inspect the report" in saved.error_message
    assert saved.current_subtask_index == 0
    assert completed == [False] and asyncio.current_task().cancelling() == 0
    assert session.manual_input is None and session._manual_resume is None
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.get(f"/tasks/{session.id}")
    assert response.status_code == 200 and response.json()["stop_reason"] == "subtask_timeout"


async def test_retrying_does_not_restart_the_same_subtask_deadline(session_retry_sleep):
    model = bounded_model(0.35)
    model._verify_result = VerificationResult(False, "missing", "missing")
    model.recover_from_error = AsyncMock(
        return_value=RecoveryPlan(RecoveryStrategy.ALTERNATIVE_APPROACH, [], "inspect again", True)
    )
    calls = 0

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.08)
        action = (
            Action(ActionType.WAIT, {"seconds": 0})
            if calls % 2
            else Action(ActionType.SUBTASK_COMPLETE)
        )
        return ActionDecision(action, "", 1, "inspect")

    model.decide_next_action = decide
    display = FakeDisplayBackend()
    session = TaskSession(llm=model, display=display, decision=None)
    # Keep image generation and disk latency out of the cumulative model-wait case.
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    assert await session.run("inspect") is False
    record = session.snapshot()
    assert record.stop_reason == "subtask_timeout" and record.current_subtask_index == 0
    assert calls < 8 and model.recover_from_error.await_count >= 1


async def test_each_new_subtask_gets_its_own_deadline(tmp_path, session_retry_sleep):
    model = bounded_model(0.5)
    model._decompose_result = DecompositionResult(
        [
            Subtask("s1", "inspect first", timeout_seconds=0.5),
            Subtask("s2", "inspect second", timeout_seconds=0.5),
        ],
        "inspect both",
    )
    seen = set()

    async def verify(*args, **kwargs):
        seen.add(session.loop.context.current_subtask.id)
        await asyncio.sleep(0.3)
        return VerificationResult(True, "visible", "visible")

    model.verify_result = verify
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    assert await session.run("inspect both") is True
    record = session._store.load(session.id)
    assert record.stop_reason is None and record.current_subtask_index == 2 and seen == {"s1", "s2"}
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.parametrize("suppress", ["cancel", "block-loop"])
@pytest.mark.parametrize("phase", ["action", "verification"])
async def test_a_provider_cannot_return_a_usable_action_after_expiry(
    tmp_path, suppress, phase, session_retry_sleep
):
    model = bounded_model(0.15)
    entered = asyncio.Event()

    async def decide(*args, **kwargs):
        entered.set()
        if suppress == "block-loop":
            time.sleep(0.3)
        else:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.3)
        if phase == "verification":
            return VerificationResult(True, "visible", "visible")
        return ActionDecision(Action(ActionType.TYPE, {"text": "late input"}), "", 1, "inspect")

    if phase == "action":
        model.decide_next_action = decide
    else:
        model.verify_result = decide
    display = FakeDisplayBackend()
    session = TaskSession(
        llm=model,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=1),
    )
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    assert await session.run("inspect") is False
    assert (
        entered.is_set()
        and not display.text_inputs
        and session._store.load(session.id).stop_reason == "subtask_timeout"
    )
    assert session._store.load(session.id).current_subtask_index == 0
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.parametrize("outcome", ["operator-stop", "task-deadline", "request-deadline"])
async def test_shorter_deadlines_and_operator_stop_keep_their_original_cause(tmp_path, outcome):
    model = bounded_model(0.3)
    entered = asyncio.Event()

    async def decide(*args, **kwargs):
        entered.set()
        if outcome == "request-deadline":
            from ai_desktop_agent.agent.budget import model_request

            async with model_request():
                await asyncio.Event().wait()
        await asyncio.Event().wait()

    model.decide_next_action = decide
    session = TaskSession(
        llm=model,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(
            max_seconds=0.1 if outcome == "task-deadline" else 2,
            request_timeout_seconds=0.05 if outcome == "request-deadline" else 2,
        ),
    )
    await session.start_async("inspect")
    await asyncio.wait_for(entered.wait(), 1)
    if outcome == "operator-stop":
        session.stop()
        with pytest.raises(asyncio.CancelledError):
            await session._task
    else:
        assert await session._task is False
    reason = {
        "operator-stop": "cancelled",
        "task-deadline": "duration_limit",
        "request-deadline": "request_timeout",
    }[outcome]
    assert session._store.load(session.id).stop_reason == reason
    assert not session.display.is_connected


async def test_expiry_signals_and_drains_an_inflight_display_worker(tmp_path):
    class BlockingDisplay(FakeDisplayBackend):
        def __init__(self):
            super().__init__()
            self.entered, self.stopped, self.finished = (threading.Event() for _ in range(3))

        def type_text(self, text):
            self.entered.set()
            assert self.stopped.wait(1), "deadline did not signal the input worker"
            self.finished.set()

        def request_stop(self):
            self.stopped.set()

        def disconnect(self):
            assert self.finished.is_set()
            super().disconnect()

    model = bounded_model(0.1)
    model._decide_result = ActionDecision(
        Action(ActionType.TYPE, {"text": "input"}), "", 1, "inspect"
    )
    display = BlockingDisplay()
    session = TaskSession(llm=model, display=display, store=TaskStore(tmp_path), decision=None)
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    assert await session.run("inspect") is False
    saved = session._store.load(session.id)
    assert display.entered.is_set() and display.finished.is_set() and not display.is_connected
    assert saved.stop_reason == "subtask_timeout"
    assert len(saved.actions) == 1 and saved.actions[0].success is False
    assert "一部が実行されている可能性" in saved.actions[0].error_message


@pytest.mark.parametrize("phase", ["verification", "recovery"])
async def test_late_optional_decisions_cannot_fall_back_after_expiry(
    tmp_path, phase, session_retry_sleep
):
    model = bounded_model()
    model.verify_result = AsyncMock(return_value=VerificationResult(True, "visible", "visible"))
    model.recover_from_error = AsyncMock()
    entered = asyncio.Event()

    async def late(*args, **kwargs):
        entered.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.4)
        return (
            DecisionVerify(None, 0.5, "partial")
            if phase == "verification"
            else DecisionRecovery("alternative_approach", True, 0.5, escalate=True)
        )

    decision = SimpleNamespace(
        verify_completion=late
        if phase == "verification"
        else AsyncMock(return_value=DecisionVerify(False, 0.99, "none")),
        choose_recovery=late,
    )
    display = FakeDisplayBackend()
    session = TaskSession(llm=model, display=display, store=TaskStore(tmp_path), decision=decision)
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    assert await session.run("inspect") is False
    assert entered.is_set() and not display.text_inputs
    assert session._store.load(session.id).stop_reason == "subtask_timeout"
    model.verify_result.assert_not_awaited()
    model.recover_from_error.assert_not_awaited()
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.parametrize("operator_stop", [False, True])
async def test_later_task_deadline_or_stop_during_drain_cannot_abandon_the_worker(
    tmp_path, monkeypatch, operator_stop
):
    from ai_desktop_agent.agent import budget

    # Startup and disk latency must not spend the deadline before the held input
    # is reached. Advance the loop and budget clocks together at observed gates.
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    monkeypatch.setattr(budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class HeldDisplay(FakeDisplayBackend):
        def __init__(self):
            super().__init__()
            self.entered, self.stopped, self.release, self.finished = (
                threading.Event() for _ in range(4)
            )

        def type_text(self, text):
            self.entered.set()
            assert self.release.wait(10)
            self.finished.set()

        def request_stop(self):
            self.stopped.set()

        def disconnect(self):
            assert self.finished.is_set()
            super().disconnect()

    model = bounded_model(0.1)
    model._decide_result = ActionDecision(
        Action(ActionType.TYPE, {"text": "input"}), "", 1, "inspect"
    )
    display = HeldDisplay()
    session = TaskSession(
        llm=model,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=0.2),
    )
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    try:
        await session.start_async("inspect")
        assert await asyncio.to_thread(display.entered.wait, 3)
        clock[0] += 0.11
        assert await asyncio.to_thread(display.stopped.wait, 3)
        if operator_stop:
            session.stop()
        clock[0] += 0.2
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert session.is_running and not display.finished.is_set()
        display.release.set()
        if operator_stop:
            with pytest.raises(asyncio.CancelledError):
                await session._task
        else:
            assert await session._task is False
        saved = session._store.load(session.id)
        assert saved.stop_reason == ("cancelled" if operator_stop else "subtask_timeout")
        assert display.finished.is_set() and not display.is_connected
        assert len(saved.actions) == 1 and saved.actions[0].success is False
    finally:
        display.release.set()
        if session.is_running:
            session.stop()
        await asyncio.gather(session._task, return_exceptions=True)
