"""Recovery boundaries preserve the actual failed action and stop unsafe tails."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.decision.types import DecisionRecovery, DecisionVerify
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    RecoveryPlan,
    RecoveryStrategy,
)
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.server import session as session_module
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.test_manual_input import wait_for_manual


@pytest.mark.parametrize("escalate", [False, True])
async def test_successful_manual_recovery_does_not_inherit_the_previous_action_error(
    tmp_path, escalate, session_retry_sleep
):
    display = FakeDisplayBackend()
    display.key_press = Mock(side_effect=OSError("keyboard response lost"))
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [Action(ActionType.REQUEST_USER_INPUT, {"reason": "接続先を確認して入力してください"})],
        "利用者に入力を依頼してから確認する",
        True,
    )
    llm = MockLLMProvider(recover_result=plan)
    llm.recover_from_error = AsyncMock(wraps=llm.recover_from_error)
    llm.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(
                Action(ActionType.KEY_PRESS, {"key": "escape"}), "inspect", 1, "inspect"
            ),
            ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "saved", 1, "saved"),
        ]
    )
    decision = None
    if escalate:
        decision = Mock()
        decision.choose_recovery = AsyncMock(
            return_value=DecisionRecovery(
                "wait_and_retry",
                True,
                0.5,
                "uncertain recovery",
                escalate=True,
            )
        )
        decision.verify_completion = AsyncMock(
            return_value=DecisionVerify(
                None,
                0.5,
                "partial",
                "main model must verify",
            )
        )
        decision.aclose = AsyncMock()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, decision=decision, store=store)
    session.executor.wait_for_still = AsyncMock()
    await session.start_async("接続先を確認して文書を保存する")
    try:
        consent = await wait_for_manual(session)
        assert llm.decide_next_action.await_count == 1
        session.resume(manual_input_id=consent["id"], allow_screen_share=True)
        assert await asyncio.wait_for(session._task, 3) is True
        record = store.load(session.id)
        assert record.state == "completed" and record.success is True
        assert [a.action_type for a in record.actions] == [
            "key_press",
            "request_user_input",
            "subtask_complete",
        ]
        assert record.actions[0].success is False
        assert "keyboard response lost" in record.actions[0].error_message
        handoff = record.actions[1]
        assert handoff.success is True and handoff.error_message == ""
        assert handoff.reasoning == plan.reasoning
        assert llm.recover_from_error.await_count == 1
        assert record.decision_escalations == (2 if escalate else 0)
    finally:
        session.stop()
        await asyncio.gather(session._task, return_exceptions=True)
        await session.aclose()


@pytest.mark.parametrize("outcome", ["action-limit", "failed-recovery", "empty-replan"])
async def test_failed_recovery_never_executes_the_remaining_input_plan(
    tmp_path, monkeypatch, outcome, session_retry_sleep
):
    display = FakeDisplayBackend()
    display.key_press = Mock(side_effect=OSError("keyboard response lost"))
    plan = RecoveryPlan(
        RecoveryStrategy.REPLAN_SUBTASK
        if outcome == "empty-replan"
        else RecoveryStrategy.ALTERNATIVE_APPROACH,
        []
        if outcome == "empty-replan"
        else [
            Action(ActionType.KEY_PRESS, {"key": "tab"}),
            Action(ActionType.TYPE, {"text": "this tail must never be typed"}),
        ],
        "inspect another target",
        True,
    )
    llm = MockLLMProvider(recover_result=plan)
    llm.recover_from_error = AsyncMock(wraps=llm.recover_from_error)
    llm.decide_next_action = AsyncMock(
        return_value=ActionDecision(
            Action(ActionType.KEY_PRESS, {"key": "escape"}),
            "inspect",
            1,
            "inspect",
        )
    )
    if outcome == "action-limit":
        monkeypatch.setattr(session_module, "MAX_ACTIONS_PER_TASK", 1)
    if outcome == "empty-replan":
        llm.decompose_task = AsyncMock(
            side_effect=[
                DecompositionResult([Subtask("s1", "save the document")], "initial plan"),
                DecompositionResult([], "no safe replacement"),
            ]
        )
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    session.executor.wait_for_still = AsyncMock()
    assert await session.run("文書を保存する") is False
    record = store.load(session.id)
    assert (
        record.stop_reason
        == {
            "action-limit": "action_limit",
            "failed-recovery": "retry_limit",
            "empty-replan": "replanning_failed",
        }[outcome]
    )
    assert display.text_inputs == []
    assert llm.decide_next_action.await_count == 1
    assert llm.recover_from_error.await_count == (3 if outcome == "failed-recovery" else 1)
    assert len(record.actions) == (4 if outcome == "failed-recovery" else 1)
    assert all(a.action_type == "key_press" and not a.success for a in record.actions)
    assert not display.is_connected


@pytest.mark.parametrize("kind", [ActionType.REGION_SELECT, ActionType.SUBTASK_COMPLETE])
async def test_session_only_recovery_step_requires_a_new_visual_decision_before_any_tail(
    tmp_path, kind, session_retry_sleep
):
    display = FakeDisplayBackend()
    display.key_press = Mock(side_effect=OSError("keyboard response lost"))
    params = (
        {"x": 10, "y": 20, "width": 100, "height": 100}
        if kind == ActionType.REGION_SELECT
        else None
    )
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [
            Action(kind, params),
            Action(ActionType.TYPE, {"text": "forbidden tail"}),
        ],
        "inspect a fresh screenshot",
        True,
    )
    llm = MockLLMProvider(recover_result=plan)
    llm.decide_next_action = AsyncMock(
        side_effect=[
            ActionDecision(
                Action(ActionType.KEY_PRESS, {"key": "escape"}), "inspect", 1, "inspect"
            ),
            ActionDecision(Action(ActionType.SUBTASK_COMPLETE), "saved", 1, "verified externally"),
        ]
    )
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    session.executor.wait_for_still = AsyncMock()
    assert await session.run("文書を確認する") is True
    record = store.load(session.id)
    assert [a.action_type for a in record.actions] == ["key_press", "subtask_complete"]
    assert display.text_inputs == [] and llm.decide_next_action.await_count == 2
    assert llm.decide_next_action.call_args.kwargs["error_context"] is not None


async def test_disconnected_display_stops_before_another_model_action(tmp_path):
    display = FakeDisplayBackend()
    display.disconnect()
    llm = MockLLMProvider()
    llm.decide_next_action = AsyncMock()
    store = TaskStore(tmp_path)
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    assert await session.run("画面を確認する") is False
    record = store.load(session.id)
    assert record.stop_reason == "execution_error"
    assert "接続されていません" in record.error_message
    assert record.actions == []
    llm.decide_next_action.assert_not_awaited()
