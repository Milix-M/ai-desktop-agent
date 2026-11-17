import asyncio
import threading
import time
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import AsyncOpenAI

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits, TaskBudget, TaskLimitError
from ai_desktop_agent.agent.decision.provider import OpenRouterClefProvider, WorkersAiClefProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def completion(content="{}", usage=True):
    result = {
        "id": "budget-test",
        "object": "chat.completion",
        "created": 1,
        "model": "local-test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage:
        result["usage"] = {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
    return httpx.Response(200, json=result)


async def llm_with_transport(handler):
    provider = OpenAICompatProvider(api_key="local-test-key")
    await provider._client.close()
    provider._client = AsyncOpenAI(
        api_key="local-test-key",
        base_url="http://model.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


@pytest.mark.parametrize(
    "setting,value",
    [
        ("TASK_MAX_SECONDS", "0"),
        ("TASK_MAX_SECONDS", "nan"),
        ("TASK_REQUEST_TIMEOUT_SECONDS", "inf"),
        ("TASK_MAX_REQUESTS", "-1"),
        ("TASK_MAX_REQUESTS", "1.5"),
        ("TASK_MAX_TOKENS", ""),
        ("TASK_MAX_COST_USD", "nan"),
        ("TASK_MAX_COST_USD", "1"),
        ("TASK_INPUT_USD_PER_MILLION", "1"),
    ],
)
def test_invalid_or_incomplete_settings_fail_before_any_task(setting, value, monkeypatch):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        ExecutionLimits.from_env()


async def test_bad_limits_stop_startup_before_docker_or_storage_mutation(monkeypatch):
    from ai_desktop_agent.server import app as server

    monkeypatch.setenv("TASK_MAX_SECONDS", "nan")
    pool, store = Mock(), Mock()
    monkeypatch.setattr(server, "get_pool", pool)
    monkeypatch.setattr(server, "get_store", store)
    with pytest.raises(ValueError):
        async with server.lifespan(server.app):
            pytest.fail("invalid settings started the application")
    pool.assert_not_called()
    store.assert_not_called()


@pytest.mark.parametrize(
    "reason", ["token_limit", "cost_limit", "request_limit", "request_timeout", "usage_unavailable"]
)
async def test_model_limits_stop_before_gui_and_survive_history_reload(
    tmp_path, reason, monkeypatch
):
    sent = []

    async def handle(request):
        sent.append(request)
        if reason == "request_timeout":
            await asyncio.Event().wait()
        if reason == "request_limit":
            return httpx.Response(500, json={"error": {"message": "temporary failure"}})
        return completion(usage=reason != "usage_unavailable")

    limits = ExecutionLimits(
        max_seconds=5,
        request_timeout_seconds=0.03 if reason == "request_timeout" else 2,
        max_requests=1 if reason == "request_limit" else 5,
        max_tokens=5 if reason == "token_limit" else 100,
        max_cost_usd=Decimal("0.000005") if reason == "cost_limit" else None,
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("1"),
    )
    store = TaskStore(tmp_path)
    llm = await llm_with_transport(handle)
    display = FakeDisplayBackend()
    session = TaskSession(llm=llm, display=display, store=store, limits=limits, decision=None)
    complete = Mock()
    session.on_complete(complete)
    assert not await session.run("文書を保存する")
    record = TaskStore(tmp_path).load(session.id)
    assert record.state == "failed" and record.success is False
    assert record.stop_reason == reason and record.error_message
    assert record.execution_limits["max_requests"] == limits.max_requests
    assert record.execution_usage["requests"] == 1
    assert len(sent) == 1 and not record.actions
    assert not display.is_connected and llm._client.is_closed()
    complete.assert_called_once_with(False)
    from ai_desktop_agent.server import app as server
    from tests.http import AuthenticatedClient

    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        detail = (await api.get("/tasks/" + session.id)).json()
        assert detail["stop_reason"] == reason
        assert detail["execution_usage"] == record.execution_usage
        assert detail["error_message"] == record.error_message


async def test_json_retry_is_charged_before_another_request_can_start(provider_retry_sleep):
    sent = []

    def handle(request):
        sent.append(request)
        return completion(content="invalid JSON")

    llm = await llm_with_transport(handle)
    budget = TaskBudget(ExecutionLimits(max_tokens=8))
    try:
        with pytest.raises(TaskLimitError) as error:
            async with budget.activate(lambda: None):
                await llm._call("文書を確認する")
        assert error.value.reason == "token_limit"
        assert len(sent) == budget.requests == 2
        assert budget.input_tokens + budget.output_tokens == 10
        assert llm.total_tokens == 10
        provider_retry_sleep.assert_awaited_once_with(1.5)
    finally:
        await llm.aclose()


@pytest.mark.parametrize("provider_kind", ["workers", "openrouter"])
@pytest.mark.parametrize("phase", ["verify", "recover"])
@pytest.mark.parametrize("failure", ["tokens", "missing", "json"])
async def test_decision_limit_is_shared_and_never_falls_back_to_llm(
    tmp_path, monkeypatch, provider_kind, phase, failure
):
    real_client = httpx.AsyncClient
    seen = []

    def handle(request):
        seen.append(request)
        if failure == "json":
            return httpx.Response(200, text="invalid JSON")
        data = {"answers": {}, "usage": {"input_tokens": 5, "output_tokens": 0}}
        if failure == "missing":
            data.pop("usage")
        return httpx.Response(200, json={"result": data} if provider_kind == "workers" else data)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    decision = (
        WorkersAiClefProvider(account_id="local", api_token="local")
        if provider_kind == "workers"
        else OpenRouterClefProvider(api_key="local")
    )
    llm = MockLLMProvider()
    llm.verify_result = AsyncMock()
    llm.recover_from_error = AsyncMock()
    session = TaskSession(
        llm=llm,
        display=FakeDisplayBackend(),
        decision=decision,
        store=TaskStore(tmp_path),
        limits=ExecutionLimits(max_tokens=5),
    )

    async def run_phase(goal):
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask(id="save", description="文書を保存する")])
        action = Action(ActionType.SUBTASK_COMPLETE)
        session.loop.record_action(action, phase == "verify")
        session.loop.action_executed()
        session.loop.wait_complete()
        if phase == "verify":
            await session._verify_phase()
        else:
            session.loop.verify_failed()
            session._pending_error = ErrorContext(action, "保存を確認できない", 0)
            await session._recover_phase()

    session._run_task = run_phase
    assert not await session.run("文書を保存する")
    assert session.loop.state == AgentState.FAILED
    assert session._budget.snapshot()["input_tokens"] == (5 if failure == "tokens" else 0)
    assert len(seen) == 1
    llm.verify_result.assert_not_awaited()
    llm.recover_from_error.assert_not_awaited()
    assert session._store.load(session.id).stop_reason == (
        "token_limit" if failure == "tokens" else "usage_unavailable"
    )


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 4},
        {"prompt_tokens": -1, "completion_tokens": 0},
        {"prompt_tokens": True, "completion_tokens": 0},
    ],
)
def test_unknown_usage_cannot_be_treated_as_free(usage):
    budget = TaskBudget(ExecutionLimits())
    with pytest.raises(TaskLimitError) as error:
        budget.record_usage(usage)
    assert error.value.reason == "usage_unavailable"
    assert budget.snapshot()["usage_complete"] is False


