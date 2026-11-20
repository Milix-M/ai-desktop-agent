"""Rejected transitions preserve the task; accepted hooks observe committed data."""

from dataclasses import asdict

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.loop import AgentLoop, InvalidTransitionError
from ai_desktop_agent.agent.state import AgentContext, AgentState, Goal, Subtask


def running_loop():
    loop = AgentLoop()
    loop.start(Goal("四半期報告を保存する"))
    loop.understanding_done()
    loop.plan_ready([Subtask("read", "報告を確認する"), Subtask("save", "報告を保存する")])
    loop.record_action(Action(ActionType.KEY_PRESS, {"key": "Tab"}), True)
    return loop


def finish(loop):
    while not loop.is_done:
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()


@pytest.mark.parametrize("state", ["completed", "failed", "waiting"])
@pytest.mark.parametrize("operation", ["start", "plan_ready", "verify_subtask_done"])
def test_rejected_transition_cannot_replace_goal_plan_or_progress(state, operation):
    loop = running_loop()
    if state == "completed":
        finish(loop)
    elif state == "failed":
        loop.fail_now()
    else:
        loop.action_executed()
    before, count = asdict(loop.context), loop.transition_count
    goal, plan = loop.context.goal, loop.context.subtasks
    observed = []
    for target in AgentState:
        loop.on_enter(target, observed.append)
    with pytest.raises(InvalidTransitionError):
        if operation == "start":
            loop.start(Goal("別の指示へ置き換える"))
        elif operation == "plan_ready":
            loop.plan_ready([Subtask("other", "別の計画を実行する")])
        else:
            loop.verify_subtask_done()
    assert asdict(loop.context) == before
    assert loop.context.goal is goal and loop.context.subtasks is plan
    assert loop.transition_count == count and not observed
    if state == "waiting":
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.context.current_subtask.id == "save"
        finish(loop)
        assert loop.context.current_subtask_index == 2 and loop.state == AgentState.COMPLETED
    else:
        loop.reset()
        loop.start()
        assert loop.context.goal is goal


def test_rejected_start_while_executing_cannot_redirect_the_running_goal():
    loop = running_loop()
    before = asdict(loop.context)
    with pytest.raises(InvalidTransitionError):
        loop.start(Goal("別の報告を開く"))
    assert asdict(loop.context) == before and loop.is_running
    finish(loop)
    assert loop.state == AgentState.COMPLETED
    assert loop.context.goal.description == "四半期報告を保存する"


def test_completion_before_a_plan_cannot_advance_the_initial_progress():
    loop = AgentLoop()
    before = asdict(loop.context)
    with pytest.raises(InvalidTransitionError):
        loop.verify_subtask_done()
    assert asdict(loop.context) == before and loop.transition_count == 0
    loop.start(Goal("報告を保存する"))
    loop.understanding_done()
    loop.plan_ready([Subtask("save", "保存する")])
    finish(loop)
    assert loop.state == AgentState.COMPLETED and loop.context.current_subtask_index == 1


def test_start_hook_observes_the_new_goal_and_accepts_a_preconfigured_goal():
    goal = Goal("保存済みの報告を開く")
    loop = AgentLoop(AgentContext(goal=goal))
    observed = []
    loop.on_enter(
        AgentState.UNDERSTANDING, lambda state: observed.append((state, loop.context.goal))
    )
    loop.start()
    assert observed == [(AgentState.UNDERSTANDING, goal)]
    loop.understanding_failed()
    loop.reset()
    replacement = Goal("次の報告を開く")
    loop.start(replacement)
    assert observed[-1] == (AgentState.UNDERSTANDING, replacement)


def test_plan_hook_observes_the_new_plan_and_the_completed_prefix():
    loop = AgentLoop()
    loop.start(Goal("報告の保存を再計画する"))
    loop.understanding_done()
    plan = [Subtask("read", "確認済みの報告"), Subtask("save", "保存を再試行する")]
    observed = []
    loop.on_enter(
        AgentState.EXECUTING,
        lambda state: observed.append(
            (state, list(loop.context.subtasks), loop.context.current_subtask_index)
        ),
    )
    loop.plan_ready(plan, completed_count=1)
    assert observed == [(AgentState.EXECUTING, plan, 1)]
    assert loop.context.current_subtask is plan[1]


@pytest.mark.parametrize("count", [1, 2])
def test_progress_hooks_observe_advanced_progress_before_running_or_completion(count):
    loop = AgentLoop()
    loop.start(Goal("報告を処理する"))
    loop.understanding_done()
    loop.plan_ready([Subtask(str(i), "報告の工程") for i in range(count)])
    loop.action_executed()
    loop.wait_complete()
    observed = []
    target = AgentState.COMPLETED if count == 1 else AgentState.EXECUTING
    loop.on_enter(
        target, lambda state: observed.append((state, loop.context.current_subtask_index))
    )
    loop.verify_subtask_done()
    assert observed == [(target, 1)]
    assert loop.is_done == (count == 1)


@pytest.mark.parametrize("completed_count", [-1, 1])
def test_an_invalid_completed_prefix_keeps_the_plan_and_can_be_corrected(completed_count):
    loop = AgentLoop()
    loop.start(Goal("報告を保存する"))
    loop.understanding_done()
    before = asdict(loop.context)
    with pytest.raises(ValueError, match="サブタスク"):
        loop.plan_ready([Subtask("save", "保存する")], completed_count=completed_count)
    assert asdict(loop.context) == before
    loop.plan_ready([Subtask("save", "保存する")])
    finish(loop)
    assert loop.state == AgentState.COMPLETED


def test_invalid_manual_return_state_keeps_the_handoff_and_accepts_a_corrected_return():
    loop = running_loop()
    previous = loop.wait_for_user()
    before = asdict(loop.context)
    with pytest.raises(ValueError, match="手動入力前"):
        loop.user_input_done(AgentState.WAITING)
    assert asdict(loop.context) == before
    loop.user_input_done(previous)
    finish(loop)
    assert loop.state == AgentState.COMPLETED


def test_verify_success_can_complete_progress_advanced_through_the_public_context():
    loop = AgentLoop()
    loop.start(Goal("報告を確認する"))
    loop.understanding_done()
    loop.plan_ready([Subtask("check", "報告を確認する")])
    loop.action_executed()
    loop.wait_complete()
    assert loop.context.advance_subtask() is None
    loop.verify_success()
    assert loop.state == AgentState.COMPLETED and loop.context.is_task_complete


def test_a_failing_entry_hook_keeps_an_accepted_transition_and_its_new_plan():
    loop = AgentLoop()
    loop.start(Goal("報告を保存する"))
    loop.understanding_done()
    plan = [Subtask("save", "保存する")]
    error = RuntimeError("observer failed")

    def observe(state):
        assert state == loop.state == AgentState.EXECUTING
        assert loop.context.subtasks is plan
        raise error

    loop.on_enter(AgentState.EXECUTING, observe)
    with pytest.raises(RuntimeError) as caught:
        loop.plan_ready(plan)
    assert caught.value is error
    assert loop.state == AgentState.EXECUTING and loop.context.subtasks is plan
    loop.fail_now()
    assert loop.is_done
