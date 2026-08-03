"""
agent/providers/gemini_provider.py
Google Gemini adapter for the LLMProvider interface.

Uses the official `google-genai` SDK (`from google import genai`) with MANUAL
function calling — automatic function calling is disabled so the ReAct loop
keeps control of tool execution, budgets, and logging.

Mapping to the provider-neutral interface (agent/providers/base.py):
    messages              -> list[types.Content]  (roles: user | model | tool)
    ToolSpec              -> types.FunctionDeclaration(parameters_json_schema=...)
    response.function_calls -> ToolCall
    tool_result_message() -> Content(role="tool", parts=[Part.from_function_response])
    count_tokens()        -> client.models.count_tokens(...).total_tokens

Credentials resolve from GEMINI_API_KEY (or GOOGLE_API_KEY). Model defaults to
gemini-3.5-flash; override with $LLM_MODEL. Verify the exact model id is current
for your account — Gemini model names change often.
"""

from __future__ import annotations

import logging
import os
import time

from .base import LLMResponse, ProviderError, ToolCall, ToolSpec, Usage

log = logging.getLogger("agent.providers.gemini")

# Retry settings for transient API errors (503, 429, etc.)
# 1+2+4+8+16 = 31s of backoff was not enough for a sustained capacity outage:
# a run died after exhausting all five inside half a minute. Env-tunable so an
# outage can be ridden out without a redeploy.
_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "6"))
_RETRY_BASE_DELAY = float(os.getenv("GEMINI_RETRY_BASE_DELAY", "2.0"))
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


# Approximate input / output price per 1M tokens, for the budget USD cap only.
#
# VERIFY THESE against the provider's pricing page before trusting MAX_USD — the
# API exposes model ids and token limits, not rates, so they cannot be checked
# programmatically. They only need to be roughly right: their job is to stop a
# runaway run, not to bill anyone.
#
# Keep an entry for every model named in .env. A missing one is not harmless —
# see _DEFAULT_PRICING below.
PRICING: dict[str, tuple[float, float]] = {
    # pro tier
    "gemini-3.1-pro":          (1.25, 10.0),
    "gemini-3.1-pro-preview":  (1.25, 10.0),
    "gemini-3-pro-preview":    (1.25, 10.0),
    # flash tier
    "gemini-3.6-flash":        (0.30, 2.50),
    "gemini-3.5-flash":        (0.30, 2.50),
    "gemini-3-flash-preview":  (0.30, 2.50),
    # flash-lite tier
    "gemini-3.5-flash-lite":   (0.10, 0.40),
    "gemini-3.1-flash-lite":   (0.10, 0.40),
    # legacy (may still work for some accounts)
    "gemini-2.5-pro":          (1.25, 10.0),
    "gemini-2.5-flash":        (0.30, 2.50),
    "gemini-2.5-flash-lite":   (0.10, 0.40),
}

# Fallback for a model we have no entry for. Deliberately the PRO rate: an
# unknown model should be over-estimated, so the USD cap trips early rather than
# letting a run overspend on a model we mispriced.
#
# But over-estimating silently is its own bug. `gemini-3.5-flash-lite` was
# missing from the table above while being the configured model, so every run
# was costed at ~4x the real rate — inflating the PR footer and tripping MAX_USD
# far too early, with nothing in the logs to say so. Hence the warning.
_DEFAULT_PRICING = (1.25, 10.0)
_warned_models: set[str] = set()


