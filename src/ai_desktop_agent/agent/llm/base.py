"""LLMプロバイダの抽象インターフェース。

すべてのLLMプロバイダ（Anthropic, OpenAI, Google, Ollama, etc.）は
このインターフェースを実装する。
"""

from abc import ABC, abstractmethod

from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    ErrorContext,
    RecoveryPlan,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot


class LLMProvider(ABC):
    """LLMプロバイダの抽象基底クラス。

    エージェントループが必要とする4つの判断ポイントを提供する:
    1. 指示理解（UNDERSTANDING）
    2. タスク分解（PLANNING）
    3. アクション決定（EXECUTING）
    4. 結果検証（VERIFYING）
    5. エラー回復（RECOVERING）
    """

    # ── プロバイダ情報 ─────────────────────────────────

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """プロバイダ名（例: 'anthropic', 'openai', 'ollama'）。"""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """使用中のモデル名。"""
        ...

    # ── 判断メソッド ───────────────────────────────────

    @abstractmethod
    async def understand_instruction(self, goal: Goal) -> UnderstandingResult:
        """ユーザー指示を解析し、意図・制約を抽出する。

        UNDERSTANDING フェーズで呼ばれる。
        """
        ...

    @abstractmethod
    async def decompose_task(self, goal: Goal, subtask_count: int) -> DecompositionResult:
        """ゴールをサブタスクに分解する。

        PLANNING フェーズで呼ばれる。

        Args:
            goal: ユーザーのゴール。
            subtask_count: これまでに生成されたサブタスクの総数
                          （後続の分解でIDの連番を維持するため）。

        Returns:
            分解されたサブタスクのリスト。
        """
        ...

    @abstractmethod
    async def decide_next_action(
        self,
        goal: Goal,
        current_subtask: Subtask,
        action_history: list[ActionRecord],
        error_context: ErrorContext | None = None,
        screenshot: Screenshot | None = None,
    ) -> ActionDecision:
        """現在の状態から次のアクションを決定する。

        EXECUTING フェーズで呼ばれる。

        Args:
            goal: ユーザーのゴール。
            current_subtask: 現在実行中のサブタスク。
            action_history: 全アクションの実行履歴。
            error_context: エラー回復中の場合はエラー情報。
            screenshot: 現在のデスクトップ画面（PNG画像）。

        Returns:
            次に実行すべきアクションの決定。
        """
        ...

    @abstractmethod
    async def verify_result(
        self,
        action: ActionDecision,
        expected_effect: str,
    ) -> VerificationResult:
        """アクションの実行結果を検証する。

        VERIFYING フェーズで呼ばれる。

        Args:
            action: 実行したアクションの決定内容。
            expected_effect: アクション決定時に期待された効果。

        Returns:
            検証結果。
        """
        ...

    @abstractmethod
    async def recover_from_error(
        self,
        error: ErrorContext,
        action_history: list[ActionRecord],
        subtask: Subtask,
    ) -> RecoveryPlan:
        """エラーからの回復計画を生成する。

        RECOVERING フェーズで呼ばれる。

        Args:
            error: エラーコンテキスト。
            action_history: 全アクションの実行履歴。
            subtask: 現在のサブタスク。

        Returns:
            回復計画。
        """
        ...
