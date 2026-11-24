"""LLMプロバイダファクトリ。

OpenAI 互換 API で全プロバイダを統一的に扱う。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.privacy import install_log_redaction, protect, protect_url

logger = logging.getLogger(__name__)

# プロバイダ設定: (api_key 環境変数, デフォルトモデル, base_url)
_PROVIDER_CONFIGS: dict[str, tuple[str, str, str | None]] = {
    "openai": ("OPENAI_API_KEY", "gpt-4o", None),
    "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-4-20250514", "https://api.anthropic.com/v1/"),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "anthropic/claude-sonnet-4",
        "https://openrouter.ai/api/v1",
    ),
    "opencode": (
        "OPENCODE_API_KEY",
        "deepseek-v4.1-flash",
        "https://opencode.ai/zen/v1",
    ),
    "opencode-go": (
        "OPENCODE_GO_API_KEY",
        "deepseek-v4.1-flash",
        "https://opencode.ai/zen/go/v1",
    ),
    "ollama": ("OLLAMA_API_KEY", "qwen2.5vl:3b", "http://localhost:11434/v1"),
}

# Go が求める独自 User-Agent（乱用監視用）。Zen 側にも付与して無害。
_USER_AGENT_HEADERS: dict[str, str] = {"User-Agent": "ai-desktop-agent/0.1.0"}
_PROVIDERS_WITH_UA: frozenset[str] = frozenset({"opencode", "opencode-go"})


_EXAMPLE_KEYS = {
    "sk-your-key-here",
    "sk-or-v1-your-key-here",
    "sk-ant-your-key-here",
    "your-key-here",
}


def _provider_key(provider: str, api_key: str | None = None) -> str:
    if provider not in _PROVIDER_CONFIGS:
        raise ValueError(
            f"未対応のLLMプロバイダです。対応プロバイダ: {', '.join(_PROVIDER_CONFIGS)}, mock"
        )
    key_env = _PROVIDER_CONFIGS[provider][0]
    value = api_key or os.environ.get(key_env)
    if not value and provider == "ollama":
        # Local inference only needs a placeholder for the OpenAI SDK.
        return "***"
    if not value or value in _EXAMPLE_KEYS or any(c.isspace() for c in value):
        raise ValueError(f"{key_env} に実際のAPIキーを設定してください。")
    return value


def validate_llm_configuration() -> None:
    """Validate local credentials before an update, without contacting a model service."""
    provider = os.environ.get("LLM_PROVIDER", "openai")
    if provider != "mock":
        _provider_key(provider)


def create_llm_provider(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """LLMプロバイダを生成する。

    provider 未指定時は LLM_PROVIDER 環境変数を参照。
    全プロバイダは OpenAICompatProvider で統一される。

    対応プロバイダ: openai, anthropic, openrouter, opencode, opencode-go, ollama, mock
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "openai")

    if provider == "mock":
        return MockLLMProvider(model=model or "mock-model")

    api_key = _provider_key(provider, api_key)
    from ai_desktop_agent.agent.llm.openai_compat_provider import (
        OpenAICompatProvider,
    )

    _, default_model, default_base_url = _PROVIDER_CONFIGS[provider]
    model = model or os.environ.get("LLM_MODEL") or default_model
    if provider == "ollama":
        base_url = base_url or os.environ.get("OLLAMA_BASE_URL")
    base_url = base_url or default_base_url

    if api_key:
        protect(api_key)
    if base_url:
        protect_url(base_url)
    install_log_redaction()
    logger.info("OpenAICompatProvider: provider=%s model=%s base_url=%s", provider, model, base_url)
    extra: dict[str, Any] = {}
    if provider in _PROVIDERS_WITH_UA:
        extra["default_headers"] = dict(_USER_AGENT_HEADERS)
    return OpenAICompatProvider(model=model, api_key=api_key, base_url=base_url, **extra)
