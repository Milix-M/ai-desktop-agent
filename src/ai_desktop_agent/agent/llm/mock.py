"""テスト用モックLLMプロバイダ。

実際のAPI呼び出しを行わず、事前に設定された応答を返す。
"""

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.base import LLMProvider
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


class MockLLMProvider(LLMProvider):
    """テスト用のモックプロバイダ。

    各メソッドの戻り値を事前に設定して、エージェントループの
    テストを実際のLLM呼び出しなしで行える。
    """

    def __init__(
        self,
        model: str = "mock-model",
        understand_result: UnderstandingResult | None = None,
        decompose_result: DecompositionResult | None = None,
        decide_result: ActionDecision | None = None,
        verify_result: VerificationResult | None = None,
        recover_result: RecoveryPlan | None = None,
    ) -> None:
        self._model = model
        self._understand_result = understand_result
        self._decompose_result = decompose_result
        self._decide_result = decide_result
        self._verify_result = verify_result
        self._recover_result = recover_result
        self._decide_calls: list[tuple] = []

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model

    async def understand_instruction(self, goal: Goal) -> UnderstandingResult:
        if self._understand_result:
            return self._understand_result
        return UnderstandingResult(intent="unknown", reasoning="mock")

    async def decompose_task(self, goal: Goal, subtask_count: int) -> DecompositionResult:
        if self._decompose_result:
            return self._decompose_result
        return DecompositionResult(
            subtasks=[Subtask(id=f"step_{subtask_count + 1}", description=goal.description)],
            reasoning="mock decomposition",
        )

    async def decide_next_action(
        self,
        goal: Goal,
        current_subtask: Subtask,
        action_history: list[ActionRecord],
        error_context: ErrorContext | None = None,
        screenshot: Screenshot | None = None,
    ) -> ActionDecision:
        self._decide_calls.append((goal, current_subtask, len(action_history)))
        if self._decide_result:
            return self._decide_result
        return ActionDecision(
            action=Action(action_type=ActionType.SUBTASK_COMPLETE),
            reasoning="mock: サブタスク完了",
        )

    async def verify_result(
        self,
        action: ActionDecision,
        expected_effect: str,
    ) -> VerificationResult:
        if self._verify_result:
            return self._verify_result
        return VerificationResult(success=True, reasoning="mock: 検証成功")

    async def recover_from_error(
        self,
        error: ErrorContext,
        action_history: list[ActionRecord],
        subtask: Subtask,
    ) -> RecoveryPlan:
        if self._recover_result:
            return self._recover_result
        return RecoveryPlan(
            strategy="wait_and_retry",
            actions=[Action(action_type=ActionType.WAIT, params={"seconds": 1.0})],
            reasoning="mock: 再試行します",
        )

    @property
    def decide_call_count(self) -> int:
        """decide_next_action の呼び出し回数。"""
        return len(self._decide_calls)
