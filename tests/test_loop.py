"""エージェントループのテスト。"""

import pytest
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.state import AgentContext, AgentState, Goal, Subtask
from ai_desktop_agent.agent.loop import AgentLoop, InvalidTransitionError


# ── フィクスチャ ──────────────────────────────────────

@pytest.fixture
def loop() -> AgentLoop:
    return AgentLoop()


@pytest.fixture
def loop_with_goal(loop: AgentLoop) -> AgentLoop:
    loop.start(Goal(description="テストタスク"))
    loop.understanding_done()
    return loop


@pytest.fixture
def loop_executing(loop_with_goal: AgentLoop) -> AgentLoop:
    loop_with_goal.plan_ready([
        Subtask(id="s1", description="step 1"),
        Subtask(id="s2", description="step 2"),
    ])
    return loop_with_goal


# ── 初期状態 ──────────────────────────────────────────

class TestInitialState:
    def test_default_state_is_idle(self, loop):
        assert loop.state == AgentState.IDLE

    def test_not_running_initially(self, loop):
        assert not loop.is_running

    def test_not_done_initially(self, loop):
        assert not loop.is_done


# ── 正常なライフサイクル ──────────────────────────────

class TestHappyPath:
    """IDLE → UNDERSTANDING → PLANNING → EXECUTING → ... → COMPLETED → IDLE"""

    def test_full_lifecycle(self, loop_executing):
        loop = loop_executing
        assert loop.state == AgentState.EXECUTING
        assert loop.is_running

        # サブタスク1: アクション実行 → 待機 → 検証成功 → サブタスク完了
        loop.action_executed()
        assert loop.state == AgentState.WAITING

        loop.wait_complete()
        assert loop.state == AgentState.VERIFYING

        loop.verify_subtask_done()
        assert loop.state == AgentState.EXECUTING  # s2へ

        # サブタスク2: 同様
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.state == AgentState.COMPLETED
        assert loop.is_done

        loop.reset()
        assert loop.state == AgentState.IDLE

    def test_transition_count_is_tracked(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_complete()
        assert loop.transition_count >= 2  # start + understand + plan + action + wait


# ── 無効遷移 ──────────────────────────────────────────

class TestInvalidTransitions:
    def test_idle_to_executing_raises(self, loop):
        with pytest.raises(InvalidTransitionError):
            loop.action_executed()

    def test_executing_to_planning_raises(self, loop_executing):
        with pytest.raises(InvalidTransitionError):
            loop_executing.recover_replan()  # EXECUTING → PLANNING は不可

    def test_completed_to_executing_raises(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.is_done
        with pytest.raises(InvalidTransitionError):
            loop.action_executed()

    def test_error_message_includes_states(self, loop):
        with pytest.raises(InvalidTransitionError) as exc:
            loop.action_executed()
        assert "idle" in str(exc.value)
        assert "waiting" in str(exc.value)


# ── エラー回復パス ────────────────────────────────────

class TestRecoveryPath:
    def test_verify_failed_to_recovering_to_retry(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_complete()
        loop.verify_failed()
        assert loop.state == AgentState.RECOVERING

        loop.recover_retry()
        assert loop.state == AgentState.EXECUTING

    def test_wait_timeout_to_recovering(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_timeout()
        assert loop.state == AgentState.RECOVERING

    def test_recover_to_replan(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_complete()
        loop.verify_failed()
        loop.recover_replan()
        assert loop.state == AgentState.PLANNING

    def test_recover_to_failed(self, loop_executing):
        loop = loop_executing
        loop.action_executed()
        loop.wait_complete()
        loop.verify_failed()
        loop.recover_failed()
        assert loop.state == AgentState.FAILED
        assert loop.is_done

        loop.reset()
        assert loop.state == AgentState.IDLE


# ── 計画失敗パス ──────────────────────────────────────

class TestPlanningFailure:
    def test_understanding_failed(self, loop):
        loop.start(Goal(description="x"))
        loop.understanding_failed()
        assert loop.state == AgentState.FAILED

    def test_planning_failed(self, loop_with_goal):
        loop_with_goal.planning_failed()
        assert loop_with_goal.state == AgentState.FAILED


# ── 一時停止・再開 ────────────────────────────────────

class TestPauseResume:
    def test_pause_from_executing(self, loop_executing):
        loop_executing.pause()
        assert loop_executing.state == AgentState.PAUSED

    def test_resume_to_executing(self, loop_executing):
        loop_executing.pause()
        loop_executing.resume()
        assert loop_executing.state == AgentState.EXECUTING

    def test_abort_from_paused(self, loop_executing):
        loop_executing.pause()
        loop_executing.abort()
        assert loop_executing.state == AgentState.IDLE


# ── サブタスク進行 ────────────────────────────────────

class TestSubtaskProgression:
    def test_verify_success_after_last_subtask(self, loop):
        """全サブタスク完了後に verify_subtask_done → COMPLETED。"""
        loop.start(Goal(description="test"))
        loop.understanding_done()
        loop.plan_ready([Subtask(id="s1", description="only step")])

        # 通常の実行サイクル → サブタスク完了
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.context.is_task_complete
        assert loop.state == AgentState.COMPLETED

    def test_multiple_subtasks_complete_in_order(self, loop):
        loop.start(Goal(description="test"))
        loop.understanding_done()
        loop.plan_ready([
            Subtask(id="s1", description="step 1"),
            Subtask(id="s2", description="step 2"),
            Subtask(id="s3", description="step 3"),
        ])
        assert loop.context.current_subtask is not None
        assert loop.context.current_subtask.id == "s1"

        # s1 → s2
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.context.current_subtask.id == "s2"

        # s2 → s3
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.context.current_subtask.id == "s3"

        # s3 → 完了
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert loop.state == AgentState.COMPLETED


# ── フック ────────────────────────────────────────────

class TestHooks:
    def test_on_enter(self, loop):
        entered: list[AgentState] = []
        loop.on_enter(AgentState.EXECUTING, lambda s: entered.append(s))
        loop.start(Goal(description="test"))
        loop.understanding_done()
        loop.plan_ready([Subtask(id="s1", description="s1")])
        assert entered == [AgentState.EXECUTING]

    def test_multiple_hooks(self, loop):
        calls: list[str] = []
        loop.on_enter(AgentState.COMPLETED, lambda s: calls.append("hook1"))
        loop.on_enter(AgentState.COMPLETED, lambda s: calls.append("hook2"))

        loop.start(Goal(description="test"))
        loop.understanding_done()
        loop.plan_ready([Subtask(id="s1", description="s1")])
        loop.action_executed()
        loop.wait_complete()
        loop.verify_subtask_done()
        assert calls == ["hook1", "hook2"]


# ── アクション記録 ────────────────────────────────────

class TestActionRecording:
    def test_record_action(self, loop_executing):
        action = Action(action_type=ActionType.LEFT_CLICK,
                        params={"x": 10, "y": 20})
        loop_executing.record_action(action, success=True, duration_ms=50.0)
        assert loop_executing.context.success_count == 1
        assert len(loop_executing.context.action_history) == 1

    def test_record_failed_action(self, loop_executing):
        action = Action(action_type=ActionType.TYPE, params={"text": "hello"})
        loop_executing.record_action(action, success=False, error="要素が見つからない")
        assert loop_executing.context.failure_count == 1
