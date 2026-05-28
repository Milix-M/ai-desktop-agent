"""LLMプロバイダファクトリ。

OpenAI 互換 API で全プロバイダを統一的に扱う。
"""

from __future__ import annotations

import logging
import os

from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider

logger = logging.getLogger(__name__)

# プロバイダ設定: (api_key 環境変数, デフォルトモデル, base_url)
_PROVIDER_CONFIGS: dict[str, tuple[str, str, str | None]] = {
    "openai": ("OPENAI_API_KEY", "gpt-4o", None),
    "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-4-20250514", None),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "anthropic/claude-sonnet-4",
        "https://openrouter.ai/api/v1",
    ),
    "ollama": ("OLLAMA_API_KEY", "llama3.2", "http://localhost:11434/v1"),
}


def create_llm_provider(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """LLMプロバイダを生成する。

    provider 未指定時は LLM_PROVIDER 環境変数を参照。
    全プロバイダは OpenAICompatProvider で統一される。

    対応プロバイダ: openai, anthropic, openrouter, ollama, mock
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "mock")

    if provider == "mock":
        return MockLLMProvider(model=model or "mock-model")

    if provider not in _PROVIDER_CONFIGS:
        raise ValueError(
            f"未対応のLLMプロバイダです: {provider}。対応プロバイダ: {', '.join(_PROVIDER_CONFIGS)}"
        )

    from ai_desktop_agent.agent.llm.openai_compat_provider import (
        OpenAICompatProvider,
    )

    key_env, default_model, default_base_url = _PROVIDER_CONFIGS[provider]

    api_key = api_key or os.environ.get(key_env)
    if not api_key and provider != "ollama":  # Ollama はローカルなので API キー不要
        raise ValueError(
            f"{key_env} が設定されていません。環境変数またはコンストラクタで指定してください。"
        )

    model = model or os.environ.get("LLM_MODEL") or default_model
    base_url = base_url or default_base_url

    logger.info("OpenAICompatProvider: provider=%s model=%s base_url=%s", provider, model, base_url)
    return OpenAICompatProvider(model=model, api_key=api_key, base_url=base_url)