async def test_pause_counts_toward_deadline_and_notifies_failure(tmp_path):
    display = FakeDisplayBackend()
    display.request_stop = Mock()
    session = TaskSession(
        llm=MockLLMProvider(),
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=0.04),
    )
    complete = Mock()
    session.on_complete(complete)

    async def paused():
        session.pause()
        await session._wait_if_paused()
        pytest.fail("expired task resumed")

    session._execute_phase = paused
    assert not await session.run("保存を待つ")
    assert asyncio.current_task().cancelling() == 0
    display.request_stop.assert_called_once()
    assert session._store.load(session.id).stop_reason == "duration_limit"
    complete.assert_called_once_with(False)


@pytest.mark.parametrize("phase", ["execute", "recover"])
async def test_deadline_signals_and_drains_display_before_cleanup(tmp_path, phase):
    class BlockingDisplay(FakeDisplayBackend):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.stopped = threading.Event()
            self.finished = threading.Event()

        def type_text(self, text):
            self.entered.set()
            assert self.stopped.wait(2), "deadline did not signal display thread"
            self.finished.set()
            raise RuntimeError("typing interrupted")

        def request_stop(self):
            self.stopped.set()

        def disconnect(self):
            assert self.finished.is_set(), "display was closed while typing continued"
            super().disconnect()

    display = BlockingDisplay()
    decision = ActionDecision(Action(ActionType.TYPE, {"text": "saved report"}), "", 1.0, "")
    session = TaskSession(
        llm=MockLLMProvider(decide_result=decision),
        display=display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=0.1),
    )
    session._capture_screenshot = AsyncMock(return_value=display.capture_screen())
    if phase == "recover":
        from ai_desktop_agent.agent.llm.types import RecoveryPlan, RecoveryStrategy

        async def recover(goal):
            session.loop.understanding_done()
            session.loop.plan_ready([Subtask(id="save", description="文書を保存する")])
            await session._execute_recovery_actions(
                RecoveryPlan(
                    RecoveryStrategy.ALTERNATIVE_APPROACH,
                    [decision.action, Action(ActionType.KEY_PRESS, {"key": "enter"})],
                    "別の方法で入力する",
                    True,
                ),
                0,
            )

        session._run_task = recover
    assert not await session.run("文書を入力する")
    assert display.entered.is_set() and display.finished.is_set() and not display.is_connected
    assert session._store.load(session.id).stop_reason == "duration_limit"
    actions = session._store.load(session.id).actions
    assert len(actions) == 1 and actions[0].success is False
    assert "一部が実行されている可能性" in actions[0].error_message
    assert not display.key_presses


