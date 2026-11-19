import asyncio
from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    DecompositionResult,
    ErrorContext,
    RecoveryPlan,
    RecoveryStrategy,
)
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def recovering(plan, **kwargs):
    llm = MockLLMProvider(recover_result=plan)
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=None, **kwargs)
    session.loop.start(Goal(description="文書を保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask(id="s1", description="文書を保存する")])
    session.loop.record_action(Action(ActionType.LEFT_CLICK, {"x": 1, "y": 1}), False, "古い失敗")
    session.loop.action_executed()
    session.loop.wait_complete()
    session.loop.verify_failed()
    return session


async def test_alternative_actions_are_executed_recorded_and_explained():
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [Action(ActionType.KEY_PRESS, {"key": "escape"})],
        "ダイアログを閉じてから保存し直す",
        True,
    )
    session = recovering(plan)
    await session._recover_phase()
    assert session.display.key_presses == ["escape"]
    assert session.loop.state == AgentState.EXECUTING
    assert session.loop.context.action_history[-1].reasoning == plan.reasoning
    assert plan.reasoning in session._pending_error.error_message


@pytest.mark.parametrize("completed_id", ["done", "replan_1_0"])
async def test_replan_preserves_completed_work_and_runs_cleanup_actions(tmp_path, completed_id):
    plan = RecoveryPlan(
        RecoveryStrategy.REPLAN_SUBTASK,
        [Action(ActionType.KEY_PRESS, {"key": "escape"})],
        "保存先を選び直す",
        True,
    )
    session = recovering(plan, store=TaskStore(tmp_path))
    done = Subtask(id=completed_id, description="文書を作成する")
    session.loop.context.subtasks.insert(0, done)
    session.loop.context.current_subtask_index = 1
    new_step = Subtask(id="replacement", description="別名で保存する")
    session.llm.decompose_task = AsyncMock(return_value=DecompositionResult([new_step], "new"))
    await session._recover_phase()
    assert session.display.key_presses == ["escape"]
    assert session.loop.context.subtasks[0] is done
    assert session.loop.context.current_subtask_index == 1
    assert session.loop.context.current_subtask.description == new_step.description
    assert session.loop.state == AgentState.EXECUTING
    identifiers = [subtask.id for subtask in session.loop.context.subtasks]
    assert len(set(identifiers)) == len(identifiers)
    saved = TaskStore(tmp_path).load(session.id)
    assert [subtask["id"] for subtask in saved.subtasks] == identifiers
    goal, count = session.llm.decompose_task.call_args.args
    assert count == 1
    assert any(done.description in constraint for constraint in goal.constraints)
    assert any(plan.reasoning in constraint for constraint in goal.constraints)
    await session.aclose()


async def test_current_verification_error_takes_precedence_over_old_failure():
    plan = RecoveryPlan(RecoveryStrategy.ALTERNATIVE_APPROACH, [], "再確認する", True)
    session = recovering(plan)
    current = ErrorContext(Action(ActionType.SUBTASK_COMPLETE), "保存されたファイルがありません", 0)
    session._pending_error = current
    session.llm.recover_from_error = AsyncMock(return_value=plan)
    await session._recover_phase()
    assert session.llm.recover_from_error.call_args.args[0] is current


async def test_recovery_obeys_retry_limit():
    plan = RecoveryPlan(RecoveryStrategy.ALTERNATIVE_APPROACH, [], "再試行する", True)
    session = recovering(plan)
    session.loop.context.current_subtask.max_retries = 1
    await session._recover_phase()
    session.loop.action_executed()
    session.loop.wait_complete()
    session.loop.verify_failed()
    await session._recover_phase()
    assert session.loop.state == AgentState.FAILED


async def test_recovery_cannot_restart_without_permission(monkeypatch):
    restart = AsyncMock()
    monkeypatch.setattr("ai_desktop_agent.server.vm_pool.VmPool.restart_vm", restart)
    plan = RecoveryPlan(
        RecoveryStrategy.ALTERNATIVE_APPROACH,
        [Action(ActionType.VM_RESTART, {"reason": "再起動して回復する"})],
        "再起動する",
        True,
    )
    session = recovering(plan)
    await session._recover_phase()
    restart.assert_not_called()
    assert session.loop.state == AgentState.RECOVERING
    assert not session.loop.context.action_history[-1].success


@pytest.mark.parametrize("outcome", ["empty_plan", "provider_error", "stop_paused"])
async def test_failed_exits_persist_before_completion_and_notify_once(tmp_path, outcome):
    store = TaskStore(tmp_path)
    llm = MockLLMProvider()
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), store=store, decision=None)
    finished = []
    session.on_complete(lambda success: finished.append((success, store.load(session.id))))
    if outcome == "empty_plan":
        llm.decompose_task = AsyncMock(return_value=DecompositionResult([], "empty"))
    elif outcome == "provider_error":
        llm.understand_instruction = AsyncMock(side_effect=RuntimeError("unavailable"))
    else:
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        session._execute_phase = wait
    await session.start_async("文書を保存する")
    if outcome == "stop_paused":
        await entered.wait()
        session.pause()
        session.stop()
    await asyncio.gather(session._task, return_exceptions=True)
    assert len(finished) == 1
    success, record = finished[0]
    assert success is False
    assert record.state == "failed"
    assert record.success is False
    assert session.loop.state == AgentState.FAILED
    assert not session.display.is_connected
    if session._control_notifications:
        await asyncio.gather(*session._control_notifications)
    assert store.load(session.id).state == "failed"


def test_action_timestamps_do_not_use_the_task_creation_time():
    plan = RecoveryPlan(RecoveryStrategy.GIVE_UP, [], "終了する", False)
    session = recovering(plan)
    session._created_at = 1
    snapshot = session.snapshot()
    assert snapshot.actions[0].at > 1
    assert snapshot.actions[0].at == session.loop.context.action_history[0].at
