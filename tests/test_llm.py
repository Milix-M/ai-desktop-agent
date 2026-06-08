"""LLM プロバイダ型定義とモックのテスト。"""

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    ErrorContext,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot


def _fake_screenshot() -> Screenshot:
    return Screenshot(image_bytes=b"\x89PNGfake", width=1024, height=768)


def _fake_action() -> Action:
    return Action(action_type=ActionType.LEFT_CLICK)


class TestActionDecision:
    def test_create_valid(self):
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 100, "y": 200})
        decision = ActionDecision(
            action=action,
            expected_effect="メニューが開く",
            confidence=0.9,
            reasoning="メニューボタンをクリック",
        )
        assert decision.confidence == 0.9
        assert decision.expected_effect == "メニューが開く"

    def test_confidence_out_of_range_raises(self):
        action = _fake_action()
        with pytest.raises(ValueError, match="confidence"):
            ActionDecision(
                action=action,
                expected_effect="test",
                confidence=1.5,
                reasoning="test",
            )

    def test_negative_confidence_raises(self):
        action = _fake_action()
        with pytest.raises(ValueError, match="confidence"):
            ActionDecision(
                action=action,
                expected_effect="test",
                confidence=-0.1,
                reasoning="test",
            )

    def test_default_confidence_is_one(self):
        action = _fake_action()
        decision = ActionDecision(
            action=action,
            expected_effect="test",
            confidence=1.0,
            reasoning="test",
        )
        assert decision.confidence == 1.0


class TestVerificationResult:
    def test_success_result(self):
        result = VerificationResult(success=True, reasoning="OK", evidence="画面上に表示あり")
        assert result.success
        assert result.evidence == "画面上に表示あり"

    def test_failure_result(self):
        result = VerificationResult(
            success=False,
            reasoning="要素が見つからない",
            evidence="画面に変化なし",
        )
        assert not result.success


class TestRecoveryPlan:
    def test_recoverable_default(self):
        plan = RecoveryPlan(
            strategy=RecoveryStrategy.WAIT_AND_RETRY,
            actions=[_fake_action()],
            reasoning="再試行",
            recoverable=True,
        )
        assert plan.recoverable
        assert plan.strategy == RecoveryStrategy.WAIT_AND_RETRY

    def test_unrecoverable(self):
        plan = RecoveryPlan(
            strategy=RecoveryStrategy.GIVE_UP,
            actions=[],
            reasoning="回復不能",
            recoverable=False,
        )
        assert not plan.recoverable


class TestRecoveryStrategy:
    def test_all_strategies_exist(self):
        assert RecoveryStrategy.WAIT_AND_RETRY == "wait_and_retry"
        assert RecoveryStrategy.ALTERNATIVE_APPROACH == "alternative_approach"
        assert RecoveryStrategy.GIVE_UP == "give_up"


class TestErrorContext:
    def test_create(self):
        action = _fake_action()
        ctx = ErrorContext(action=action, error_message="timeout", retry_count=2)
        assert ctx.retry_count == 2


class TestDecompositionResult:
    def test_create(self):
        subtasks = [
            Subtask(id="s1", description="アプリ起動"),
            Subtask(id="s2", description="データ入力"),
        ]
        result = DecompositionResult(subtasks=subtasks, reasoning="2段階に分解")
        assert len(result.subtasks) == 2


class TestUnderstandingResult:
    def test_create_minimal(self):
        result = UnderstandingResult(
            intent="spreadsheet_creation",
            target_application=None,
            constraints=[],
            reasoning="test",
        )
        assert result.intent == "spreadsheet_creation"
        assert result.target_application is None

    def test_create_full(self):
        result = UnderstandingResult(
            intent="file_edit",
            target_application="LibreOffice Calc",
            constraints=["A列に日付"],
            reasoning="ファイル編集タスク",
        )
        assert len(result.constraints) == 1


# ── MockLLMProvider ─────────────────────────────


@pytest.mark.asyncio
class TestMockLLMProvider:
    async def test_default_understand(self):
        provider = MockLLMProvider()
        result = await provider.understand_instruction(Goal(description="test"))
        assert result.intent == "unknown"

    async def test_default_decompose(self):
        provider = MockLLMProvider()
        result = await provider.decompose_task(Goal(description="test"), 0)
        assert len(result.subtasks) == 1

    async def test_default_decide(self):
        provider = MockLLMProvider()
        result = await provider.decide_next_action(
            Goal(description="test"),
            Subtask(id="s1", description="test"),
            [],
            _fake_screenshot(),
        )
        assert result.action.action_type == ActionType.SUBTASK_COMPLETE

    async def test_default_verify(self):
        provider = MockLLMProvider()
        action = _fake_action()
        decision = ActionDecision(
            action=action,
            expected_effect="click",
            confidence=1.0,
            reasoning="click button",
        )
        result = await provider.verify_result(decision, "click")
        assert result.success

    async def test_default_recover(self):
        provider = MockLLMProvider()
        action = _fake_action()
        ctx = ErrorContext(action=action, error_message="fail", retry_count=0)
        result = await provider.recover_from_error(ctx, [], Subtask(id="s1", description="test"))
        assert result.recoverable

    async def test_custom_results(self):
        """モックにカスタム結果を設定できること。"""
        provider = MockLLMProvider(
            verify_result=VerificationResult(success=False, reasoning="NG", evidence="none"),
            recover_result=RecoveryPlan(
                strategy=RecoveryStrategy.GIVE_UP,
                actions=[],
                reasoning="give up",
                recoverable=False,
            ),
        )
        action = _fake_action()
        decision = ActionDecision(
            action=action,
            expected_effect="test",
            confidence=1.0,
            reasoning="test",
        )

        verify = await provider.verify_result(decision, "")
        assert not verify.success

        ctx = ErrorContext(action=action, error_message="x", retry_count=0)
        recovery = await provider.recover_from_error(ctx, [], Subtask(id="s1", description="test"))
        assert not recovery.recoverable

    async def test_decide_call_count(self):
        """decide_next_action の呼び出し回数が正しく記録されること。"""
        provider = MockLLMProvider()
        assert provider.decide_call_count == 0

        ss = _fake_screenshot()
        await provider.decide_next_action(
            Goal(description="test"),
            Subtask(id="s1", description="test"),
            [],
            ss,
        )
        assert provider.decide_call_count == 1

        await provider.decide_next_action(
            Goal(description="test2"),
            Subtask(id="s2", description="test2"),
            [],
            ss,
        )
        assert provider.decide_call_count == 2
