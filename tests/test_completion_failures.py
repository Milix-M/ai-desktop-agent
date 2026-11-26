"""Completion-service failures stop once; valid non-achievement still recovers."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.privacy import protect, unprotect
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient
from tests.test_task_budget import completion, llm_with_transport


def script_pre_verification(provider):
    step = Subtask(
        "s1",
        "inspect the visible result",
        expected_outcome="result visible",
        max_retries=1,
    )
    provider.initial_plan = AsyncMock(
        return_value=(
            UnderstandingResult("inspect", None, [], "inspect"),
            DecompositionResult([step], "inspect"),
        )
    )
    provider.decide_next_action = AsyncMock(
        return_value=ActionDecision(
            Action(ActionType.SUBTASK_COMPLETE), "result inspected", 1, "visible result"
        )
    )
    provider.recover_from_error = AsyncMock(
        return_value=RecoveryPlan(RecoveryStrategy.ALTERNATIVE_APPROACH, [], "inspect again", True)
    )


@pytest.mark.parametrize(
    "response,expected",
    [
        (400, 1),
        (401, 1),
        (403, 1),
        (404, 1),
        (422, 1),
        (429, 3),
        (503, 3),
        ("malformed", 3),
        ("invalid-success", 1),
    ],
)
async def test_verification_failure_is_not_restarted_after_provider_exhaustion_and_is_persisted(
    tmp_path, response, expected, provider_retry_sleep, session_retry_sleep, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if response == "malformed":
            return completion('[false,{"success":true}]')
        if response == "invalid-success":
            return completion('{"success":"false","reasoning":"invalid flag","evidence":""}')
        return httpx.Response(
            response,
            json={
                "error": {
                    "message": "verification rejected local-test-key",
                    "code": "invalid_request",
                }
            },
        )

    provider = await llm_with_transport(respond)
    script_pre_verification(provider)
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=provider,
        display=FakeDisplayBackend(),
        store=store,
        decision=None,
        limits=ExecutionLimits(max_requests=8),
    )
    completions, errors = [], []
    session.on_complete(lambda success: completions.append((success, store.load(session.id))))
    session.on_error(errors.append)
    assert await session.run("inspect without changing the desktop") is False
    assert len(requests) == provider.call_count == expected
    assert provider.recover_from_error.await_count == 0
    record = store.load(session.id)
    assert record.state == "failed" and record.success is False
    assert record.stop_reason == "execution_error" and record.error_message
    assert "local-test-key" not in record.error_message
    assert record.execution_usage["requests"] == expected
    assert len(record.actions) == 1 and record.actions[0].action_type == "subtask_complete"
    assert session._verify_failures == {}  # Service failure is not a valid negative judgment.
    assert errors == [record.error_message]
    assert len(completions) == 1 and completions[0][1].error_message == record.error_message
    assert not session.display.is_connected
    assert [call.args for call in provider_retry_sleep.await_args_list] == (
        [(1.5,), (3.0,)] if expected == 3 else []
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        detail = (await client.get(f"/tasks/{session.id}")).json()
    assert (
        detail["stop_reason"] == "execution_error"
        and detail["error_message"] == record.error_message
    )


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("eventual_success", [False, True])
async def test_a_valid_negative_verdict_retains_bounded_recovery_and_can_later_complete(
    tmp_path, eventual_success, hybrid, session_retry_sleep
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return completion(
            json.dumps(
                {
                    "success": eventual_success and len(requests) > 1,
                    "reasoning": "screen inspected",
                    "evidence": "visible outcome",
                }
            )
        )

    provider = await llm_with_transport(respond)
    script_pre_verification(provider)
    store = TaskStore(tmp_path)
    session = TaskSession(llm=provider, display=FakeDisplayBackend(), store=store, decision=None)
    if hybrid:
        session.executor.cua = SimpleNamespace(
            disabled=False,
            observe=AsyncMock(return_value=[]),
            invalidate=lambda: None,
            aclose=AsyncMock(),
        )
    assert await session.run("inspect the visible result") is eventual_success
    assert provider.recover_from_error.await_count == (0 if hybrid else 1) and len(requests) == 2
    record = store.load(session.id)
    assert record.state == ("completed" if eventual_success else "failed")
    assert record.stop_reason == (None if eventual_success else "verification_failed")
    if not eventual_success:
        assert "screen inspected" in record.error_message
        assert "visible outcome" in record.error_message
    assert session._verify_failures == {"s1": 1 if eventual_success else 2}


@pytest.mark.parametrize(
    "phase",
    [
        "understand_instruction",
        "decompose_task",
        "decide_next_action",
        "verify_result",
        "recover_from_error",
    ],
)
async def test_unhandled_phase_errors_keep_their_redacted_cause_in_durable_history(
    tmp_path, phase, session_retry_sleep
):
    secret = "execution-failure-sensitive-value"
    protect(secret)
    provider = MockLLMProvider(verify_result=VerificationResult(False, "not ready", "absent"))
    setattr(provider, phase, AsyncMock(side_effect=RuntimeError("provider failed: " + secret)))
    session = TaskSession(
        llm=provider, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    try:
        assert await session.run("inspect the visible result") is False
        record = session._store.load(session.id)
        assert record.stop_reason == "execution_error"
        assert record.error_message == "provider failed: [REDACTED]"
        assert session.snapshot().error_message == record.error_message
        assert not session.display.is_connected
    finally:
        unprotect(secret)


async def test_failed_verification_capture_stops_without_another_decision_or_model_judgment(
    tmp_path, session_retry_sleep
):
    provider = MockLLMProvider()
    provider.verify_result = AsyncMock()
    display = FakeDisplayBackend()
    capture = display.capture_screen
    session = TaskSession(llm=provider, display=display, store=TaskStore(tmp_path), decision=None)
    failed_captures = []

    def screen(**kwargs):
        if session.loop.state == AgentState.VERIFYING:
            failed_captures.append(True)
            raise OSError("verification screen unavailable")
        return capture(**kwargs)

    display.capture_screen = screen
    assert await session.run("inspect") is False
    assert failed_captures == [True]
    provider.verify_result.assert_not_awaited()
    assert session._store.load(session.id).error_message == "verification screen unavailable"


@pytest.mark.parametrize("primary_fails", [False, True])
@pytest.mark.parametrize("optional_outcome", ["error", "uncertain"])
async def test_optional_decision_failure_still_falls_back_once_and_does_not_restart_a_failed_llm(
    tmp_path, primary_fails, optional_outcome, provider_retry_sleep, session_retry_sleep
):
    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(401, json={"error": {"message": "invalid key"}})
            if primary_fails
            else completion('{"success":true,"reasoning":"visible","evidence":"result"}')
        )

    provider = await llm_with_transport(respond)
    script_pre_verification(provider)
    optional = SimpleNamespace(
        verify_completion=AsyncMock(
            side_effect=RuntimeError("optional service unavailable")
            if optional_outcome == "error"
            else None,
            return_value=SimpleNamespace(
                success=None, prob=0.5, grade="partial", reasoning="uncertain"
            ),
        ),
        aclose=AsyncMock(),
    )
    session = TaskSession(
        llm=provider,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=optional,
        limits=ExecutionLimits(max_requests=3),
    )
    assert await session.run("inspect") is not primary_fails
    assert len(requests) == optional.verify_completion.await_count == 1
    assert session.decision_calls == session.decision_escalations == 1
    optional.aclose.assert_awaited_once()


async def test_cancelled_verification_is_not_reclassified_as_a_provider_failure(
    tmp_path, session_retry_sleep
):
    provider = MockLLMProvider()
    entered = asyncio.Event()

    async def verify(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    provider.verify_result = verify
    session = TaskSession(
        llm=provider, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    await session.start_async("inspect")
    await asyncio.wait_for(entered.wait(), 2)
    session.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(session._task, 2)
    record = session._store.load(session.id)
    assert record.state == "failed" and record.stop_reason != "execution_error"


@pytest.mark.parametrize("source", ["optional", "primary"])
async def test_verification_limits_never_fall_back_or_become_execution_errors(
    tmp_path, source, session_retry_sleep
):
    provider = MockLLMProvider()
    provider.verify_result = AsyncMock(return_value=VerificationResult(True, "visible", "result"))
    optional = SimpleNamespace(verify_completion=AsyncMock(), aclose=AsyncMock())
    session = TaskSession(
        llm=provider,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=optional if source == "optional" else None,
    )

    async def limited(*args, **kwargs):
        session._budget.fail("request_limit", "verification request budget exhausted")

    if source == "optional":
        optional.verify_completion.side_effect = limited
    else:
        provider.verify_result.side_effect = limited
    assert await session.run("inspect") is False
    record = session._store.load(session.id)
    assert record.stop_reason == "request_limit"
    assert record.error_message == "verification request budget exhausted"
    assert session.decision_escalations == 0
    assert provider.verify_result.await_count == (source == "primary")


async def test_an_exception_without_text_still_leaves_a_visible_failure_reason(tmp_path):
    provider = MockLLMProvider()
    provider.understand_instruction = AsyncMock(side_effect=RuntimeError())
    session = TaskSession(
        llm=provider, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    assert await session.run("inspect") is False
    record = session._store.load(session.id)
    assert record.stop_reason == "execution_error" and record.error_message


async def test_verification_request_timeout_stops_the_sdk_call_without_restarting_the_task(
    tmp_path, session_retry_sleep
):
    calls = []

    async def stalled(request):
        calls.append(request)
        await asyncio.Event().wait()

    provider = await llm_with_transport(stalled)
    script_pre_verification(provider)
    session = TaskSession(
        llm=provider,
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(request_timeout_seconds=0.02),
    )
    assert await session.run("inspect") is False
    record = session._store.load(session.id)
    assert len(calls) == 1 and record.stop_reason == "request_timeout"
    assert record.error_message and provider.recover_from_error.await_count == 0


def test_successful_background_actions_do_not_require_a_repaint(tmp_path):
    import hashlib

    from ai_desktop_agent.agent.state import ActionRecord
    from ai_desktop_agent.vm.screenshot import Screenshot

    session = TaskSession(
        llm=MockLLMProvider(),
        display=FakeDisplayBackend(),
        store=TaskStore(tmp_path),
        decision=None,
    )
    image = Screenshot(b"same frame", 20, 20)
    session._prev_turn_hash = hashlib.sha256(image.image_bytes).hexdigest()
    session.loop.context.action_history.append(
        ActionRecord(Action(ActionType.NOTE_READ, {"title": "progress"}), True)
    )
    assert not session._is_screen_unchanged(image)
    session.loop.context.action_history.append(
        ActionRecord(Action(ActionType.LEFT_CLICK, {"x": 4, "y": 4}), True)
    )
    assert session._is_screen_unchanged(image)


async def test_invalid_workspace_batch_is_corrected_before_dispatch(tmp_path):
    from ai_desktop_agent.actions.primitives import WorkspaceBatchError
    from ai_desktop_agent.agent.state import Goal
    from ai_desktop_agent.vm.screenshot import Screenshot

    provider = MockLLMProvider()
    provider.decide_next_action = AsyncMock(
        side_effect=[
            WorkspaceBatchError("duplicate source"),
            ActionDecision(Action(ActionType.WAIT, {"seconds": 0}), "", 1.0, ""),
        ]
    )
    session = TaskSession(
        llm=provider, display=FakeDisplayBackend(), store=TaskStore(tmp_path), decision=None
    )
    goal = Goal(description="File the documents")
    session.loop.start(goal)
    result = await session._decide_action(Subtask("s1", "inspect"), Screenshot(b"frame", 20, 20))
    assert result.action.action_type == ActionType.WAIT
    assert (
        "duplicate source" in provider.decide_next_action.await_args.kwargs["goal"].constraints[-1]
    )
    assert not goal.constraints
    assert not session.loop.context.action_history
