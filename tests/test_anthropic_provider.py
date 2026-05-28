"""AnthropicProvider と LLMProviderFactory のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.anthropic_provider import AnthropicProvider
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask

# ── AnthropicProvider ─────────────────────────────────


class TestAnthropicProvider:
    """AnthropicProvider のユニットテスト（API はモック）。"""

    @pytest.fixture
    def provider(self):
        """APIキー不要の AnthropicProvider（モック注入）。"""
        with patch("anthropic.AsyncAnthropic", autospec=True):
            return AnthropicProvider(api_key="test-key")

    def _make_mock_response(self, json_data: dict) -> MagicMock:
        """JSONデータを含むモックAPIレスポンスを作成。"""
        import json

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = json.dumps(json_data)

        response = MagicMock()
        response.content = [text_block]
        return response

    def test_provider_name(self, provider):
        assert provider.provider_name == "anthropic"

    def test_model_name(self, provider):
        assert provider.model_name == "claude-sonnet-4-20250514"

    def test_custom_model_name(self):
        with patch("anthropic.AsyncAnthropic", autospec=True):
            p = AnthropicProvider(model="claude-opus-4-20250514", api_key="test-key")
        assert p.model_name == "claude-opus-4-20250514"

    def test_raises_without_api_key(self):
        """APIキーがない場合は ValueError。"""
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                AnthropicProvider()

    # ── understand_instruction ───────────────────────

    @pytest.mark.asyncio
    async def test_understand_instruction(self, provider):
        response = self._make_mock_response(
            {
                "intent": "spreadsheet_creation",
                "target_application": "LibreOffice Calc",
                "constraints": ["A列に日付", "B列に金額"],
                "reasoning": "スプレッドシート作成の指示",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(description="売上レポートを作成して")
        result = await provider.understand_instruction(goal)

        assert result.intent == "spreadsheet_creation"
        assert result.target_application == "LibreOffice Calc"
        assert "A列に日付" in result.constraints

    # ── decompose_task ───────────────────────────────

    @pytest.mark.asyncio
    async def test_decompose_task(self, provider):
        response = self._make_mock_response(
            {
                "subtasks": [
                    {
                        "id": "step_1",
                        "description": "LibreOffice Calcを起動",
                        "expected_outcome": "Calcが表示されている",
                    },
                    {
                        "id": "step_2",
                        "description": "データを入力",
                        "expected_outcome": "セルにデータが入力されている",
                    },
                ],
                "reasoning": "2ステップに分解",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(
            description="レポート作成",
            intent="spreadsheet_creation",
            target_application="LibreOffice Calc",
        )
        result = await provider.decompose_task(goal, subtask_count=0)

        assert len(result.subtasks) == 2
        assert result.subtasks[0].id == "step_1"
        assert result.subtasks[1].id == "step_2"

    # ── decide_next_action ───────────────────────────

    @pytest.mark.asyncio
    async def test_decide_next_action_mouse_move(self, provider):
        response = self._make_mock_response(
            {
                "action_type": "mouse_move",
                "params": {"x": 100, "y": 200},
                "expected_effect": "カーソルが(100,200)に移動",
                "confidence": 0.95,
                "reasoning": "メニューを開くため",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(description="テスト")
        subtask = Subtask(id="step_1", description="クリックする")
        result = await provider.decide_next_action(goal, subtask, [])

        assert result.action.action_type == ActionType.MOUSE_MOVE
        assert result.action.params == {"x": 100, "y": 200}
        assert result.confidence == 0.95

    @pytest.mark.asyncio
    async def test_decide_next_action_subtask_complete(self, provider):
        response = self._make_mock_response(
            {
                "action_type": "subtask_complete",
                "params": {},
                "expected_effect": "サブタスク完了",
                "confidence": 1.0,
                "reasoning": "全て完了したため",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(description="テスト")
        subtask = Subtask(id="step_1", description="完了")
        result = await provider.decide_next_action(goal, subtask, [])

        assert result.action.action_type == ActionType.SUBTASK_COMPLETE

    @pytest.mark.asyncio
    async def test_decide_next_action_with_error_context(self, provider):
        response = self._make_mock_response(
            {
                "action_type": "wait",
                "params": {"seconds": 2.0},
                "expected_effect": "2秒待機",
                "confidence": 0.8,
                "reasoning": "エラー後なので待機してから再試行",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(description="テスト")
        subtask = Subtask(id="step_1", description="再試行")
        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 0, "y": 0}),
            error_message="要素が見つかりません",
            retry_count=1,
        )
        result = await provider.decide_next_action(goal, subtask, [], error_context=error)

        assert result.action.action_type == ActionType.WAIT

    @pytest.mark.asyncio
    async def test_decide_next_action_with_history(self, provider):
        response = self._make_mock_response(
            {
                "action_type": "left_click",
                "params": {"x": 50, "y": 50},
                "expected_effect": "ボタンクリック",
                "confidence": 0.9,
                "reasoning": "前の移動後にクリック",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        goal = Goal(description="テスト")
        subtask = Subtask(id="step_1", description="クリック")
        history = [
            ActionRecord(
                action=Action(action_type=ActionType.MOUSE_MOVE, params={"x": 50, "y": 50}),
                success=True,
            )
        ]
        result = await provider.decide_next_action(goal, subtask, history)

        assert result.action.action_type == ActionType.LEFT_CLICK

    # ── verify_result ────────────────────────────────

    @pytest.mark.asyncio
    async def test_verify_result_success(self, provider):
        response = self._make_mock_response(
            {
                "success": True,
                "reasoning": "期待通りの結果",
                "evidence": "画面に期待したテキストが表示されている",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        decision = ActionDecision(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 10, "y": 20}),
            expected_effect="ボタンがクリックされる",
        )
        result = await provider.verify_result(decision, decision.expected_effect)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_verify_result_failure(self, provider):
        response = self._make_mock_response(
            {
                "success": False,
                "reasoning": "期待した変化がない",
                "evidence": "画面に変化なし",
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        decision = ActionDecision(
            action=Action(action_type=ActionType.LEFT_CLICK),
            expected_effect="ダイアログが開く",
        )
        result = await provider.verify_result(decision, decision.expected_effect)

        assert result.success is False

    # ── recover_from_error ───────────────────────────

    @pytest.mark.asyncio
    async def test_recover_from_error_wait_and_retry(self, provider):
        response = self._make_mock_response(
            {
                "strategy": "wait_and_retry",
                "actions": [{"action_type": "wait", "params": {"seconds": 2.0}}],
                "reasoning": "少し待って再試行",
                "recoverable": True,
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 0, "y": 0}),
            error_message="timeout",
            retry_count=0,
        )
        subtask = Subtask(id="step_1", description="操作")
        result = await provider.recover_from_error(error, [], subtask)

        assert result.strategy == "wait_and_retry"
        assert result.recoverable is True
        assert len(result.actions) == 1

    @pytest.mark.asyncio
    async def test_recover_from_error_give_up(self, provider):
        response = self._make_mock_response(
            {
                "strategy": "give_up",
                "actions": [],
                "reasoning": "3回失敗したため諦める",
                "recoverable": False,
            }
        )
        provider._client.messages.create = AsyncMock(return_value=response)

        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK),
            error_message="not found",
            retry_count=3,
        )
        subtask = Subtask(id="step_1", description="操作", max_retries=3)
        result = await provider.recover_from_error(error, [], subtask)

        assert result.recoverable is False

    # ── JSON パース ──────────────────────────────────

    def test_parse_json_plain(self, provider):
        result = provider._parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_code_block(self, provider):
        result = provider._parse_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_parse_json_code_block_no_lang(self, provider):
        result = provider._parse_json('```\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    # ── アクション履歴フォーマット ────────────────────

    def test_format_empty_history(self, provider):
        text = provider._format_action_history([])
        assert "履歴なし" in text

    def test_format_action_history(self, provider):
        history = [
            ActionRecord(
                action=Action(action_type=ActionType.MOUSE_MOVE, params={"x": 0, "y": 0}),
                success=True,
            ),
            ActionRecord(
                action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 2}),
                success=False,
                error_message="timeout",
            ),
        ]
        text = provider._format_action_history(history)
        assert "✓" in text
        assert "✗" in text
        assert "mouse_move" in text
        assert "left_click" in text
        assert "timeout" in text


# ── LLMProviderFactory ────────────────────────────────


class TestLLMProviderFactory:
    """create_llm_provider のテスト。"""

    def test_default_is_mock(self):
        provider = create_llm_provider()
        assert isinstance(provider, MockLLMProvider)

    def test_explicit_mock(self):
        provider = create_llm_provider(provider="mock")
        assert isinstance(provider, MockLLMProvider)

    def test_anthropic_requires_api_key(self):
        """APIキーがない場合、AnthropicProvider でエラーになる。"""
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                create_llm_provider(provider="anthropic")

    def test_anthropic_with_api_key(self):
        with patch("anthropic.AsyncAnthropic", autospec=True):
            provider = create_llm_provider(provider="anthropic", api_key="test-key")
        assert provider.provider_name == "anthropic"

    def test_anthropic_with_env_api_key(self):
        with (
            patch("anthropic.AsyncAnthropic", autospec=True),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}),
        ):
            provider = create_llm_provider(provider="anthropic")
        assert provider.provider_name == "anthropic"

    def test_anthropic_custom_model(self):
        with patch("anthropic.AsyncAnthropic", autospec=True):
            provider = create_llm_provider(
                provider="anthropic",
                api_key="test-key",
                model="claude-opus-4-20250514",
            )
        assert provider.model_name == "claude-opus-4-20250514"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="未対応のLLMプロバイダ"):
            create_llm_provider(provider="unknown_provider")

    def test_provider_from_env(self):
        with patch.dict("os.environ", {"LLM_PROVIDER": "mock"}):
            provider = create_llm_provider()
        assert isinstance(provider, MockLLMProvider)

    # ── OpenRouter ───────────────────────────────────

    def test_openrouter_requires_api_key(self):
        """OPENROUTER_API_KEY がない場合はエラー。"""
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                create_llm_provider(provider="openrouter")

    def test_openrouter_with_api_key(self):
        with patch("ai_desktop_agent.agent.llm.anthropic_provider.AsyncAnthropic"):
            provider = create_llm_provider(provider="openrouter", api_key="sk-or-test")
        assert provider.provider_name == "anthropic"
        assert "anthropic/" in provider.model_name

    def test_openrouter_with_env_key(self):
        with (
            patch("anthropic.AsyncAnthropic", autospec=True),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-env"}),
        ):
            provider = create_llm_provider(provider="openrouter")
        assert provider.provider_name == "anthropic"

    def test_openrouter_custom_model(self):
        with patch("anthropic.AsyncAnthropic", autospec=True):
            provider = create_llm_provider(
                provider="openrouter",
                api_key="sk-or-test",
                model="anthropic/claude-opus-4",
            )
        assert provider.model_name == "anthropic/claude-opus-4"

    # ── base_url parameter ───────────────────────────

    def test_anthropic_with_base_url(self):
        """base_url を指定してカスタムエンドポイントに接続。"""
        with patch("ai_desktop_agent.agent.llm.anthropic_provider.AsyncAnthropic") as mock_client:
            from ai_desktop_agent.agent.llm.anthropic_provider import (
                AnthropicProvider,
            )

            AnthropicProvider(
                api_key="test-key",
                base_url="https://custom-endpoint.example.com/v1",
            )
        mock_client.assert_called_once()
        _, kwargs = mock_client.call_args
        assert kwargs.get("base_url") == "https://custom-endpoint.example.com/v1"
