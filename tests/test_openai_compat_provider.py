"""OpenAICompatProvider と LLMProviderFactory のテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot

# ── OpenAICompatProvider ─────────────────────────────


class TestOpenAICompatProvider:
    """OpenAICompatProvider のユニットテスト（API はモック）。"""

    @pytest.fixture
    def provider(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            return OpenAICompatProvider(api_key="test-key")

    @staticmethod
    def _fake_screenshot() -> Screenshot:
        return Screenshot(image_bytes=b"\x89PNGfake", width=1024, height=768)

    def _make_mock_response(self, json_data: dict) -> MagicMock:
        """JSONデータを含むモックAPIレスポンスを作成。"""
        import json

        choice = MagicMock()
        choice.message.content = json.dumps(json_data)

        response = MagicMock()
        response.choices = [choice]
        return response

    def test_provider_name(self, provider):
        assert provider.provider_name == "openai_compat"

    def test_model_name_default(self, provider):
        assert provider.model_name == "gpt-4o"

    def test_custom_model_name(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            p = OpenAICompatProvider(model="custom-model", api_key="test-key")
        assert p.model_name == "custom-model"

    def test_base_url_passed(self):
        """base_url 付きで作成してもエラーにならない。"""
        with patch("openai.AsyncOpenAI", autospec=True):
            p = OpenAICompatProvider(api_key="test-key", base_url="https://openrouter.ai/api/v1")
        assert p.provider_name == "openai_compat"

    # ── understand_instruction ───────────────────────

    @pytest.mark.asyncio
    async def test_understand_instruction(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "intent": "spreadsheet_creation",
                    "target_application": "LibreOffice Calc",
                    "constraints": ["A列に日付"],
                    "reasoning": "スプレッドシート作成",
                }
            )
        )
        goal = Goal(description="レポート作成")
        result = await provider.understand_instruction(goal)
        assert result.intent == "spreadsheet_creation"
        assert result.target_application == "LibreOffice Calc"

    # ── decompose_task ───────────────────────────────

    @pytest.mark.asyncio
    async def test_decompose_task(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "subtasks": [
                        {
                            "id": "step_1",
                            "description": "アプリ起動",
                            "expected_outcome": "起動完了",
                        },
                    ],
                    "reasoning": "1ステップ",
                }
            )
        )
        goal = Goal(description="起動", intent="launch")
        result = await provider.decompose_task(goal, subtask_count=0)
        assert len(result.subtasks) == 1
        assert result.subtasks[0].id == "step_1"

    # ── decide_next_action ───────────────────────────

    @pytest.mark.asyncio
    async def test_decide_next_action_left_click(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "action_type": "left_click",
                    "params": {"x": 100, "y": 200},
                    "expected_effect": "クリック",
                    "confidence": 0.9,
                    "reasoning": "ボタンを押す",
                }
            )
        )
        goal = Goal(description="test")
        subtask = Subtask(id="s1", description="click")
        result = await provider.decide_next_action(goal, subtask, [], self._fake_screenshot())
        assert result.action.action_type == ActionType.LEFT_CLICK
        assert result.action.params == {"x": 100, "y": 200}

    @pytest.mark.asyncio
    async def test_decide_next_action_subtask_complete(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "action_type": "subtask_complete",
                    "params": {},
                    "expected_effect": "完了",
                    "confidence": 1.0,
                    "reasoning": "done",
                }
            )
        )
        result = await provider.decide_next_action(
            Goal(description="t"), Subtask(id="s1", description="d"), [], self._fake_screenshot()
        )
        assert result.action.action_type == ActionType.SUBTASK_COMPLETE

    @pytest.mark.asyncio
    async def test_decide_next_action_with_error(self, provider):
        from ai_desktop_agent.agent.llm.types import ErrorContext

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "action_type": "wait",
                    "params": {"seconds": 2.0},
                    "expected_effect": "待機",
                    "confidence": 0.8,
                    "reasoning": "retry after wait",
                }
            )
        )
        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK),
            error_message="timeout",
            retry_count=1,
        )
        result = await provider.decide_next_action(
            Goal(description="t"),
            Subtask(id="s1", description="retry"),
            [],
            self._fake_screenshot(),
            error_context=error,
        )
        assert result.action.action_type == ActionType.WAIT

    # ── verify_result ────────────────────────────────

    @pytest.mark.asyncio
    async def test_verify_result_success(self, provider):
        from ai_desktop_agent.agent.llm.types import ActionDecision

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {"success": True, "reasoning": "OK", "evidence": "画面に表示"}
            )
        )
        decision = ActionDecision(action=Action(action_type=ActionType.LEFT_CLICK))
        result = await provider.verify_result(decision, "クリックされる")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_verify_result_failure(self, provider):
        from ai_desktop_agent.agent.llm.types import ActionDecision

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {"success": False, "reasoning": "NG", "evidence": "変化なし"}
            )
        )
        decision = ActionDecision(action=Action(action_type=ActionType.LEFT_CLICK))
        result = await provider.verify_result(decision, "ダイアログ")
        assert result.success is False

    # ── recover_from_error ───────────────────────────

    @pytest.mark.asyncio
    async def test_recover_from_error(self, provider):
        from ai_desktop_agent.agent.llm.types import ErrorContext

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "strategy": "wait_and_retry",
                    "actions": [{"action_type": "wait", "params": {"seconds": 1.0}}],
                    "reasoning": "再試行",
                    "recoverable": True,
                }
            )
        )
        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK),
            error_message="not found",
            retry_count=0,
        )
        result = await provider.recover_from_error(error, [], Subtask(id="s1", description="d"))
        assert result.strategy == "wait_and_retry"
        assert result.recoverable is True

    @pytest.mark.asyncio
    async def test_recover_from_error_give_up(self, provider):
        from ai_desktop_agent.agent.llm.types import ErrorContext

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "strategy": "give_up",
                    "actions": [],
                    "reasoning": "諦める",
                    "recoverable": False,
                }
            )
        )
        error = ErrorContext(
            action=Action(action_type=ActionType.LEFT_CLICK),
            error_message="not found",
            retry_count=3,
        )
        result = await provider.recover_from_error(
            error, [], Subtask(id="s1", description="d", max_retries=3)
        )
        assert result.recoverable is False

    # ── JSON パース ──────────────────────────────────

    def test_parse_json_plain(self, provider):
        assert provider._parse_json('{"key": "value"}') == {"key": "value"}

    def test_parse_json_code_block(self, provider):
        assert provider._parse_json('```json\n{"key": "value"}\n```') == {"key": "value"}

    def test_parse_json_no_lang(self, provider):
        assert provider._parse_json('```\n{"key": "value"}\n```') == {"key": "value"}

    # ── 履歴フォーマット ────────────────────────────

    def test_format_empty(self, provider):
        assert "履歴なし" in provider._format_action_history([])

    def test_format_with_actions(self, provider):
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
        assert "timeout" in text

    # ── スクリーンショット ────────────────────────────

    @pytest.mark.asyncio
    async def test_decide_next_action_with_screenshot(self, provider):
        """decide_next_action にスクリーンショットを渡せる。"""
        from unittest.mock import AsyncMock

        from ai_desktop_agent.vm.screenshot import Screenshot

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {
                    "action_type": "left_click",
                    "params": {"x": 100, "y": 200},
                    "expected_effect": "クリック",
                    "confidence": 0.9,
                    "reasoning": "ボタンを押す",
                }
            )
        )
        goal = Goal(description="ファイルを開く", intent="file_management")
        subtask = Subtask(id="s1", description="ファイルマネージャーを起動")
        screenshot = Screenshot(image_bytes=b"\x89PNGfake", width=1024, height=768)

        result = await provider.decide_next_action(goal, subtask, [], screenshot=screenshot)
        assert result.action.action_type == ActionType.LEFT_CLICK

    @pytest.mark.asyncio
    async def test_call_with_image_bytes(self, provider):
        """_call に image_bytes を渡すと vision API リクエストになる。"""
        from unittest.mock import AsyncMock

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._make_mock_response(
                {"action_type": "wait", "params": {}, "confidence": 1.0, "reasoning": "ok"}
            )
        )
        data = await provider._call(
            "prompt",
            image_bytes=b"fake_png_data",
        )
        assert data["action_type"] == "wait"


# ── LLMProviderFactory ────────────────────────────────


class TestLLMProviderFactory:
    """create_llm_provider のテスト。"""

    def test_default_is_openai_requires_key(self):
        """デフォルトプロバイダはopenai。APIキー未設定時はエラー。"""
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="OPENAI_API_KEY"),
        ):
            create_llm_provider()

    def test_explicit_mock(self):
        assert isinstance(create_llm_provider(provider="mock"), MockLLMProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="未対応"):
            create_llm_provider(provider="unknown")

    # ── openai ───────────────────────────────────────

    def test_openai_with_api_key(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            p = create_llm_provider(provider="openai", api_key="sk-test")
        assert p.provider_name == "openai_compat"
        assert p.model_name == "gpt-4o"

    def test_openai_requires_key(self):
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                create_llm_provider(provider="openai")

    # ── anthropic (OpenAI互換経由) ───────────────────

    def test_anthropic_with_api_key(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            p = create_llm_provider(provider="anthropic", api_key="sk-test")
        assert p.provider_name == "openai_compat"
        assert "claude" in p.model_name

    def test_anthropic_requires_key(self):
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                create_llm_provider(provider="anthropic")

    # ── openrouter ───────────────────────────────────

    def test_openrouter_with_api_key(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            p = create_llm_provider(provider="openrouter", api_key="sk-or-test")
        assert p.provider_name == "openai_compat"
        assert "anthropic/" in p.model_name

    def test_openrouter_requires_key(self):
        with patch.dict("os.environ", {}, clear=True):  # noqa: SIM117
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                create_llm_provider(provider="openrouter")

    def test_openrouter_from_env(self):
        with (
            patch("openai.AsyncOpenAI", autospec=True),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-env"}),
        ):
            p = create_llm_provider(provider="openrouter")
        assert p.provider_name == "openai_compat"

    # ── ollama ───────────────────────────────────────

    def test_ollama(self):
        with patch("openai.AsyncOpenAI", autospec=True):
            p = create_llm_provider(provider="ollama")
        assert p.provider_name == "openai_compat"
        assert p.model_name == "llama3.2"

    # ── LLM_PROVIDER 環境変数 ─────────────────────────

    def test_provider_from_env(self):
        with patch.dict("os.environ", {"LLM_PROVIDER": "mock"}):
            assert isinstance(create_llm_provider(), MockLLMProvider)

    def test_llm_model_env(self):
        with (
            patch("openai.AsyncOpenAI", autospec=True),
            patch.dict("os.environ", {"LLM_MODEL": "custom-model"}),
        ):
            p = create_llm_provider(provider="openai", api_key="sk")
        assert p.model_name == "custom-model"
