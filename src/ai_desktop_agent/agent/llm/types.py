"""LLM プロバイダとエージェントがやりとりする型定義。"""

from __future__ import annotations

import dataclasses
from enum import StrEnum

from ai_desktop_agent.actions.primitives import Action
from ai_desktop_agent.agent.state import Subtask
from ai_desktop_agent.validation import validate_text

# ── 回復戦略 ────────────────────────────────


class RecoveryStrategy(StrEnum):
    """エラーからの回復戦略。"""

    WAIT_AND_RETRY = "wait_and_retry"
    ALTERNATIVE_APPROACH = "alternative_approach"
    REPLAN_SUBTASK = "replan_subtask"
    GIVE_UP = "give_up"


# ── 理解フェーズ ──────────────────────────────


@dataclasses.dataclass
class UnderstandingResult:
    """ユーザー指示の解析結果。"""

    intent: str
    target_application: str | None
    constraints: list[str]
    reasoning: str

    def __post_init__(self) -> None:
        validate_text(self.intent, "intent")
        if self.target_application is not None:
            validate_text(self.target_application, "target_application")
        if not isinstance(self.constraints, list):
            raise ValueError("constraintsは文字列の配列で指定してください")
        for constraint in self.constraints:
            validate_text(constraint, "constraints")
        validate_text(self.reasoning, "reasoning")


# ── 計画フェーズ ──────────────────────────────


@dataclasses.dataclass
class DecompositionResult:
    """タスク分解の結果。"""

    subtasks: list[Subtask]
    reasoning: str

    def __post_init__(self) -> None:
        if not isinstance(self.subtasks, list) or any(
            not isinstance(subtask, Subtask) for subtask in self.subtasks
        ):
            raise ValueError("subtasksはSubtaskの配列で指定してください")
        ids = [subtask.id for subtask in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("subtasksのidが重複しています")
        validate_text(self.reasoning, "reasoning")


# ── 実行フェーズ ──────────────────────────────


@dataclasses.dataclass
class ActionDecision:
    """次に実行すべきアクションの決定。

    action_type=REGION_SELECT の場合、まず領域ズームを要求している。
    その場合 params は {x, y, width, height} を含む。
    """

    action: Action
    expected_effect: str  # アクション後に期待される画面上の変化
    confidence: float  # 0.0〜1.0、座標の確信度
    reasoning: str
    is_region_zoom: bool = False  # 領域ズーム後の決定か

    repeats: int = 1  # 同じ方向キーを続けて押す回数

    working_note: str = ""  # 空文字なら既存のメモを維持する

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            raise ValueError("actionはActionで指定してください")
        validate_text(self.expected_effect, "expected_effect")
        if type(self.repeats) is not int or not 1 <= self.repeats <= 5:
            raise ValueError("方向キーの回数は1〜5回です")
        if self.repeats > 1 and (
            self.action.action_type.value != "key_press"
            or self.action.params.get("key") not in ("up", "down", "left", "right")
        ):
            raise ValueError("繰り返せるのは方向キーだけです")
        if type(self.confidence) not in (int, float) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence は 0.0〜1.0 の範囲である必要があります: {self.confidence}"
            )
        validate_text(self.reasoning, "reasoning")
        if type(self.is_region_zoom) is not bool:
            raise ValueError("is_region_zoomは真偽値で指定してください")


# ── 検証フェーズ ──────────────────────────────


@dataclasses.dataclass
class VerificationResult:
    """アクション結果の検証。"""

    success: bool
    reasoning: str
    evidence: str

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("successは真偽値で指定してください")
        validate_text(self.reasoning, "reasoning")
        validate_text(self.evidence, "evidence")


# ── エラー回復 ───────────────────────────────


@dataclasses.dataclass
class ErrorContext:
    """エラー発生時のコンテキスト。"""

    action: Action
    error_message: str
    retry_count: int


@dataclasses.dataclass
class RecoveryPlan:
    """エラーからの回復計画。"""

    strategy: RecoveryStrategy
    actions: list[Action]
    reasoning: str
    recoverable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, RecoveryStrategy):
            raise ValueError("strategyはRecoveryStrategyで指定してください")
        if not isinstance(self.actions, list) or any(
            not isinstance(action, Action) for action in self.actions
        ):
            raise ValueError("actionsはActionの配列で指定してください")
        validate_text(self.reasoning, "reasoning")
        if type(self.recoverable) is not bool:
            raise ValueError("recoverableは真偽値で指定してください")
