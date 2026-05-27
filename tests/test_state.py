"""エージェント状態管理のテスト。"""

import pytest
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.state import (
    AgentContext,
    AgentState,
    Goal,
    Subtask,
    can_transition,
    is_active,
    is_terminal,
)


class TestAgentState:
    """AgentState enum と遷移ルールのテスト。"""

    def test_valid_transitions_from_idle(self):
        """IDLE からは UNDERSTANDING にのみ遷移可能。"""
        assert can_transition(AgentState.IDLE, AgentState.UNDERSTANDING)
        assert not can_transition(AgentState.IDLE, AgentState.EXECUTING)
        assert not can_transition(AgentState.IDLE, AgentState.COMPLETED)

    def test_executing_can_go_to_waiting(self):
        """EXECUTING → WAITING は有効。"""
        assert can_transition(AgentState.EXECUTING, AgentState.WAITING)

    def test_executing_can_go_to_recovering(self):
        """EXECUTING → RECOVERING は有効。"""
        assert can_transition(AgentState.EXECUTING, AgentState.RECOVERING)

    def test_recovering_can_replan(self):
        """RECOVERING → PLANNING は有効（再計画）。"""
        assert can_transition(AgentState.RECOVERING, AgentState.PLANNING)

    def test_recovering_can_retry(self):
        """RECOVERING → EXECUTING は有効（リトライ）。"""
        assert can_transition(AgentState.RECOVERING, AgentState.EXECUTING)

    def test_completed_goes_to_idle(self):
        """COMPLETED → IDLE のみ有効。"""
        assert can_transition(AgentState.COMPLETED, AgentState.IDLE)
        assert not can_transition(AgentState.COMPLETED, AgentState.EXECUTING)

    def test_failed_goes_to_idle(self):
        """FAILED → IDLE のみ有効。"""
        assert can_transition(AgentState.FAILED, AgentState.IDLE)
        assert not can_transition(AgentState.FAILED, AgentState.EXECUTING)

    def test_is_terminal(self):
        """終端状態の判定。"""
        assert is_terminal(AgentState.COMPLETED)
        assert is_terminal(AgentState.FAILED)
        assert not is_terminal(AgentState.EXECUTING)
        assert not is_terminal(AgentState.IDLE)

    def test_is_active(self):
        """アクティブ状態の判定。"""
        assert is_active(AgentState.EXECUTING)
        assert is_active(AgentState.WAITING)
        assert is_active(AgentState.VERIFYING)
        assert not is_active(AgentState.IDLE)
        assert not is_active(AgentState.PAUSED)
        assert not is_active(AgentState.COMPLETED)


class TestSubtask:
    """Subtask データクラスのテスト。"""

    def test_create_valid(self):
        st = Subtask(id="s1", description="アプリを起動")
        assert st.id == "s1"
        assert st.description == "アプリを起動"
        assert st.max_retries == 3

    def test_negative_max_retries_raises(self):
        with pytest.raises(ValueError, match="max_retries"):
            Subtask(id="s1", description="x", max_retries=-1)

    def test_zero_retries_ok(self):
        """max_retries=0 は有効（リトライなし）。"""
        st = Subtask(id="s1", description="x", max_retries=0)
        assert st.max_retries == 0

    def test_non_positive_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            Subtask(id="s1", description="x", timeout_seconds=0)

    def test_expected_outcome_default(self):
        st = Subtask(id="s1", description="x")
        assert st.expected_outcome == ""


class TestGoal:
    """Goal データクラスのテスト。"""

    def test_create_minimal(self):
        g = Goal(description="Excelを開いて")
        assert g.description == "Excelを開いて"
        assert g.intent == ""
        assert g.target_application is None
        assert g.constraints == []

    def test_create_full(self):
        g = Goal(
            description="レポート作成",
            intent="spreadsheet_creation",
            target_application="LibreOffice Calc",
            constraints=["A列に日付"],
            expected_output="/home/user/report.ods",
        )
        assert g.intent == "spreadsheet_creation"
        assert "A列に日付" in g.constraints


class TestAgentContext:
    """AgentContext のテスト。"""

    def test_initial_state(self):
        ctx = AgentContext()
        assert ctx.state == AgentState.IDLE
        assert ctx.goal is None
        assert ctx.subtasks == []
        assert ctx.current_subtask_index == 0
        assert ctx.current_subtask is None
        assert not ctx.is_task_complete

    def test_current_subtask(self):
        ctx = AgentContext(subtasks=[
            Subtask(id="s1", description="step 1"),
            Subtask(id="s2", description="step 2"),
        ])
        assert ctx.current_subtask is not None
        assert ctx.current_subtask.id == "s1"

    def test_advance_subtask(self):
        ctx = AgentContext(subtasks=[
            Subtask(id="s1", description="step 1"),
            Subtask(id="s2", description="step 2"),
        ])
        next_st = ctx.advance_subtask()
        assert next_st is not None
        assert next_st.id == "s2"
        assert ctx.current_subtask_index == 1

    def test_advance_past_end(self):
        ctx = AgentContext(subtasks=[Subtask(id="s1", description="step 1")])
        ctx.advance_subtask()
        assert ctx.current_subtask_index == 1
        assert ctx.current_subtask is None
        assert ctx.is_task_complete

    def test_is_task_complete_empty(self):
        """サブタスクが空なら未完了。"""
        ctx = AgentContext(subtasks=[])
        assert not ctx.is_task_complete

    def test_is_task_complete_after_advance(self):
        ctx = AgentContext(subtasks=[
            Subtask(id="s1", description="step 1"),
            Subtask(id="s2", description="step 2"),
        ])
        ctx.advance_subtask()  # → s2
        ctx.advance_subtask()  # → past end
        assert ctx.is_task_complete

    def test_record_action(self):
        ctx = AgentContext()
        action = Action(action_type=ActionType.LEFT_CLICK)
        ctx.record_action(action, success=True, duration_ms=150.0)
        ctx.record_action(Action(action_type=ActionType.TYPE, params={"text": "hello"}), success=False, error="timeout")
        assert len(ctx.action_history) == 2
        assert ctx.success_count == 1
        assert ctx.failure_count == 1

    def test_last_actions(self):
        ctx = AgentContext()
        for i in range(15):
            action = Action(action_type=ActionType.WAIT, params={"seconds": 1.0})
            ctx.record_action(action, success=True)
        assert len(ctx.last_actions(10)) == 10
        assert len(ctx.last_actions(20)) == 15  # 15件しかないので15

    def test_retry_counts(self):
        ctx = AgentContext()
        ctx.retry_counts["s1:left_click"] = 2
        assert ctx.retry_counts["s1:left_click"] == 2
