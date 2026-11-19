"""An observer failure must preserve execution, durable results and later observers."""

import asyncio
import io
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.decision.provider import MockDecisionProvider
from ai_desktop_agent.agent.decision.types import DecisionVerify
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    UnderstandingResult,
)
from ai_desktop_agent.agent.state import AgentState, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


async def test_a_plan_observer_can_pause_before_the_first_action_and_resume_normally(tmp_path):
    model = MockLLMProvider()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=FakeDisplayBackend(), store=store, decision=None)
    paused = asyncio.Event()
    requested = False
    completed = []

    def changed(state, context):
        nonlocal requested
        if state == AgentState.EXECUTING and not requested:
            requested = True
            session.pause()
        elif state == AgentState.PAUSED:
            paused.set()

    session.on_state_change(changed)
    session.on_complete(completed.append)
    run = asyncio.create_task(session.run("保存済みの文書を確認する"))
    try:
        await asyncio.wait_for(paused.wait(), 5)
        assert not run.done() and session.loop.state == AgentState.PAUSED
        assert model.decide_call_count == 0 and store.load(session.id).state == "paused"
        notification = session.resume()
        await asyncio.wait_for(notification, 5)
        assert await asyncio.wait_for(asyncio.shield(run), 5)
        saved = TaskStore(tmp_path).load(session.id)
        assert model.decide_call_count > 0 and saved.state == "completed"
        assert saved.success is True and saved.stop_reason is None and completed == [True]
    finally:
        session.stop()
        await asyncio.gather(run, return_exceptions=True)
        await session.aclose()


@pytest.mark.parametrize("phase", ["state_change", "action", "error", "complete"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failing_observers_do_not_hide_results_from_later_observers(
    tmp_path, caplog, phase, asynchronous
):
    success = phase != "error"
    store = TaskStore(tmp_path)
    model = MockLLMProvider(
        understand_result=UnderstandingResult(
            "inspect_report", "file-manager", ["read-only"], "inspect the saved report"
        ),
        decompose_result=DecompositionResult(
            [Subtask("inspect", "inspect the saved report")] if success else [],
            "inspect" if success else "no valid plan",
        ),
    )
    session = TaskSession(llm=model, display=FakeDisplayBackend(), store=store, decision=None)
    notified = []
    completions = []
    attempts = 0

    def fail(*args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("observer unavailable")

    async def fail_async(*args):
        await asyncio.sleep(0)
        fail(*args)

    def observe(*args):
        # Public notifications must follow the corresponding durable write.
        saved = TaskStore(tmp_path).load(session.id)
        assert saved is not None
        notified.append((args, saved.state, saved.success))

    register = getattr(session, f"on_{phase}")
    register(fail_async if asynchronous else fail)
    register(observe)
    session.on_complete(completions.append)
    try:
        assert (
            await asyncio.wait_for(session.run("inspect without modifying the report"), 5)
            is success
        )
        assert attempts > 0 and len(notified) == attempts
        assert completions == [success]
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == ("completed" if success else "failed")
        assert saved.success is success
        assert saved.stop_reason == (None if success else "planning_failed")
        assert saved.goal["intent"] == "inspect_report"
        assert saved.goal["target_application"] == "file-manager"
        assert saved.goal["constraints"] == ["read-only"]
        assert not session.is_running and not session.display.is_connected
        assert "イベント通知に失敗" in caplog.text
    finally:
        await session.aclose()


async def test_a_provider_close_failure_does_not_skip_other_resources_or_rewrite_completion(
    tmp_path, caplog
):
    display = FakeDisplayBackend()
    closed = []

    class ClosingModel(MockLLMProvider):
        async def aclose(self):
            assert not display.is_connected
            closed.append("model")
            raise OSError("provider close acknowledgement lost")

    class ClosingDecision(MockDecisionProvider):
        async def verify_completion(self, *args, **kwargs):
            return DecisionVerify(None, 0.5, "partial", "ask the main model")

        async def aclose(self):
            assert not display.is_connected
            closed.append("decision")

    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=ClosingModel(), display=display, store=store, decision=ClosingDecision()
    )
    completed = []
    session.on_complete(completed.append)
    assert await asyncio.wait_for(session.run("inspect the report"), 5)
    await session.aclose()
    saved = TaskStore(tmp_path).load(session.id)
    assert saved.state == "completed" and saved.success is True and saved.stop_reason is None
    assert completed == [True] and closed == ["model", "decision"]
    assert "プロバイダ接続の解放に失敗" in caplog.text


async def test_recovered_screen_completes_after_uncertain_wake_and_optional_raw_capture_failures(
    tmp_path, caplog
):
    class RecoveringDisplay(FakeDisplayBackend):
        def __init__(self):
            super().__init__(screen_width=96, screen_height=64)
            self.wakes = []
            self.raw_calls = 0

        def capture_screen(self, *, with_overlay=True):
            screenshot = super().capture_screen(with_overlay=with_overlay)
            if self.screenshots_taken == 1:
                pixels = Image.new("RGB", (self.screen_width, self.screen_height), "black")
                buffer = io.BytesIO()
                pixels.save(buffer, format="PNG")
                return replace(screenshot, image_bytes=buffer.getvalue())
            return screenshot

        def key_press(self, key):
            self.wakes.append(key)
            raise OSError("wake acknowledgement lost")

        def capture_raw(self):
            self.raw_calls += 1
            raise OSError("optional raw capture unavailable")

    caplog.set_level("DEBUG", logger="ai_desktop_agent.server.session")
    display = RecoveringDisplay()
    store = TaskStore(tmp_path)
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(Action(ActionType.LEFT_CLICK, {"x": 20, "y": 20}), "", 1, ""),
            ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "", 1, ""),
        ]
    )
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    try:
        assert await asyncio.wait_for(session.run("inspect the recovered display"), 5)
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == "completed" and saved.success is True and saved.stop_reason is None
        assert display.wakes == ["shift"] and display.raw_calls > 0
        assert [action.action_type for action in saved.actions] == [
            "left_click",
            "subtask_complete",
        ]
        assert all(action.success for action in saved.actions)
        assert not display.text_inputs and not display.is_connected
        assert "ウェイクアップキー送信に失敗" in caplog.text
    finally:
        await session.aclose()


@pytest.mark.parametrize("provider", ["clef-openrouter", "workers-ai"])
async def test_missing_optional_decision_credentials_keep_the_main_model_operational(
    tmp_path, monkeypatch, caplog, provider
):
    monkeypatch.setenv("DECISION_PROVIDER", provider)
    for name in [
        "CLEF_API_KEY",
        "OPENROUTER_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    ]:
        monkeypatch.delenv(name, raising=False)
    model = MockLLMProvider()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=FakeDisplayBackend(), store=store)
    completed = []
    session.on_complete(completed.append)
    try:
        assert await asyncio.wait_for(session.run("inspect the report"), 5)
        saved = TaskStore(tmp_path).load(session.id)
        assert saved.state == "completed" and saved.success is True
        assert saved.decision_calls == 0 and saved.decision_escalations == 0
        assert model.decide_call_count > 0 and completed == [True]
        assert not session.display.is_connected
        assert "決定モデルの初期化に失敗したため無効化します" in caplog.text
    finally:
        await session.aclose()
