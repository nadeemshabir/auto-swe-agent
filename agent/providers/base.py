"""
agent/providers/base.py
Provider-neutral types and the LLMProvider interface the ReAct loop depends on.

The loop never imports a vendor SDK directly — it talks to an LLMProvider.
Each provider (Anthropic, Gemini, …) owns the translation to/from its native
wire format, including how a turn history is represented. The loop treats the
`messages` list as opaque and only ever extends it via provider helpers
(`user_message`, `assistant_turn`, `tool_result_message`).

See docs/llm-provider-abstraction.md (ADR-001).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# ── errors ──────────────────────────────────────────────────────────────────

class ProviderError(Exception):
    """Any failure talking to the model provider (network, API, bad config)."""


# ── value objects ───────────────────────────────────────────────────────────

@dataclass
class ToolSpec:
    """A tool the model may call. `input_schema` is JSON Schema."""
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Usage:
    """Token accounting for one model call."""
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class LLMResponse:
    """Normalized result of one model call."""
    text: str                       # concatenated text blocks (may be "")
    tool_calls: list[ToolCall]      # tools the model wants run this turn
    stop_reason: str                # "end_turn" | "tool_use" | "max_tokens"
                                    #  | "refusal" | "pause_turn" | ...
    usage: Usage
    raw: Any = None                 # native assistant content, echoed back verbatim


# ── interface ───────────────────────────────────────────────────────────────

@runtime_checkable
class LLMProvider(Protocol):
    """What the ReAct loop requires of any provider.

    A provider is responsible for: making the call, normalizing the response,
    constructing native-format message entries, counting tokens, and pricing.
    """

    name: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> LLMResponse:
        """Run one model turn and return a normalized LLMResponse.
        Must raise ProviderError on any unrecoverable API failure."""
        ...

    # message construction (native format, opaque to the loop) ----------------

    def user_message(self, text: str) -> Any:
        """Build a user-role turn from plain text."""
        ...

    def assistant_turn(self, resp: LLMResponse) -> Any:
        """Build the assistant-role turn to append, echoing `resp.raw` so
        thinking/tool_use blocks are preserved for same-model continuation."""
        ...

    def tool_result_message(
        self, results: list[tuple[ToolCall, str, bool]]
    ) -> Any:
        """Build one user-role turn carrying tool results.
        Each item is (call, content, is_error)."""
        ...

    # accounting --------------------------------------------------------------

    def count_tokens(
        self, *, system: str, messages: list[Any], tools: list[ToolSpec]
    ) -> int:
        """Exact prompt token count from the provider (budget-critical checks)."""
        ...

    def cost_usd(self, usage: Usage) -> float:
        """Approximate USD cost for the given usage on this model."""
        ...
