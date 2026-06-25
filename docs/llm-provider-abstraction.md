# ADR-001: Pluggable LLM Provider (Anthropic Claude **or** Google Gemini)

**Status:** Accepted · **Date:** 2026-06-25 · **Applies to:** `agent/loop.py` (ReAct loop)

## Context

The autonomous SWE agent's reasoning core is an LLM. We do **not** want the
agent loop hard-wired to one vendor. We want to switch between **Anthropic
Claude** and **Google Gemini** (and add others later) without touching the
ReAct loop, the tool definitions, or the retrieval layer.

Two consequences drive the design:

1. **The loop must speak one internal shape**, and a thin adapter per provider
   translates to/from each vendor's API.
2. **Token counting is provider-specific.** `agent/retrieval.py` therefore uses
   a cheap, provider-neutral *estimate* (`count_tokens`, ~3.5 chars/token) for
   context packing, and defers *exact* counts to the active provider's API at
   loop time. No provider tokenizer (and specifically not `tiktoken`, which is
   OpenAI's) lives in the retrieval hot path.

## Decision

A small `LLMProvider` interface. The loop depends only on this interface; each
provider is a self-contained adapter selected at runtime by config.

### Configuration (env / `.env`)

| Variable | Meaning | Example |
|---|---|---|
| `LLM_PROVIDER` | which adapter to load | `anthropic` \| `gemini` |
| `LLM_MODEL` | model id (provider-specific) | `claude-opus-4-8` / `gemini-2.5-pro` |
| `LLM_EFFORT` | reasoning effort (Claude) | `low`\|`medium`\|`high`\|`xhigh`\|`max` |
| `LLM_MAX_TOKENS` | output cap | `16000` |
| `ANTHROPIC_API_KEY` | Claude credential | — |
| `GEMINI_API_KEY` | Gemini credential | — |

Default provider is `anthropic`, default model `claude-opus-4-8` (most capable
for long-horizon agentic coding). The model and effort are **configurable**, per
the project decision — start on the strongest model, dial down for cost/volume.

### Interface

```python
# agent/providers/base.py
from dataclasses import dataclass
from typing import Protocol, Any

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]

@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str            # "end_turn" | "tool_use" | "max_tokens" | "refusal"
    usage: dict[str, int]       # input/output tokens (for the budget controller)
    raw: Any                    # provider-native message, echoed back verbatim next turn

class LLMProvider(Protocol):
    def complete(
        self,
        system: str,
        messages: list[dict],     # provider-neutral turn history
        tools: list[dict],        # JSON-schema tool specs (one canonical shape)
        max_tokens: int,
    ) -> LLMResponse: ...

    def count_tokens(self, system: str, messages: list[dict], tools: list[dict]) -> int: ...
```

`loop.py` runs a **manual agentic loop** against this: call `complete()`, if
`stop_reason == "tool_use"` execute each `ToolCall` (retrieve / read / edit /
run_tests), append the results, repeat until `end_turn` — enforcing the hard
step + token budget the README promises.

### Provider notes

**Anthropic (default, concrete).**
- SDK: `anthropic`; model `claude-opus-4-8`; **manual agentic loop** (not the
  auto tool-runner) so we keep budget caps, step logging, and human-gating.
- `client.messages.create(model=..., max_tokens=..., system=..., tools=...,
  messages=..., thinking={"type": "adaptive"},
  output_config={"effort": LLM_EFFORT})`.
- Tools: `{"name","description","input_schema"}` — this is our canonical shape;
  other adapters convert to/from it.
- **Always check `response.stop_reason` before reading content** — handle
  `tool_use`, `end_turn`, `max_tokens`, and `refusal`. Loop on `tool_use`,
  feeding `tool_result` blocks back as a user turn with matching `tool_use_id`.
- Exact counts: `client.messages.count_tokens(model=..., system=..., tools=...,
  messages=...)` — use for budget-critical checks, not per-step.
- Stream when `max_tokens` is large (≳16k) to avoid SDK HTTP timeouts.

**Google Gemini (alternate, interface-level).**
- SDK: `google-genai`. Map our canonical `complete()` onto
  `generate_content` with **function calling** (Gemini's tool/`FunctionDeclaration`
  shape); translate our `{name, description, input_schema}` tools into Gemini
  `FunctionDeclaration`s and Gemini `functionCall` parts back into `ToolCall`.
- Exact counts: Gemini's `count_tokens`.
- Confirm current model ids, the function-calling request/response shape, and
  token-counting call against Google's official `google-genai` docs before
  implementing — those specifics are not pinned here.

> The retrieval layer (`agent/retrieval.py`) is already provider-neutral: it
> embeds with a local sentence-transformer and estimates tokens locally, so it
> works unchanged under either provider.

## Consequences

- **+** Swap providers via one env var; the loop and tools are untouched.
- **+** Easy A/B of Claude vs Gemini on the same issue set (feeds `eval/`).
- **−** A canonical tool/message shape must be maintained, plus one adapter per
  provider. Worth it for vendor independence.

## Open items

- Pin exact Gemini model ids + function-calling shape when the adapter is built.
- Decide whether the budget controller counts estimated or exact tokens per step
  (estimate per step, reconcile with `usage` from each `LLMResponse`).
