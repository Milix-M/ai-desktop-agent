"""Session stop must survive a provider that suppresses cancellation outside request scopes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits, TaskLimitError
from ai_desktop_agent.agent.decision.types import DecisionRecovery, DecisionVerify
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import session as session_module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient

CASES = [
    ("understanding", "value"),
    ("planning", "value"),
    ("planning", "empty"),
    ("decision", "value"),
    ("decision", "uncancel"),
    ("decision", "restart"),
    ("decision", "manual"),
    ("decision", "error"),
    ("decision", "limit-error"),
    ("decision", "usage-error"),
    ("verification", "value"),
    ("verification", "negative"),
    ("recovery", "value"),
    ("recovery", "give-up"),
    ("replanning", "value"),
    ("replanning", "empty"),
    ("optional-verification", "value"),
    ("optional-recovery", "value"),
    ("zoom", "value"),
]


@pytest.mark.parametrize("phase,outcome", CASES)
async def test_operator_stop_prevents_late_responses_errors_or_actions(
    tmp_path, monkeypatch, phase, outcome, session_retry_sleep
):
    entered = asyncio.Event()
    step = Subtask("s1", "inspect the report", expected_outcome="report visible")
    decomposition = DecompositionResult([step], "inspect")
    model = MockLLMProvider(decompose_result=decomposition)
    model.verify_result = AsyncMock(return_value=VerificationResult(True, "visible", "visible"))
    model.recover_from_error = AsyncMock()
    late_input = ActionDecision(
        Action(ActionType.TYPE, {"text": "must not type"}), "", 1, "inspect"
    )

    async def suppress(value):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if outcome == "uncancel":
                asyncio.current_task().uncancel()
            if outcome == "error":
                raise RuntimeError("late provider failure") from None
            if outcome == "limit-error":
                raise TaskLimitError("request_limit", "late provider limit") from None
            if outcome == "usage-error":
                session._budget.record_usage(None)
            return value

    decision = None
    if phase == "understanding":
        model.understand_instruction = lambda *a, **kw: suppress(
            UnderstandingResult("late", None, [], "late")
        )
    elif phase == "planning":
        model.decompose_task = lambda *a, **kw: suppress(
            DecompositionResult([], "late") if outcome == "empty" else decomposition
        )
    elif phase == "decision":
        action = late_input
        if outcome == "restart":
            action = ActionDecision(
                Action(ActionType.VM_RESTART, {"reason": "late restart"}), "", 1, "restart"
            )
        elif outcome == "manual":
            action = ActionDecision(
                Action(ActionType.REQUEST_USER_INPUT, {"reason": "late input"}),
                "",
                1,
                "authenticate",
            )
        model.decide_next_action = lambda *a, **kw: suppress(action)
    elif phase == "verification":
        model.verify_result = lambda *a, **kw: suppress(
            VerificationResult(outcome != "negative", "late", "late")
        )
    elif phase in {"recovery", "replanning", "optional-recovery"}:
        model.verify_result = AsyncMock(
            return_value=VerificationResult(False, "missing", "missing")
        )
        plan = RecoveryPlan(
            RecoveryStrategy.ALTERNATIVE_APPROACH, [late_input.action], "late recovery", True
        )
        if phase == "recovery":
            if outcome == "give-up":
                plan = RecoveryPlan(RecoveryStrategy.GIVE_UP, [], "late give-up", False)
            model.recover_from_error = lambda *a, **kw: suppress(plan)
        elif phase == "replanning":
            model.recover_from_error = AsyncMock(
                return_value=RecoveryPlan(RecoveryStrategy.REPLAN_SUBTASK, [], "replan", True)
            )
            count = 0

            async def decompose(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    return decomposition
                return await suppress(
                    DecompositionResult([], "late")
                    if outcome == "empty"
                    else DecompositionResult([Subtask("new", "late plan")], "late")
                )

            model.decompose_task = decompose
        else:
            decision = SimpleNamespace(
                verify_completion=AsyncMock(return_value=DecisionVerify(False, 0.99, "none")),
                choose_recovery=lambda *a, **kw: suppress(
                    DecisionRecovery("alternative_approach", True, 0.5, escalate=True)
                ),
            )
    elif phase == "optional-verification":
        decision = SimpleNamespace(
            verify_completion=lambda *a, **kw: suppress(DecisionVerify(None, 0.5, "partial"))
        )
    elif phase == "zoom":
        count = 0

        async def zoom(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return ActionDecision(
                    Action(ActionType.REGION_SELECT, {"x": 0, "y": 0, "width": 100, "height": 100}),
                    "",
                    1,
                    "refine",
                )
            return await suppress(late_input)

        model.decide_next_action = zoom

    display = FakeDisplayBackend()
    # A failed backend stop cannot authorize a new operation either.
    display.request_stop = Mock(side_effect=OSError("stop transport failed"))
    session = TaskSession(
        llm=model,
        display=display,
        store=TaskStore(tmp_path),
        decision=decision,
        limits=ExecutionLimits(max_seconds=1),
    )
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    session.allow_vm_restart = True
    restart = Mock(return_value=SimpleNamespace(id="desktop"))

    class Pool:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        restart_vm = staticmethod(restart)

    monkeypatch.setattr("ai_desktop_agent.server.vm_pool.VmPool", Pool)
    monkeypatch.setattr(session_module, "RESTART_SETTLE_SECONDS", 0)
    completed = []
    session.on_complete(completed.append)
    try:
        await session.start_async("inspect the report")
        await asyncio.wait_for(entered.wait(), 2)
        session.stop()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        saved = TaskStore(tmp_path).load(session.id)
        assert (
            saved.state == "failed" and saved.success is False and saved.stop_reason == "cancelled"
        )
        assert saved.error_message and saved.current_subtask_index == 0
        assert not display.text_inputs and not display.is_connected
        restart.assert_not_called()
        assert completed == [False] and session.manual_input is None
        assert all(action.action_type == "subtask_complete" for action in saved.actions)
        assert all(subtask["id"] != "new" for subtask in saved.subtasks)
        if phase == "optional-verification":
            model.verify_result.assert_not_awaited()
        if phase == "optional-recovery":
            model.recover_from_error.assert_not_awaited()
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            result = await client.get(f"/tasks/{session.id}")
        assert result.status_code == 200 and result.json()["stop_reason"] == "cancelled"
    finally:
        if session.is_running:
            session.stop()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_a_stopped_session_does_not_stop_an_independent_session(
    tmp_path, session_retry_sleep
):
    entered = asyncio.Event()
    model = MockLLMProvider()

    async def ignore(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ActionDecision(Action(ActionType.TYPE, {"text": "late"}), "", 1, "inspect")

    model.decide_next_action = ignore
    stopped = TaskSession(
        llm=model,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path / "stopped"),
        decision=None,
        limits=ExecutionLimits(max_seconds=1),
    )
    normal = TaskSession(
        llm=MockLLMProvider(),
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path / "normal"),
        decision=None,
    )
    stopped._capture_screenshot = AsyncMock(return_value=stopped.display.capture_screen())
    normal._capture_screenshot = AsyncMock(return_value=normal.display.capture_screen())
    try:
        await stopped.start_async("inspect")
        await asyncio.wait_for(entered.wait(), 2)
        stopped.stop()
        with pytest.raises(asyncio.CancelledError):
            await stopped._task
        assert await normal.run("inspect independently") is True
        assert normal._store.load(normal.id).stop_reason is None
        stopped.stop()  # A completed owner cannot affect another session.
        assert not stopped.display.text_inputs
    finally:
        if stopped.is_running:
            stopped.stop()
        await asyncio.gather(stopped._task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["execution", "limit"])
async def test_stop_during_error_notification_cannot_leave_a_running_history_record(
    tmp_path, failure
):
    model = MockLLMProvider()
    entered = asyncio.Event()

    async def fail(*args, **kwargs):
        if failure == "limit":
            session._budget.fail("request_limit", "original quota failure")
        raise RuntimeError("original provider failure")

    model.understand_instruction = fail
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    before_error, completed = [], []

    async def error(message):
        before_error.append(TaskStore(tmp_path).load(session.id))
        entered.set()
        await asyncio.Event().wait()

    session.on_error(error)
    session.on_complete(
        lambda success: completed.append((success, TaskStore(tmp_path).load(session.id)))
    )
    try:
        await session.start_async("inspect")
        await asyncio.wait_for(entered.wait(), 2)
        session.stop()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        saved = TaskStore(tmp_path).load(session.id)
        reason = "execution_error" if failure == "execution" else "request_limit"
        assert saved.state == "failed" and saved.success is False and saved.stop_reason == reason
        assert len(before_error) == 1 and before_error[0].state == "failed"
        assert before_error[0].stop_reason == reason and "original" in before_error[0].error_message
        assert (
            len(completed) == 1 and completed[0][0] is False and completed[0][1].state == "failed"
        )
        assert not session.display.is_connected
    finally:
        if session.is_running:
            session.stop()
        await asyncio.gather(session._task, return_exceptions=True)