async def test_parallel_tasks_and_standalone_calls_do_not_share_budgets():
    llms = [await llm_with_transport(lambda request: completion()) for _ in range(2)]
    budgets = [
        TaskBudget(ExecutionLimits(max_tokens=5)),
        TaskBudget(ExecutionLimits(max_tokens=20)),
    ]

    async def call(llm, budget):
        try:
            async with budget.activate(lambda: None):
                return await llm._call("保存を確認する")
        except TaskLimitError as error:
            return error.reason

    try:
        results = await asyncio.gather(
            *(call(llm, budget) for llm, budget in zip(llms, budgets, strict=True))
        )
        assert results == ["token_limit", {}]
        assert [b.requests for b in budgets] == [1, 1]
        assert await llms[0]._call("独立した確認") == {}
        assert budgets[0].requests == 1
    finally:
        await asyncio.gather(*(llm.aclose() for llm in llms))


async def test_expired_deadline_is_checked_before_sends_or_gui_work():
    session = TaskSession(
        llm=MockLLMProvider(), display=FakeDisplayBackend(), limits=ExecutionLimits(max_seconds=1)
    )
    session._budget._started = time.monotonic() - 2
    with pytest.raises(TaskLimitError):
        session._budget.begin_request()
    with pytest.raises(TaskLimitError):
        await session._execute_recorded_action(Action(ActionType.TYPE, {"text": "report"}), "")
    assert session._budget.requests == 0 and not session.display.text_inputs
    await session.aclose()


async def test_external_cancellation_is_preserved_when_deadline_also_fires():
    async def run():
        task = asyncio.current_task()
        budget = TaskBudget(ExecutionLimits(max_seconds=0.01))
        async with budget.activate(lambda: task.cancel()):
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_malformed_success_response_cannot_trigger_unmetered_retries():
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(
            200, text="invalid JSON", headers={"Content-Type": "application/json"}
        )

    llm = await llm_with_transport(handle)
    budget = TaskBudget(ExecutionLimits())
    try:
        with pytest.raises(TaskLimitError) as error:
            async with budget.activate(lambda: None):
                await llm._call("文書を確認する")
        assert error.value.reason == "usage_unavailable"
        assert len(sent) == 1 and budget.snapshot()["usage_complete"] is False
    finally:
        await llm.aclose()
