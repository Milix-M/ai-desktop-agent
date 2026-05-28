"""LLMプロバイダが返す応答型の定義。

各LLMプロバイダはこのモジュールで定義された型で応答を返す。
JSONパースで得られたdictをこれらの型に変換して利用する。
"""

from dataclasses import dataclass, field
from enum import StrEnum

from ai_desktop_agent.actions.primitives import Action
from ai_desktop_agent.agent.state import Subtask


@dataclass(frozen=True)
class ActionDecision:
    """LLMが決定した次のアクション。

    Attributes:
        action: 実行すべきアクション。
        expected_effect: 期待される効果（検証に使用）。
        confidence: 確信度 0.0〜1.0。
        reasoning: 判断理由（ログ出力用）。
    """

    action: Action
    expected_effect: str = ""
    confidence: float = 1.0
    reasoning: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence は 0.0〜1.0 の範囲である必要がある: {self.confidence}")


@dataclass(frozen=True)
class VerificationResult:
    """アクション実行結果の検証結果。

    Attributes:
        success: 検証に成功したか。
        reasoning: 判断理由。
        evidence: 成功/失敗の根拠（OCRテキストなど）。
    """

    success: bool
    reasoning: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class RecoveryPlan:
    """エラーからの回復計画。

    Attributes:
        strategy: 回復戦略の種類。
        actions: 実行すべき回復アクションのリスト。
        reasoning: この戦略を選んだ理由。
        recoverable: 回復可能かどうか。False の場合 FAILED へ。
    """

    strategy: str
    actions: list[Action] = field(default_factory=list)
    reasoning: str = ""
    recoverable: bool = True


@dataclass(frozen=True)
class DecompositionResult:
    """タスク分解の結果。

    Attributes:
        subtasks: 分解されたサブタスクのリスト。
        reasoning: 分解の理由。
    """

    subtasks: list[Subtask]
    reasoning: str = ""


@dataclass(frozen=True)
class UnderstandingResult:
    """指示理解の結果。

    Attributes:
        intent: 分類された意図。
        target_application: 使用すべきアプリケーション（あれば）。
        constraints: 制約条件のリスト。
        reasoning: 理解の理由。
    """

    intent: str
    target_application: str | None = None
    constraints: list[str] = field(default_factory=list)
    reasoning: str = ""


class RecoveryStrategy(StrEnum):
    """エラー回復戦略の種類。"""

    WAIT_AND_RETRY = "wait_and_retry"
    SCROLL_AND_RETRY = "scroll_and_retry"
    CLOSE_DIALOG = "close_dialog"
    ALT_WINDOW = "alt_window"
    REFRESH = "refresh"
    ALTERNATIVE_APPROACH = "alternative_approach"
    REPLAN_SUBTASK = "replan_subtask"
    ASK_USER = "ask_user"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class ErrorContext:
    """エラー発生時のコンテキスト情報。

    Attributes:
        action: 失敗したアクション。
        error_message: エラーメッセージ。
        retry_count: このサブタスク/アクションでの再試行回数。
    """

    action: Action
    error_message: str
    retry_count: int = 0
