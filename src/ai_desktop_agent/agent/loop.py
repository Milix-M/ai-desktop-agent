"""エージェントループ — 状態機械の遷移を管理するコアクラス。

AgentLoop は AgentContext の状態遷移を管理する。
各メソッドは1つの遷移に対応し、無効な遷移は ValueError を送出する。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from ai_desktop_agent.actions.primitives import Action
from ai_desktop_agent.agent.state import (
    AgentContext,
    AgentState,
    Goal,
    Subtask,
    can_transition,
    is_active,
    is_terminal,
)


class InvalidTransitionError(ValueError):
    """不正な状態遷移が試みられた場合のエラー。"""

    def __init__(self, from_state: AgentState, to_state: AgentState) -> None:
        super().__init__(f"不正な状態遷移: {from_state.value} → {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


@dataclass(frozen=True)
class TransitionRecord:
    """状態遷移の記録。"""

    from_state: AgentState
    to_state: AgentState
    timestamp: float


class AgentLoop:
    """エージェント状態機械のループ本体。

    AgentContext をラップし、有効な遷移のみを通す。
    LLM呼び出しやVNC操作は行わず、状態管理に徹する。
    """

    def __init__(self, context: AgentContext | None = None) -> None:
        self.context = context or AgentContext()
        self._history: list[TransitionRecord] = []
        self._hooks: dict[AgentState, list[Callable[[AgentState], None]]] = {}

    # ── 内部 ──────────────────────────────────────────

    def _transition(self, to_state: AgentState) -> None:
        """状態遷移を実行し、履歴とフックを処理。"""
        from_state = self.context.state
        if not can_transition(from_state, to_state):
            raise InvalidTransitionError(from_state, to_state)

        self.context.state = to_state
        self._history.append(
            TransitionRecord(
                from_state=from_state,
                to_state=to_state,
                timestamp=time.monotonic(),
            )
        )

        # 遷移後フックを実行
        for hook in self._hooks.get(to_state, []):
            hook(to_state)

    # ── フック ────────────────────────────────────────

    def on_enter(self, state: AgentState, callback: Callable[[AgentState], None]) -> None:
        """状態に入ったときのフックを登録。"""
        self._hooks.setdefault(state, []).append(callback)

    # ── 公開プロパティ ────────────────────────────────

    @property
    def state(self) -> AgentState:
        return self.context.state

    @property
    def is_running(self) -> bool:
        return is_active(self.context.state)

    @property
    def is_done(self) -> bool:
        return is_terminal(self.context.state)

    @property
    def transition_count(self) -> int:
        return len(self._history)

    # ── ライフサイクル遷移 ────────────────────────────

    def start(self, goal: Goal | None = None) -> None:
        """タスクを開始: IDLE → UNDERSTANDING。

        Args:
            goal: 設定するゴール。None の場合は既存の context.goal を使用。
        """
        if goal is not None:
            self.context.goal = goal
        self._transition(AgentState.UNDERSTANDING)

    def understanding_done(self) -> None:
        """指示理解が完了: UNDERSTANDING → PLANNING。"""
        self._transition(AgentState.PLANNING)

    def understanding_failed(self) -> None:
        """指示理解に失敗: UNDERSTANDING → FAILED。"""
        self._transition(AgentState.FAILED)

    def plan_ready(self, subtasks: list[Subtask]) -> None:
        """計画が完了、実行に移る: PLANNING → EXECUTING。

        Args:
            subtasks: 計画されたサブタスクのリスト。
        """
        self.context.subtasks = subtasks
        self.context.current_subtask_index = 0
        self.context.retry_counts.clear()
        self._transition(AgentState.EXECUTING)

    def planning_failed(self) -> None:
        """計画に失敗: PLANNING → FAILED。"""
        self._transition(AgentState.FAILED)

    # ── 実行サイクル遷移 ──────────────────────────────

    def action_executed(self) -> None:
        """アクション実行後: EXECUTING → WAITING。"""
        self._transition(AgentState.WAITING)

    def wait_complete(self) -> None:
        """待機完了: WAITING → VERIFYING。"""
        self._transition(AgentState.VERIFYING)

    def wait_timeout(self) -> None:
        """待機タイムアウト: WAITING → RECOVERING。"""
        self._transition(AgentState.RECOVERING)

    def verify_success(self) -> None:
        """検証成功 → 次のアクションへ or 完了。

        現在のサブタスクの全アクションが完了していれば COMPLETED、
        そうでなければ EXECUTING に戻る。
        """
        if self.context.is_task_complete:
            self._transition(AgentState.COMPLETED)
        else:
            self._transition(AgentState.EXECUTING)

    def verify_subtask_done(self) -> None:
        """サブタスク完了 → 次のサブタスクへ。"""
        self.context.advance_subtask()
        if self.context.is_task_complete:
            self._transition(AgentState.COMPLETED)
        else:
            self._transition(AgentState.EXECUTING)

    def verify_failed(self) -> None:
        """検証失敗: VERIFYING → RECOVERING。"""
        self._transition(AgentState.RECOVERING)

    # ── 回復遷移 ──────────────────────────────────────

    def recover_retry(self) -> None:
        """再試行: RECOVERING → EXECUTING。"""
        self._transition(AgentState.EXECUTING)

    def recover_replan(self) -> None:
        """再計画: RECOVERING → PLANNING。"""
        self._transition(AgentState.PLANNING)

    def recover_failed(self) -> None:
        """回復不能: RECOVERING → FAILED。"""
        self._transition(AgentState.FAILED)

    # ── 割り込み遷移 ──────────────────────────────────

    def pause(self) -> None:
        """ユーザーによる一時停止 → PAUSED。"""
        self._transition(AgentState.PAUSED)

    def resume(self) -> None:
        """PAUSED → EXECUTING に復帰。"""
        self._transition(AgentState.EXECUTING)

    def abort(self) -> None:
        """PAUSED → IDLE（タスク中断）。"""
        self._transition(AgentState.IDLE)

    # ── 終了 ──────────────────────────────────────────

    def reset(self) -> None:
        """COMPLETED または FAILED → IDLE にリセット。"""
        self._transition(AgentState.IDLE)

    # ── アクション記録の委譲 ──────────────────────────

    def record_action(
        self, action: Action, success: bool, error: str = "", duration_ms: float = 0.0
    ) -> None:
        """アクションの実行結果をコンテキストに記録。"""
        self.context.record_action(action, success, error, duration_ms)
