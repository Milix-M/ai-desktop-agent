"""目標達成検証（画像つき subtask 検証）のテスト。"""

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    ErrorContext,
    VerificationResult,
)
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot


def _complete_decision() -> ActionDecision:
    return ActionDecision(
        action=Action(action_type=ActionType.SUBTASK_COMPLETE),
        expected_effect="done",
        confidence=0.9,
        reasoning="done",
    )


class _VerifyLLM(MockLLMProvider):
    """verify_result の成否を固定し、decide 時の error_context を記録する。"""

    def __init__(self, verify_ok: bool):
        super().__init__(
            verify_result=VerificationResult(
                success=verify_ok, reasoning="mock-verify", evidence="mock"
            ),
            decide_result=_complete_decision(),
        )
        self.seen_errors: list[ErrorContext | None] = []

    async def decide_next_action(self, *args, **kwargs):
        self._decide_calls.append(args)
        return ActionDecision(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 10, "y": 20}),
            expected_effect="click",
            confidence=0.9,
            reasoning="click",
        )

    async def decide_with_capture(self, *args, **kwargs):
        self.seen_errors.append(kwargs.get("error_context"))
        return await MockLLMProvider.decide_next_action(self, *args, **kwargs)


def _session_with_complete(llm, subtask=None) -> TaskSession:
    """VERIFYING 状態で最終履歴が SUBTASK_COMPLETE のセッションを作る。"""
    s = TaskSession(llm=llm, display=FakeDisplayBackend())
    s.loop.start(Goal(description="g"))
    s.loop.understanding_done()
    s.loop.plan_ready([subtask or Subtask(id="s1", description="d")])
    # 決定→実行の代わりに履歴を直接記録（実操作1件＋完了宣言）
    s.loop.record_action(Action(action_type=ActionType.MOUSE_MOVE, params={"x": 1, "y": 1}), True)
    s.loop.record_action(Action(action_type=ActionType.SUBTASK_COMPLETE), True)
    s.loop.action_executed()  # EXECUTING → WAITING
    s.loop.wait_complete()  # WAITING → VERIFYING
    assert s.loop.state == AgentState.VERIFYING
    return s


class TestSubtaskVerification:
    @pytest.mark.asyncio
    async def test_verify_ok_advances(self):
        s = _session_with_complete(_VerifyLLM(verify_ok=True))
        await s._verify_phase()
        assert s.loop.state == AgentState.COMPLETED

    @pytest.mark.asyncio
    async def test_verify_ng_goes_recovering(self):
        s = _session_with_complete(_VerifyLLM(verify_ok=False))
        await s._verify_phase()
        assert s.loop.state == AgentState.RECOVERING
        assert s._pending_error is not None
        assert "期待結果" in s._pending_error.error_message

    @pytest.mark.asyncio
    async def test_verify_ng_cap_fails(self):
        llm = _VerifyLLM(verify_ok=False)
        s = _session_with_complete(llm, Subtask(id="s1", description="d", max_retries=0))
        await s._verify_phase()
        assert s.loop.state == AgentState.FAILED

    @pytest.mark.asyncio
    async def test_pending_error_reaches_next_decide(self):
        llm = _VerifyLLM(verify_ok=False)
        orig = llm.decide_next_action
        llm.decide_next_action = llm.decide_with_capture  # type: ignore[method-assign]
        s = _session_with_complete(llm)
        await s._verify_phase()  # RECOVERING + pending 設定
        assert s.loop.state == AgentState.RECOVERING
        s.loop.recover_retry()  # EXECUTING
        # _execute_phase 相当：pending が渡されることを直接確認
        err = s._pending_error
        assert err is not None
        await s._decide_action(s.loop.context.current_subtask, s.display.capture_screen(), err)
        assert llm.seen_errors and llm.seen_errors[-1] is err
        _ = orig


class TestVerifyWithImage:
    @pytest.mark.asyncio
    async def test_verify_sends_image(self):
        """screenshot 付き verify では画像が送られる。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from ai_desktop_agent.agent.llm.openai_compat_provider import (
            OpenAICompatProvider,
        )

        with patch("openai.AsyncOpenAI", autospec=True):
            provider = OpenAICompatProvider(api_key="k")
        import json as _json

        choice = MagicMock()
        choice.message.content = _json.dumps(
            {"success": True, "reasoning": "ok", "evidence": "window visible"}
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[choice])
        )
        ss = Screenshot(image_bytes=b"\x89PNGfake", width=1024, height=768)
        result = await provider.verify_result(
            _complete_decision(), "effect", screenshot=ss, expected_outcome="Firefoxが開く"
        )
        assert result.success is True
        kwargs = provider._client.chat.completions.create.call_args.kwargs
        content = kwargs["messages"][1]["content"]
        assert isinstance(content, list)
        assert any(p.get("type") == "image_url" for p in content)
        assert "Firefoxが開く" in content[0]["text"]

    @pytest.mark.asyncio
    async def test_verify_text_only_unchanged(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from ai_desktop_agent.agent.llm.openai_compat_provider import (
            OpenAICompatProvider,
        )

        with patch("openai.AsyncOpenAI", autospec=True):
            provider = OpenAICompatProvider(api_key="k")
        import json as _json

        choice = MagicMock()
        choice.message.content = _json.dumps(
            {"success": False, "reasoning": "ng", "evidence": "none"}
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[choice])
        )
        result = await provider.verify_result(_complete_decision(), "effect")
        assert result.success is False
        kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert isinstance(kwargs["messages"][1]["content"], str)
