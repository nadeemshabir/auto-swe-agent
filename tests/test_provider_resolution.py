"""
tests/test_provider_resolution.py
Per-role provider/model configuration — agent/providers/resolve_role.

Each agent (Planner, Coder, Reviewer) can be pointed at its own provider and
model. The contract these tests pin down:

  1. role-specific vars win;
  2. otherwise the global LLM_* vars apply;
  3. otherwise a hardcoded default applies — resolution NEVER yields an empty
     provider or model, however half-filled the .env is;
  4. a model id is never inherited across providers (see F17 below).

(4) was a live bug: with LLM_PROVIDER=gemini and only PLANNER_PROVIDER=anthropic
set, the Planner resolved to the Anthropic adapter but inherited the *Gemini*
LLM_MODEL, sending "gemini-3.5-flash-lite" to the Anthropic API.
"""

from __future__ import annotations

import pytest

from agent.providers import (
    DEFAULT_MODELS,
    DEFAULT_PROVIDER,
    ROLES,
    ProviderError,
    resolve_role,
)

ALL_VARS = [
    "LLM_PROVIDER", "LLM_MODEL",
    "PLANNER_PROVIDER", "PLANNER_MODEL",
    "CODER_PROVIDER", "CODER_MODEL",
    "REVIEWER_PROVIDER", "REVIEWER_MODEL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a completely unconfigured environment."""
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


# ── rule 3: never empty ──────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ROLES)
def test_nothing_configured_yields_a_real_provider_and_model(role):
    provider, model = resolve_role(role)
    assert provider == DEFAULT_PROVIDER
    assert model == DEFAULT_MODELS[DEFAULT_PROVIDER]
    assert provider and model, "resolution must never return an empty string"


@pytest.mark.parametrize("role", ROLES)
def test_provider_set_but_model_blank_still_yields_a_model(role, monkeypatch):
    """A half-filled .env is the normal case, not an error."""
    monkeypatch.setenv(f"{role.upper()}_PROVIDER", "gemini")
    monkeypatch.setenv(f"{role.upper()}_MODEL", "   ")   # whitespace == unset
    provider, model = resolve_role(role)
    assert provider == "gemini"
    assert model == DEFAULT_MODELS["gemini"]


@pytest.mark.parametrize("role", ROLES)
def test_blank_global_vars_are_treated_as_unset(role, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "  ")
    monkeypatch.setenv("LLM_MODEL", "")
    provider, model = resolve_role(role)
    assert provider == DEFAULT_PROVIDER
    assert model == DEFAULT_MODELS[DEFAULT_PROVIDER]


# ── rule 2: global config applies to every role ──────────────────────────────

@pytest.mark.parametrize("role", ROLES)
def test_global_config_applies_to_all_roles(role, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-custom-1")
    assert resolve_role(role) == ("gemini", "gemini-custom-1")


# ── rule 1: role-specific wins ───────────────────────────────────────────────

def test_each_role_can_be_configured_independently(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "cheap-model")
    monkeypatch.setenv("PLANNER_MODEL", "strong-model")
    monkeypatch.setenv("REVIEWER_MODEL", "strong-model")

    # The intended production shape: cheap Coder, strong Planner and Reviewer.
    assert resolve_role("planner") == ("gemini", "strong-model")
    assert resolve_role("reviewer") == ("gemini", "strong-model")
    assert resolve_role("coder") == ("gemini", "cheap-model")


def test_coder_has_its_own_override(monkeypatch):
    """The Coder had no per-role config at all before this change."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CODER_PROVIDER", "anthropic")
    monkeypatch.setenv("CODER_MODEL", "coder-model-1")
    assert resolve_role("coder") == ("anthropic", "coder-model-1")
    # ...and the other roles are untouched by it.
    assert resolve_role("planner")[0] == "gemini"


def test_reviewer_no_longer_inherits_planner_config(monkeypatch):
    """Setting PLANNER_* used to silently reconfigure the Reviewer too."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("PLANNER_PROVIDER", "anthropic")
    monkeypatch.setenv("PLANNER_MODEL", "planner-only-model")

    assert resolve_role("planner") == ("anthropic", "planner-only-model")
    assert resolve_role("reviewer") == ("gemini", DEFAULT_MODELS["gemini"]), \
        "the Reviewer picked up the Planner's config"


# ── rule 4: a model id never crosses providers ───────────────────────────────

def test_model_is_not_inherited_across_providers(monkeypatch):
    """F17: an Anthropic-role Planner must not receive a Gemini model id."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("PLANNER_PROVIDER", "anthropic")   # no PLANNER_MODEL

    provider, model = resolve_role("planner")
    assert provider == "anthropic"
    assert model == DEFAULT_MODELS["anthropic"]
    assert "gemini" not in model.lower(), \
        f"a Gemini model id leaked into the Anthropic provider: {model}"


def test_same_provider_override_still_inherits_the_global_model(monkeypatch):
    """Redundantly naming the same provider must not discard LLM_MODEL."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-custom-1")
    monkeypatch.setenv("PLANNER_PROVIDER", "gemini")
    assert resolve_role("planner") == ("gemini", "gemini-custom-1")


# ── validation ───────────────────────────────────────────────────────────────

def test_unknown_provider_fails_loudly(monkeypatch):
    """A typo must not silently fall back to a provider nobody asked for."""
    monkeypatch.setenv("PLANNER_PROVIDER", "opeanai")   # typo on purpose
    with pytest.raises(ProviderError, match="Unknown LLM provider"):
        resolve_role("planner")


def test_unknown_role_is_rejected():
    with pytest.raises(ProviderError, match="Unknown agent role"):
        resolve_role("architect")


def test_provider_names_are_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("PLANNER_PROVIDER", "  Anthropic  ")
    assert resolve_role("planner")[0] == "anthropic"


def test_every_supported_provider_has_a_non_empty_default():
    """The floor of the resolution chain must actually exist."""
    assert DEFAULT_PROVIDER in DEFAULT_MODELS
    for provider, model in DEFAULT_MODELS.items():
        assert model and model.strip(), f"{provider} has no default model"
