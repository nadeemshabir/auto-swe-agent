"""
agent/providers
Pluggable LLM providers. The ReAct loop selects one via `get_provider()`,
driven by the LLM_PROVIDER env var (default "anthropic").

See docs/llm-provider-abstraction.md.
"""

from __future__ import annotations

import os

from .base import (
    LLMProvider,
    LLMResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    "get_provider",
    "LLMProvider",
    "LLMResponse",
    "ProviderError",
    "ToolCall",
    "ToolSpec",
    "Usage",
]


def get_provider(name: str | None = None, **kwargs) -> LLMProvider:
    """Return a configured provider. `name` defaults to $LLM_PROVIDER or
    'anthropic'. Extra kwargs (model, effort, …) pass through to the adapter."""
    name = (name or os.getenv("LLM_PROVIDER", "anthropic")).strip().lower()

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    if name == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(**kwargs)

    raise ProviderError(
        f"Unknown LLM_PROVIDER {name!r}. Supported: 'anthropic', 'gemini'."
    )