class GeminiProvider:
    """Concrete LLMProvider backed by the official `google-genai` SDK."""

    name = "gemini"

    def __init__(self, model: str | None = None, **_: object) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:  # pragma: no cover - dependency guard
            raise ProviderError(
                "The 'google-genai' package is required for LLM_PROVIDER=gemini. "
                "Install it: pip install google-genai"
            ) from e

        self._genai = genai
        self._types = types
        self.model = model or os.getenv("LLM_MODEL", "gemini-3.5-flash")
        try:
            # Client() resolves GEMINI_API_KEY / GOOGLE_API_KEY from the env.
            self.client = genai.Client()
        except Exception as e:  # missing key, bad config, …
            raise ProviderError(f"could not initialize Gemini client: {e}") from e

    # ── core call ────────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str,
        messages: list,
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> LLMResponse:
        types = self._types
        try:
            config = types.GenerateContentConfig(
                system_instruction=system or None,
                max_output_tokens=max_tokens,
                tools=[self._tool(tools)] if tools else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
        except Exception as e:
            raise ProviderError(f"could not build Gemini request: {e}") from e

        last_err = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=messages, config=config,
                )
                break  # success
            except Exception as e:
                last_err = e
                # Check if the error contains a retryable HTTP status code.
                err_str = str(e)
                retryable = any(str(code) in err_str for code in _RETRYABLE_STATUS_CODES)
                if not retryable or attempt == _MAX_RETRIES:
                    raise ProviderError(f"Gemini API error: {e}") from e
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(
                    "Gemini transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, _MAX_RETRIES, delay, e,
                )
                time.sleep(delay)

        try:
            candidate = (resp.candidates or [None])[0]

            # text (collect from parts to avoid SDK warnings when only tool calls exist)
            #
            # Thinking parts are EXCLUDED. Gemini's reasoning models return the
            # chain of thought as parts carrying `thought=True`, alongside the
            # actual answer. Concatenating both put the model's internal
            # deliberation into resp.text, so the Planner and Reviewer — which
            # ask for a bare JSON object — received prose like
            #   "Maybe Random Utilities is a separate class... Let's plan: 1. ..."
            # JSON extraction then failed, both attempts fell back to raw text,
            # and every structured field came back empty (plan2.md §22 F23).
            #
            # It also silently inflated prompt size on every subsequent turn,
            # since assistant_turn echoes the content back.
            text = ""
            if candidate is not None and candidate.content and candidate.content.parts:
                text = "".join(
                    p.text for p in candidate.content.parts
                    if getattr(p, "text", None) and not getattr(p, "thought", False)
                )

            # tool calls
            tool_calls = []
            for i, fc in enumerate(resp.function_calls or []):
                tool_calls.append(ToolCall(id=f"{fc.name}-{i}", name=fc.name, args=dict(fc.args or {})))

            usage = Usage(
                input_tokens=getattr(resp.usage_metadata, "prompt_token_count", 0) or 0,
                output_tokens=getattr(resp.usage_metadata, "candidates_token_count", 0) or 0,
            )
            stop_reason = self._stop_reason(candidate, bool(tool_calls))
            raw = candidate.content if candidate is not None else None
        except Exception as e:
            raise ProviderError(f"could not parse Gemini response: {e}") from e

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=raw,
        )

    # ── message construction ──────────────────────────────────────────────────

    def user_message(self, text: str):
        types = self._types
        return types.Content(role="user", parts=[types.Part.from_text(text=text)])

    def assistant_turn(self, resp: LLMResponse):
        # resp.raw is the model's Content (role="model"); echo it back verbatim.
        return resp.raw

    def tool_result_message(self, results: list[tuple[ToolCall, str, bool]]):
        types = self._types
        parts = []
        for call, content, is_error in results:
            payload = {"error": content} if is_error else {"output": content}
            parts.append(types.Part.from_function_response(name=call.name, response=payload))
        return types.Content(role="user", parts=parts)

    # ── accounting ─────────────────────────────────────────────────────────────

    def count_tokens(self, *, system: str, messages: list, tools: list[ToolSpec]) -> int:
        try:
            r = self.client.models.count_tokens(model=self.model, contents=messages)
            return getattr(r, "total_tokens", 0) or 0
        except Exception as e:
            raise ProviderError(f"Gemini count_tokens error: {e}") from e

    def cost_usd(self, usage: Usage) -> float:
        prices = PRICING.get(self.model)
        if prices is None:
            prices = _DEFAULT_PRICING
            if self.model not in _warned_models:
                _warned_models.add(self.model)
                log.warning(
                    "no PRICING entry for %r — costing it at the pro rate %s. "
                    "Cost figures and the MAX_USD cap will be wrong for this "
                    "model; add it to PRICING in %s.",
                    self.model, _DEFAULT_PRICING, __name__,
                )
        p_in, p_out = prices
        return usage.input_tokens / 1e6 * p_in + usage.output_tokens / 1e6 * p_out

    # ── helpers ────────────────────────────────────────────────────────────────

    def _tool(self, specs: list[ToolSpec]):
        """One Tool carrying all function declarations."""
        types = self._types
        decls = []
        for s in specs:
            try:
                decls.append(types.FunctionDeclaration(
                    name=s.name, description=s.description,
                    parameters_json_schema=s.input_schema,
                ))
            except (TypeError, ValueError):
                # older google-genai used `parameters` for the schema dict
                decls.append(types.FunctionDeclaration(
                    name=s.name, description=s.description, parameters=s.input_schema,
                ))
        return types.Tool(function_declarations=decls)

    @staticmethod
    def _stop_reason(candidate, has_tool_calls: bool) -> str:
        if has_tool_calls:
            return "tool_use"
        if candidate is None:
            return "refusal"
        fr = getattr(candidate, "finish_reason", None)
        name = getattr(fr, "name", str(fr)).upper() if fr is not None else ""
        if name == "MAX_TOKENS":
            return "max_tokens"
        if name in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
            return "refusal"
        return "end_turn"
