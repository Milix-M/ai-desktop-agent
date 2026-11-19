"""An elapsed request limit applies even when its timer could not run on time."""

import asyncio
import contextlib
import json
import time

import httpx
import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits, TaskBudget, TaskLimitError, model_request
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient
from tests.test_model_metadata import metadata_reply
from tests.test_task_budget import completion, llm_with_transport

PHASES = {
    "understand_instruction": "understand.valid",
    "decompose_task": "plan.valid",
    "action_decision": "action.valid",
    "verify_result": "verify.valid",
    "recover_from_error": "recover.valid",
}


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("response", ["success", "http-error"])
async def test_elapsed_request_cannot_authorize_input_completion_or_retry(
    tmp_path, monkeypatch, phase, response, session_retry_sleep, provider_retry_sleep
):
    calls, actions, recoveries = [], 0, 0

    def respond(request):
        nonlocal actions, recoveries
        body = json.loads(request.content)
        if "response_format" not in body:
            return completion("{}")  # SDK initialization outside the task's request budget.
        stage = body["response_format"]["json_schema"]["name"]
        calls.append(stage)
        if stage == "action_decision":
            actions += 1
        if stage == "recover_from_error":
            recoveries += 1
        if stage == phase:
            time.sleep(0.4)  # The event loop cannot deliver its timeout callback.
            if response == "http-error":
                return httpx.Response(503, json={"error": {"message": "local-test-key rejected"}})
        data = metadata_reply(stage, PHASES[phase], "forbidden input", action_number=actions or 1)
        if stage == "verify_result" and recoveries:
            data["success"] = True
        return completion(json.dumps(data))

    provider = await llm_with_transport(respond)
    await provider._call("initialize SDK")
    display = FakeDisplayBackend()
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=provider,
        display=display,
        store=store,
        decision=None,
        limits=ExecutionLimits(max_seconds=15, max_requests=25, request_timeout_seconds=0.2),
    )
    assert await session.run("inspect the report without entering text") is False
    assert display.text_inputs == [] and not display.is_connected
    saved = TaskStore(tmp_path).load(session.id)
    assert (
        saved.state == "failed"
        and saved.success is False
        and saved.stop_reason == "request_timeout"
    )
    assert saved.error_message and "local-test-key" not in saved.error_message
    assert saved.current_subtask_index == 0 and all(
        a.action_type == "subtask_complete" for a in saved.actions
    )
    assert calls.count(phase) == 1 and saved.execution_usage["requests"] == len(calls)
    reported = len(calls) - (response == "http-error")
    assert saved.execution_usage["input_tokens"] == 4 * reported
    assert saved.execution_usage["output_tokens"] == reported
    assert saved.execution_usage["usage_complete"] is (response == "success")
    provider_retry_sleep.assert_not_awaited()
    assert asyncio.current_task().cancelling() == 0
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        result = await client.get(f"/tasks/{session.id}")
    assert result.status_code == 200 and result.json()["stop_reason"] == "request_timeout"


async def test_a_suppressed_request_cancellation_still_stops_and_retains_reported_usage():
    budget = TaskBudget(ExecutionLimits(max_seconds=2, request_timeout_seconds=0.03))
    with pytest.raises(TaskLimitError) as error:
        async with budget.activate(lambda: None):
            async with model_request() as request:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.1)
                request.record_usage({"prompt_tokens": 4, "completion_tokens": 1})
    assert error.value.reason == "request_timeout" and budget.requests == 1
    assert budget.snapshot()["input_tokens"] == 4 and budget.usage_complete
    assert asyncio.current_task().cancelling() == 0


async def test_an_inner_timeout_error_keeps_its_own_identity_and_is_not_a_request_limit():
    budget = TaskBudget(ExecutionLimits(request_timeout_seconds=1))
    original = TimeoutError("inner operation failed")
    async with budget.activate(lambda: None):
        with pytest.raises(TimeoutError) as caught:
            async with model_request():
                raise original
    assert caught.value is original and budget.stop_reason is None
    assert budget.requests == 1 and not budget.usage_complete
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.parametrize("after_request_timeout", [False, True])
async def test_suppressed_cancellation_cannot_consume_an_operator_stop(
    tmp_path, after_request_timeout
):
    entered, caught_cancel = asyncio.Event(), asyncio.Event()
    model = MockLLMProvider()

    async def decide(*args, **kwargs):
        async with model_request():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                caught_cancel.set()
                if after_request_timeout:
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.Event().wait()
            return ActionDecision(Action(ActionType.TYPE, {"text": "forbidden"}), "", 1, "inspect")

    model.decide_next_action = decide
    display = FakeDisplayBackend()
    session = TaskSession(
        llm=model,
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(
            max_seconds=1, request_timeout_seconds=0.08 if after_request_timeout else 0.5
        ),
    )
    try:
        await session.start_async("inspect")
        await asyncio.wait_for(entered.wait(), 1)
        if after_request_timeout:
            await asyncio.wait_for(caught_cancel.wait(), 1)
        session.stop()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        saved = session._store.load(session.id)
        assert saved.stop_reason == "cancelled" and not saved.actions
        assert not display.text_inputs and not display.is_connected
    finally:
        if session.is_running:
            session.stop()
        await asyncio.gather(session._task, return_exceptions=True)
