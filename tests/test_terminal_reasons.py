"""Controlled failures survive reload and are durable before terminal events."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.privacy import protect, unprotect
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import session as session_module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("empty-plan", "planning_failed"),
        ("unmet", "verification_failed"),
        ("give-up", "recovery_failed"),
        ("unrecoverable", "recovery_failed"),
        ("retry-limit", "retry_limit"),
        ("replan-empty", "replanning_failed"),
        ("replan-limit", "replan_limit"),
        ("action-limit", "action_limit"),
        ("recovery-action-limit", "action_limit"),
    ],
)
async def test_terminal_reason_survives_reloading_history_and_precedes_notifications(
    tmp_path, monkeypatch, outcome, code, session_retry_sleep
):
    secret = "terminal-reason-private-value"
    protect(secret)
    subtask = Subtask(
        "s1",
        "inspect the report",
        expected_outcome="report visible",
        max_retries=0 if outcome in {"unmet", "retry-limit"} else 3,
    )
    model = MockLLMProvider(
        decompose_result=DecompositionResult([subtask], "inspect"),
        verify_result=VerificationResult(False, "report unavailable " + secret, "report missing"),
        recover_result=RecoveryPlan(
            RecoveryStrategy.GIVE_UP, [], "permission unavailable " + secret, False
        ),
    )
    if outcome == "empty-plan":
        model.decompose_task = AsyncMock(
            return_value=DecompositionResult([], "no valid steps " + secret)
        )
    if outcome == "unrecoverable":
        model._recover_result = RecoveryPlan(
            RecoveryStrategy.ALTERNATIVE_APPROACH, [], "permission unavailable " + secret, False
        )
    mechanical_failure = outcome in {
        "retry-limit",
        "replan-empty",
        "replan-limit",
        "recovery-action-limit",
    }
    if mechanical_failure:
        model._decide_result = ActionDecision(
            Action(ActionType.KEY_PRESS, {"key": "escape"}), "dismiss", 1, "dismiss dialog"
        )
    if outcome in {"replan-empty", "replan-limit"}:
        model._recover_result = RecoveryPlan(
            RecoveryStrategy.REPLAN_SUBTASK,
            [Action(ActionType.TYPE, {"text": "must not type"})]
            if outcome == "replan-limit"
            else [],
            "different plan " + secret,
            True,
        )
    if outcome == "replan-empty":
        model.decompose_task = AsyncMock(
            side_effect=[
                DecompositionResult([subtask], "inspect"),
                DecompositionResult([], "no replacement steps " + secret),
            ]
        )
    if outcome == "recovery-action-limit":
        model._recover_result = RecoveryPlan(
            RecoveryStrategy.ALTERNATIVE_APPROACH,
            [Action(ActionType.TYPE, {"text": "must not type"})],
            "dismiss dialog",
            True,
        )
    display = FakeDisplayBackend()
    if mechanical_failure:
        display.key_press = Mock(side_effect=OSError("dialog unavailable " + secret))
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    if outcome == "replan-limit":
        session._replan_count = 3
    if outcome == "action-limit":
        monkeypatch.setattr(session_module, "MAX_ACTIONS_PER_TASK", 0)
    if outcome == "recovery-action-limit":
        monkeypatch.setattr(session_module, "MAX_ACTIONS_PER_TASK", 1)
    terminal, errors, completions = [], [], []

    def saved():
        return TaskStore(tmp_path).load(session.id)

    def state_changed(state, context):
        if state == AgentState.FAILED:
            terminal.append(saved())

    session.on_state_change(state_changed)
    session.on_error(lambda message: errors.append((message, saved())))
    session.on_complete(lambda success: completions.append((success, saved())))
    try:
        assert await session.run("inspect the report") is False
        record = saved()
        assert record.state == "failed" and record.success is False
        assert record.stop_reason == code and record.error_message
        assert secret not in record.error_message
        if outcome not in {"action-limit", "recovery-action-limit", "replan-limit"}:
            assert "[REDACTED]" in record.error_message
        assert len(terminal) == 1 and terminal[0].error_message == record.error_message
        assert len(errors) == 1 and errors[0][0] == record.error_message
        assert errors[0][1].stop_reason == code
        assert errors[0][1].error_message == record.error_message
        assert len(completions) == 1 and completions[0][0] is False
        assert completions[0][1].error_message == record.error_message
        assert display.text_inputs == [] and not display.is_connected
        monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
        monkeypatch.setattr(server, "_sessions", {})
        monkeypatch.setattr(server, "_active_session", None)
        async with AuthenticatedClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            detail = (await client.get(f"/tasks/{session.id}")).json()
        assert detail["stop_reason"] == code and detail["error_message"] == record.error_message
    finally:
        unprotect(secret)


async def test_an_unhandled_active_state_is_not_persisted_as_a_running_failed_task(tmp_path):
    session = TaskSession(
        llm=MockLLMProvider(),
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=None,
    )

    async def incomplete_cycle():
        session.loop.action_executed()  # A broken cycle returns in WAITING.

    session._execute_phase = incomplete_cycle
    assert await session.run("inspect") is False
    record = session._store.load(session.id)
    assert record.state == "failed" and record.stop_reason == "invalid_state"
    assert record.error_message and not session.display.is_connected


@pytest.mark.parametrize("stage", ["execute", "verify", "recover"])
async def test_missing_subtask_fails_durably_before_any_desktop_or_model_action(tmp_path, stage):
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock()
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    session.loop.start(Goal("inspect"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s1", "inspect")])
    session.loop.context.advance_subtask()
    try:
        if stage == "execute":
            await session._execute_phase()
        else:
            session.loop.record_action(Action(ActionType.SUBTASK_COMPLETE), True)
            session.loop.action_executed()
            session.loop.wait_complete()
            if stage == "verify":
                await session._verify_phase()
            else:
                session.loop.verify_failed()
                await session._recover_phase()
        record = session._store.load(session.id)
        assert record.state == "failed" and record.stop_reason == "invalid_state"
        model.decide_next_action.assert_not_awaited()
        assert session.display.clicks == []
    finally:
        await session.aclose()


async def test_late_cancellation_preserves_an_already_durable_business_failure(tmp_path):
    model = MockLLMProvider(decompose_result=DecompositionResult([], "no valid steps"))
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    entered = asyncio.Event()

    async def notify(state, context):
        if state == AgentState.FAILED and not entered.is_set():
            entered.set()
            await asyncio.Event().wait()

    session.on_state_change(notify)
    await session.start_async("inspect")
    await asyncio.wait_for(entered.wait(), 2)
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(session._task, 2)
    record = session._store.load(session.id)
    assert record.stop_reason == "planning_failed" and "no valid steps" in record.error_message


@pytest.mark.parametrize("before_start", [False, True])
async def test_operator_stop_has_a_durable_reason_even_before_the_task_starts(
    tmp_path, before_start
):
    entered = asyncio.Event()
    model = MockLLMProvider()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    model.understand_instruction = wait
    session = TaskSession(
        llm=model, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    if before_start:
        task = session._task = asyncio.create_task(session.run("inspect"))
        task.add_done_callback(session._ensure_closed)
        task.cancel()
    else:
        await session.start_async("inspect")
        await asyncio.wait_for(entered.wait(), 2)
        task = session._task
        session.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    if before_start:
        await asyncio.sleep(0)
        await session._cleanup_task
    record = session._store.load(session.id)
    assert record.state == "failed" and record.stop_reason == "cancelled" and record.error_message
