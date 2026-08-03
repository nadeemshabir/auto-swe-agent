"""
agent/providers
Pluggable LLM providers, and the per-role configuration that selects them.

Every agent in the pipeline (Planner, Coder, Reviewer) can be pointed at its own
provider and model, so a strong model can plan and review while a cheap one does
the mechanical coding turns — which is where most tokens go.

Configuration, per role:

    PLANNER_PROVIDER  / PLANNER_MODEL
    CODER_PROVIDER    / CODER_MODEL
    REVIEWER_PROVIDER / REVIEWER_MODEL

Resolution for each of the six values (see `resolve_role`):

    1. the role-specific variable, if set
    2. the global LLM_PROVIDER / LLM_MODEL, if set
    3. a hardcoded default that is never empty

Rule 3 is the important one: resolution always terminates at a real provider and
a real model, so a half-filled .env can never produce an empty model name.

One subtlety that used to be a live bug (plan2.md §22 F17): a MODEL NAME IS
PROVIDER-SPECIFIC. If the global config is Gemini and you override just
PLANNER_PROVIDER=anthropic, the Planner must NOT inherit the global Gemini
LLM_MODEL — that would send "gemini-3.5-flash-lite" to the Anthropic API. So
step 2 applies to the model only when the resolved provider matches the global
provider; otherwise resolution falls through to that provider's own default.

See docs/llm-provider-abstraction.md.
"""

from __future__ import annotations

import logging
import os

from .base import (
    LLMProvider,
    LLMResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)

log = logging.getLogger("agent.providers")

__all__ = [
    "get_provider",
    "get_provider_for_role",
    "resolve_role",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODELS",
    "ROLES",
    "LLMProvider",
    "LLMResponse",
    "ProviderError",
    "ToolCall",
    "ToolSpec",
    "Usage",
]


# ── defaults — the floor that resolution can never fall below ────────────────

DEFAULT_PROVIDER = "anthropic"

# One default model per provider. These MUST be non-empty: they are the last
# resort when nothing is configured.
#
# NOTE: these ids predate several provider releases and are pending validation
# (plan2.md DECISION D20). Update them here — this dict is the single source of
# truth for "what model do we use when nobody said" — not in the adapters.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-4-8",
    "gemini": "gemini-3.5-flash",
}

ROLES = ("planner", "coder", "reviewer")


def _env(name: str) -> str:
    """Read an env var, treating whitespace-only as unset."""
    return (os.getenv(name) or "").strip()


def _known(provider: str) -> str:
    """Normalize and validate a provider name, or raise."""
    provider = provider.strip().lower()
    if provider not in DEFAULT_MODELS:
        raise ProviderError(
            f"Unknown LLM provider {provider!r}. "
            f"Supported: {', '.join(sorted(DEFAULT_MODELS))}."
        )
    return provider


def resolve_role(role: str) -> tuple[str, str]:
    """Resolve (provider, model) for one agent role.

    Parameters
    ----------
    role : str
        One of ROLES — 'planner', 'coder', 'reviewer'.

    Returns
    -------
    tuple[str, str]
        A validated provider name and a non-empty model id. Never returns an
        empty string for either.

    Raises
    ------
    ProviderError
        If a configured provider name is not supported. A typo fails loudly
        here rather than silently falling back to a default the user did not
        ask for.
    """
    role = role.strip().lower()
    if role not in ROLES:
        raise ProviderError(f"Unknown agent role {role!r}. Expected one of {ROLES}.")
    prefix = role.upper()

    # The provider actually in effect globally, with the hardcoded floor applied.
    global_provider = _known(_env("LLM_PROVIDER") or DEFAULT_PROVIDER)

    role_provider = _env(f"{prefix}_PROVIDER")
    provider = _known(role_provider) if role_provider else global_provider

    model = _env(f"{prefix}_MODEL")
    if not model and provider == global_provider:
        # Safe to inherit: same provider, so a global model id is meaningful.
        model = _env("LLM_MODEL")
    if not model:
        model = DEFAULT_MODELS[provider]
        if role_provider:
            log.debug(
                "%s: %s_PROVIDER=%s with no %s_MODEL — using that provider's "
                "default %r rather than the global LLM_MODEL, which belongs to %s",
                role, prefix, provider, prefix, model, global_provider,
            )

    return provider, model


def get_provider_for_role(role: str, **kwargs) -> LLMProvider:
    """Build the configured provider for one agent role.

    Explicit kwargs win over resolution, so callers and tests can still force a
    specific model.
    """
    provider, model = resolve_role(role)
    kwargs.setdefault("model", model)
    log.info("%s agent -> provider=%s model=%s", role, provider, kwargs["model"])
    return get_provider(name=provider, **kwargs)


def get_provider(name: str | None = None, **kwargs) -> LLMProvider:
    """Return a configured provider.

    `name` defaults to $LLM_PROVIDER, then DEFAULT_PROVIDER. When no `model` is
    supplied, one is resolved the same way `resolve_role` does — inheriting
    $LLM_MODEL only when it belongs to the provider being built, otherwise
    falling back to that provider's own default. This keeps direct callers
    (`agent.loop`'s CLI, self-tests) safe from the cross-provider model mix-up
    described in the module docstring.
    """
    global_provider = _known(_env("LLM_PROVIDER") or DEFAULT_PROVIDER)
    provider = _known(name) if name else global_provider

    if not kwargs.get("model"):
        model = _env("LLM_MODEL") if provider == global_provider else ""
        kwargs["model"] = model or DEFAULT_MODELS[provider]

    if provider == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    if provider == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(**kwargs)

    # Unreachable: _known() already validated the name.
    raise ProviderError(f"Unsupported provider {provider!r}")
