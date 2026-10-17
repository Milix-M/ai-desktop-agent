"""プラットフォーム別プロンプトのテスト（Android/iモード切替）。"""

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.openai_compat_provider import (
    PLATFORM_ANDROID,
    PLATFORM_LINUX,
    _system_prompt,
)
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


class TestSystemPrompt:
    def test_linux_is_default_without_android_section(self):
        prompt = _system_prompt(PLATFORM_LINUX)
        assert "Linux デスクトップ" in prompt
        assert "Android 端末" not in prompt

    def test_android_appends_android_section(self):
        prompt = _system_prompt(PLATFORM_ANDROID)
        assert "Linux デスクトップ" in prompt  # 基盤は共通
        assert "Android 端末" in prompt
        assert "Alt+F2" in prompt  # 禁止事項として言及
        assert "Quickstep" in prompt  # ホーム選択の案内
        assert "512, 730" in prompt  # ナビゲーションバーのタップ座標


class TestSessionPlatform:
    def test_android_vm_ids(self):
        for vm_id in ("android", "and-12345678"):
            s = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), vm_id=vm_id)
            assert s._is_android_vm() is True
            assert s._platform() == PLATFORM_ANDROID

    def test_linux_vm_ids(self):
        for vm_id in (None, "vm", "vm-12345678", "desktop", "desk-12345678"):
            s = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), vm_id=vm_id)
            assert s._is_android_vm() is False
            assert s._platform() == PLATFORM_LINUX


class TestPlatformThreading:
    @pytest.mark.asyncio
    async def test_decide_receives_android_platform(self):
        from ai_desktop_agent.agent.state import Subtask
        from ai_desktop_agent.vm.screenshot import Screenshot

        seen = {}

        class _SpyLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                seen.update(kwargs)
                return await super().decide_next_action(*args, **kwargs)

        llm = _SpyLLM()
        session = TaskSession(llm=llm, display=FakeDisplayBackend(), vm_id="and-abc")
        shot = FakeDisplayBackend().capture_screen()
        assert isinstance(shot, Screenshot)
        await session._decide_action(Subtask(id="s1", description="d"), shot)
        assert seen.get("platform") == PLATFORM_ANDROID

    @pytest.mark.asyncio
    async def test_decide_defaults_to_linux(self):
        from ai_desktop_agent.agent.state import Subtask

        seen = {}

        class _SpyLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                seen.update(kwargs)
                return await super().decide_next_action(*args, **kwargs)

        llm = _SpyLLM()
        session = TaskSession(llm=llm, display=FakeDisplayBackend(), vm_id="vm-abc")
        shot = FakeDisplayBackend().capture_screen()
        await session._decide_action(Subtask(id="s1", description="d"), shot)
        assert seen.get("platform") == PLATFORM_LINUX


class TestEmptyDecideRetry:
    """空応答の決定再試行テスト（履歴を汚さない）。"""

    @staticmethod
    def _click_decision():
        from ai_desktop_agent.actions.primitives import Action, ActionType
        from ai_desktop_agent.agent.llm.types import ActionDecision

        return ActionDecision(
            action=Action(action_type=ActionType.WAIT, params={"seconds": 1.0}),
            expected_effect="wait",
            confidence=0.9,
            reasoning="retry-ok",
        )

    @pytest.mark.asyncio
    async def test_recovers_after_transient_empties(self):
        from ai_desktop_agent.agent.state import Subtask

        calls = []

        class _FlakyLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                calls.append(1)
                if len(calls) <= 2:
                    raise ValueError("応答が空です")
                return TestEmptyDecideRetry._click_decision()

        session = TaskSession(llm=_FlakyLLM(), display=FakeDisplayBackend())
        shot = FakeDisplayBackend().capture_screen()
        out = await session._decide_action(Subtask(id="s1", description="d"), shot)
        assert out.reasoning == "retry-ok"
        assert len(calls) == 3
        assert session.loop.context.action_history == []

    @pytest.mark.asyncio
    async def test_gives_up_after_cap(self):
        from ai_desktop_agent.agent.state import Subtask
        from ai_desktop_agent.server.session import MAX_EMPTY_RETRIES

        calls = []

        class _DeadLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                calls.append(1)
                raise ValueError("応答が空です（空応答が連続）")

        session = TaskSession(llm=_DeadLLM(), display=FakeDisplayBackend())
        with pytest.raises(ValueError, match="空応答"):
            await session._decide_action(
                Subtask(id="s1", description="d"), FakeDisplayBackend().capture_screen()
            )
        assert len(calls) == MAX_EMPTY_RETRIES + 1

    @pytest.mark.asyncio
    async def test_non_empty_errors_propagate_immediately(self):
        from ai_desktop_agent.agent.state import Subtask

        calls = []

        class _BoomLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                calls.append(1)
                raise RuntimeError("API down")

        session = TaskSession(llm=_BoomLLM(), display=FakeDisplayBackend())
        with pytest.raises(RuntimeError, match="API down"):
            await session._decide_action(
                Subtask(id="s1", description="d"), FakeDisplayBackend().capture_screen()
            )
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_truncated_json_retries(self):
        """JSON破損（切断）は再試行し、回復すれば進む。"""
        import json as _json

        from ai_desktop_agent.agent.state import Subtask

        calls = []

        class _ChoppyLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                calls.append(1)
                if len(calls) <= 2:
                    raise _json.JSONDecodeError("Unterminated string", '{"a": ', 8)
                return TestEmptyDecideRetry._click_decision()

        session = TaskSession(llm=_ChoppyLLM(), display=FakeDisplayBackend())
        shot = FakeDisplayBackend().capture_screen()
        out = await session._decide_action(Subtask(id="s1", description="d"), shot)
        assert out.reasoning == "retry-ok"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_validation_errors_not_retried(self):
        """アクション検証エラー（決定的）は即時送出する。"""
        from ai_desktop_agent.agent.state import Subtask

        calls = []

        class _BadParamsLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                calls.append(1)
                raise ValueError("必須パラメータが不足しています")

        session = TaskSession(llm=_BadParamsLLM(), display=FakeDisplayBackend())
        with pytest.raises(ValueError, match="必須パラメータ"):
            await session._decide_action(
                Subtask(id="s1", description="d"), FakeDisplayBackend().capture_screen()
            )
        assert len(calls) == 1
