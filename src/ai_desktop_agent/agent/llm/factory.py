"""LLMプロバイダファクトリ。

環境変数または明示的な指定に基づいて適切な LLMProvider を生成する。
"""

from __future__ import annotations

import logging
import os

from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider

logger = logging.getLogger(__name__)


def create_llm_provider(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> LLMProvider:
    """LLMプロバイダを生成する。

    provider が指定されない場合、環境変数 LLM_PROVIDER を参照する。
    model が指定されない場合、プロバイダのデフォルトモデルを使用する。

    Args:
        provider: プロバイダ名 ("anthropic", "openrouter", "mock" など)。
        model: 使用するモデル名。
        api_key: API キー（環境変数より優先）。

    Returns:
        生成された LLMProvider インスタンス。

    Raises:
        ValueError: 未対応のプロバイダが指定された場合。
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "mock")

    if provider in ("anthropic", "openrouter"):
        from ai_desktop_agent.agent.llm.claude_provider import ClaudeProvider

        if provider == "openrouter":
            api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise ValueError(
                    "OPENROUTER_API_KEY が設定されていません。"
                    "OpenRouter を使用する場合は環境変数で指定してください。"
                )
            model = model or os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4")
            logger.info("ClaudeProvider (OpenRouter 経由): model=%s", model)
            return ClaudeProvider(
                model=model,
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1",
            )

        # 直接 Anthropic API
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY が設定されていません。"
                "環境変数またはコンストラクタで指定してください。"
            )
        model = model or os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")
        return ClaudeProvider(model=model, api_key=api_key)

    if provider == "mock":
        return MockLLMProvider(model=model or "mock-model")

    raise ValueError(
        f"未対応のLLMプロバイダです: {provider}。対応プロバイダ: anthropic, openrouter, mock"
    )
