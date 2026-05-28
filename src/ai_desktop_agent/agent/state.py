"""エージェント状態管理 — 状態機械、コンテキスト、ゴール・サブタスク定義。

エージェントは有限状態機械として動作する。
状態遷移は loop.py の AgentLoop が管理する。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ai_desktop_agent.actions.primitives import Action


class AgentState(StrEnum):
    """エージェントの状態。"""

    IDLE = "idle"                # 指示待ち
    UNDERSTANDING = "understanding"  # ユーザー指示を解析中
    PLANNING = "planning"        # サブタスク分解・計画立案中
    EXECUTING = "executing"      # アクション実行中
    WAITING = "waiting"          # UI変化待機中
    VERIFYING = "verifying"      # 実行結果を検証中
    RECOVERING = "recovering"    # エラーからの回復を試行中
    PAUSED = "paused"            # ユーザーによる一時停止
    COMPLETED = "completed"      # タスク正常完了
    FAILED = "failed"            # タスク遂行不能


# 有効な状態遷移
_VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE:           {AgentState.UNDERSTANDING},
    AgentState.UNDERSTANDING:  {AgentState.PLANNING, AgentState.FAILED},
    AgentState.PLANNING:       {AgentState.EXECUTING, AgentState.FAILED},
    AgentState.EXECUTING:      {AgentState.WAITING, AgentState.RECOVERING, AgentState.COMPLETED, AgentState.FAILED, AgentState.EXECUTING, AgentState.PAUSED},
    AgentState.WAITING:        {AgentState.VERIFYING, AgentState.RECOVERING},
    AgentState.VERIFYING:      {AgentState.EXECUTING, AgentState.RECOVERING, AgentState.COMPLETED, AgentState.FAILED},
    AgentState.RECOVERING:     {AgentState.PLANNING, AgentState.EXECUTING, AgentState.FAILED},
    AgentState.PAUSED:         {AgentState.EXECUTING, AgentState.IDLE},
    AgentState.COMPLETED:      {AgentState.IDLE},
    AgentState.FAILED:         {AgentState.IDLE},
}

# 終端状態
_TERMINAL_STATES: frozenset[AgentState] = frozenset({
    AgentState.COMPLETED,
    AgentState.FAILED,
})


def can_transition(from_state: AgentState, to_state: AgentState) -> bool:
    """from_state から to_state への遷移が有効かどうか。"""
    return to_state in _VALID_TRANSITIONS.get(from_state, set())


def is_terminal(state: AgentState) -> bool:
    """終端状態（COMPLETED / FAILED）かどうか。"""
    return state in _TERMINAL_STATES


def is_active(state: AgentState) -> bool:
    """エージェントがアクティブに動作中かどうか（IDLE/PAUSED/終端以外）。"""
    return state not in (AgentState.IDLE, AgentState.PAUSED) and not is_terminal(state)


@dataclass
class Subtask:
    """タスクを構成する1つのサブタスク。

    Attributes:
        id: サブタスク識別子。
        description: サブタスクの説明（人間向け）。
        expected_outcome: 期待される完了状態（検証に使用）。
        max_retries: エラー時の最大リトライ回数。
        timeout_seconds: サブタスク全体のタイムアウト。
    """

    id: str
    description: str
    expected_outcome: str = ""
    max_retries: int = 3
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries は 0 以上である必要がある")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds は正の値である必要がある")


@dataclass
class Goal:
    """ユーザー指示から抽出された構造化ゴール。

    Attributes:
        description: ユーザーの生の指示。
        intent: 分類された意図（例: "spreadsheet_creation"）。
        target_application: 使用すべきアプリケーション。
        constraints: 制約条件のリスト。
        expected_output: 期待される成果物パスなど。
    """

    description: str
    intent: str = ""
    target_application: str | None = None
    constraints: list[str] = field(default_factory=list)
    expected_output: str = ""


@dataclass
class ActionRecord:
    """実行されたアクションとその結果を記録する。

    Attributes:
        action: 実行されたアクション。
        success: アクションが成功したかどうか。
        error_message: 失敗時のエラーメッセージ。
        duration_ms: 実行にかかった時間（ミリ秒）。
    """

    action: Action
    success: bool
    error_message: str = ""
    duration_ms: float = 0.0


@dataclass
class AgentContext:
    """エージェントの作業メモリ。タスク実行中の全状態を保持する。

    Attributes:
        state: 現在のエージェント状態。
        goal: ユーザーのゴール。
        subtasks: 分解されたサブタスクのリスト。
        current_subtask_index: 現在実行中のサブタスクのインデックス。
        action_history: 実行済みアクションの履歴。
    """

    state: AgentState = AgentState.IDLE
    goal: Goal | None = None
    subtasks: list[Subtask] = field(default_factory=list)
    current_subtask_index: int = 0
    action_history: list[ActionRecord] = field(default_factory=list)

    # サブタスクごとのリトライカウント
    retry_counts: dict[str, int] = field(default_factory=dict)

    @property
    def current_subtask(self) -> Subtask | None:
        """現在のサブタスクを返す。なければ None。"""
        if 0 <= self.current_subtask_index < len(self.subtasks):
            return self.subtasks[self.current_subtask_index]
        return None

    @property
    def is_task_complete(self) -> bool:
        """すべてのサブタスクが完了したか。"""
        return self.current_subtask_index >= len(self.subtasks) and len(self.subtasks) > 0

    def advance_subtask(self) -> Subtask | None:
        """次のサブタスクに進む。最後のサブタスクを超えたら None。"""
        self.current_subtask_index += 1
        return self.current_subtask

    def record_action(self, action: Action, success: bool, error: str = "", duration_ms: float = 0.0) -> None:
        """アクションの実行結果を履歴に記録する。"""
        self.action_history.append(ActionRecord(
            action=action,
            success=success,
            error_message=error,
            duration_ms=duration_ms,
        ))

    def last_actions(self, n: int = 10) -> list[ActionRecord]:
        """直近 n 件のアクション履歴を返す（LLMコンテキスト用）。"""
        return self.action_history[-n:]

    @property
    def success_count(self) -> int:
        """成功したアクションの数。"""
        return sum(1 for r in self.action_history if r.success)

    @property
    def failure_count(self) -> int:
        """失敗したアクションの数。"""
        return sum(1 for r in self.action_history if not r.success)
