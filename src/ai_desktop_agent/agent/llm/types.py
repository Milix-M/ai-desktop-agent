"""LLM プロバイダとエージェントがやりとりする型定義。"""

from __future__ import annotations

import dataclasses
from enum import StrEnum

from ai_desktop_agent.actions.primitives import Action

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


# ── 計画フェーズ ──────────────────────────────


@dataclasses.dataclass
class DecompositionResult:
    """タスク分解の結果。"""

    subtasks: list  # list[Subtask]
    reasoning: str


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

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence は 0.0〜1.0 の範囲である必要があります: {self.confidence}"
            )


# ── 検証フェーズ ──────────────────────────────


@dataclasses.dataclass
class VerificationResult:
    """アクション結果の検証。"""

    success: bool
    reasoning: str
    evidence: str


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
