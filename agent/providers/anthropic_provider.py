"""
agent/providers/anthropic_provider.py
Anthropic Claude adapter for the LLMProvider interface.

Default model: claude-opus-4-8 (most capable for long-horizon agentic coding).
Uses adaptive thinking + the effort parameter; both are degraded gracefully if
the configured model doesn't support them. Credentials resolve from the
environment (ANTHROPIC_API_KEY) — never hard-code a key.
"""

from __future__ import annotations

import os

from .base import (
    LLMResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)


# Input / output price per 1M tokens. Used only for the budget controller's
# USD cap — keep in sync with current pricing, treat as approximate.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-opus-4-7":  (5.0, 25.0),
    "claude-opus-4-6":  (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Models that accept output_config.effort and adaptive thinking. Anything not
# listed gets a plain request (no effort / no thinking) so we never 400.
_EFFORT_MODELS = {
    "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-4-6",
}
_ADAPTIVE_THINKING_MODELS = {
    "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-4-6",
}


class AnthropicProvider:
    """Concrete LLMProvider backed by the official `anthropic` SDK."""

    name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_retries: int = 4,
        timeout: float = 120.0,
    ) -> None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - dependency guard
            raise ProviderError(
                "The 'anthropic' package is required for LLM_PROVIDER=anthropic. "
                "Install it: pip install anthropic"
            ) from e

        self._anthropic = anthropic
        self.model = model or os.getenv("LLM_MODEL", "claude-opus-4-8")
        self.effort = effort or os.getenv("LLM_EFFORT", "high")
        # The SDK auto-retries 429/5xx with backoff; bump the ceiling a little.
        self.client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout)

    # ── core call ────────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str,
        messages: list,
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> LLMResponse:
        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=[self._tool_dict(t) for t in tools],
        )
        if self.model in _ADAPTIVE_THINKING_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
        if self.model in _EFFORT_MODELS:
            kwargs["output_config"] = {"effort": self.effort}

        resp = self._create_with_fallback(kwargs)

        text = "".join(
            getattr(b, "text", "") for b in resp.content if b.type == "text"
        )
        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in resp.content
            if b.type == "tool_use"
        ]
        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
        )
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
            raw=resp.content,
        )

    def _create_with_fallback(self, kwargs: dict):
        """Call messages.create; if the model rejects an optional param
        (effort/thinking), drop those and retry once. All other API errors
        become ProviderError."""
        try:
            return self.client.messages.create(**kwargs)
        except self._anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "effort" in msg or "thinking" in msg or "output_config" in msg:
                kwargs.pop("output_config", None)
                kwargs.pop("thinking", None)
                try:
                    return self.client.messages.create(**kwargs)
                except self._anthropic.APIError as e2:
                    raise ProviderError(f"Anthropic API error: {e2}") from e2
            raise ProviderError(f"Anthropic bad request: {e}") from e
        except self._anthropic.APIError as e:
            raise ProviderError(f"Anthropic API error: {e}") from e

    # ── message construction ──────────────────────────────────────────────────

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def assistant_turn(self, resp: LLMResponse) -> dict:
        # Echo native content (text + thinking + tool_use) so continuation works.
        return {"role": "assistant", "content": resp.raw}

    def tool_result_message(self, results: list[tuple[ToolCall, str, bool]]) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                    "is_error": is_error,
                }
                for (call, content, is_error) in results
            ],
        }

    # ── accounting ─────────────────────────────────────────────────────────────

    def count_tokens(self, *, system: str, messages: list, tools: list[ToolSpec]) -> int:
        try:
            r = self.client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=messages,
                tools=[self._tool_dict(t) for t in tools],
            )
            return r.input_tokens
        except self._anthropic.APIError as e:
            raise ProviderError(f"Anthropic count_tokens error: {e}") from e

    def cost_usd(self, usage: Usage) -> float:
        p_in, p_out = PRICING.get(self.model, (5.0, 25.0))
        return usage.input_tokens / 1e6 * p_in + usage.output_tokens / 1e6 * p_out

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _tool_dict(t: ToolSpec) -> dict:
        return {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
