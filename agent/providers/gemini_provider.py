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
gemini-2.5-pro; override with $LLM_MODEL. Verify the exact model id is current
for your account — Gemini model names change often.
"""

from __future__ import annotations

import os

from .base import LLMResponse, ProviderError, ToolCall, ToolSpec, Usage


# Approximate input / output price per 1M tokens, for the budget USD cap only.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro":   (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
}


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
        self.model = model or os.getenv("LLM_MODEL", "gemini-2.5-pro")
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
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            tools=[self._tool(tools)] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=messages, config=config,
            )
        except Exception as e:
            raise ProviderError(f"Gemini API error: {e}") from e

        candidate = (resp.candidates or [None])[0]

        # text (collect from parts to avoid SDK warnings when only tool calls exist)
        text = ""
        if candidate is not None and candidate.content and candidate.content.parts:
            text = "".join(p.text for p in candidate.content.parts if getattr(p, "text", None))

        # tool calls
        tool_calls = []
        for i, fc in enumerate(resp.function_calls or []):
            tool_calls.append(ToolCall(id=f"{fc.name}-{i}", name=fc.name, args=dict(fc.args or {})))

        usage = Usage(
            input_tokens=getattr(resp.usage_metadata, "prompt_token_count", 0) or 0,
            output_tokens=getattr(resp.usage_metadata, "candidates_token_count", 0) or 0,
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=self._stop_reason(candidate, bool(tool_calls)),
            usage=usage,
            raw=(candidate.content if candidate is not None else None),
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
        return types.Content(role="tool", parts=parts)

    # ── accounting ─────────────────────────────────────────────────────────────

    def count_tokens(self, *, system: str, messages: list, tools: list[ToolSpec]) -> int:
        try:
            r = self.client.models.count_tokens(model=self.model, contents=messages)
            return getattr(r, "total_tokens", 0) or 0
        except Exception as e:
            raise ProviderError(f"Gemini count_tokens error: {e}") from e

    def cost_usd(self, usage: Usage) -> float:
        p_in, p_out = PRICING.get(self.model, (1.25, 10.0))
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
