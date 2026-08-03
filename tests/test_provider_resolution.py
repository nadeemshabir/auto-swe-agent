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


# ── thinking parts must not leak into resp.text (§22 F23) ────────────────────

def test_gemini_excludes_thinking_parts_from_text():
    """F23: reasoning models return the chain of thought as parts with
    thought=True. Concatenating those into resp.text fed the model's internal
    deliberation to the Planner's JSON parser, which then failed and fell back
    to raw text with every structured field empty."""
    from agent.providers.gemini_provider import GeminiProvider

    class P:
        def __init__(self, text, thought=False):
            self.text, self.thought = text, thought

    parts = [
        P("Maybe Random Utilities is a separate class. Let's plan: 1. Update", thought=True),
        P('{"understanding": "adds utility helpers", "plan_steps": ["step 1"]}'),
    ]

    # The extraction expression as used in GeminiProvider.complete()
    text = "".join(
        p.text for p in parts
        if getattr(p, "text", None) and not getattr(p, "thought", False)
    )

    assert "Let's plan" not in text, "thinking leaked into the answer text"
    assert text.startswith("{"), f"answer should be the bare JSON object, got: {text[:60]!r}"

    from agent.schemas import PlannerOutput
    plan = PlannerOutput.from_llm_text(text)
    assert plan.plan_steps == ["step 1"], "parsing failed on the cleaned text"
    assert plan.understanding == "adds utility helpers"


def test_planner_falls_back_visibly_when_given_thinking_text():
    """The failure mode this produced: everything empty except understanding."""
    from agent.schemas import PlannerOutput
    plan = PlannerOutput.from_llm_text("Maybe X. Let's plan: 1. Update `utils.py`")
    assert plan.understanding                      # raw text preserved
    assert plan.plan_steps == []                   # ...and nothing else populated
    assert plan.files_to_touch == []
    assert plan.is_empty() is False                # has text, so not "empty"


# ── fallback model when the configured one is unusable (§22 F24) ─────────────

class _Resp:
    def __init__(self, text):
        from agent.providers import Usage
        self.text, self.tool_calls, self.stop_reason = text, [], "end_turn"
        self.usage, self.raw = Usage(100, 50), None


class _Prov:
    name = "mock"

    def __init__(self, model, texts):
        self.model, self._texts, self.calls = model, list(texts), 0

    def complete(self, **kw):
        t = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        if isinstance(t, Exception):
            raise t
        return _Resp(t)

    def user_message(self, text):
        return {"role": "user", "content": text}

    def assistant_turn(self, r):
        return {"role": "assistant", "content": r.text}

    def tool_result_message(self, results):
        return {"role": "user", "content": "r"}

    def count_tokens(self, **kw):
        return 10

    def cost_usd(self, u):
        return 0.001


GOOD_JSON = ('{"understanding":"u","root_cause_hypothesis":"r",'
             '"files_to_touch":["a.py"],"plan_steps":["s1"],'
             '"test_strategy":"t","risk_notes":"n"}')


def test_planner_falls_back_when_configured_model_emits_garbage(tmp_path):
    """F24: a reasoning model that never returns JSON left the Coder with no
    plan at all. A duller model that answers cleanly is far better."""
    from agent.planner import run_planner

    primary = _Prov("preview-model", ["thinking out loud...", "still thinking..."])
    backup = _Prov("stable-model", [GOOD_JSON])

    plan, usage = run_planner("Fix it.", tmp_path, provider=primary,
                              fallback_provider=backup, skip_retrieval=True)

    assert plan.plan_steps == ["s1"], "fallback model's plan was not used"
    assert plan.files_to_touch == ["a.py"]
    assert primary.calls == 2, "primary should get its nudge before giving up"
    assert backup.calls == 1
    assert usage.input_tokens == 300, "usage from both models must be counted"


def test_planner_falls_back_when_configured_model_errors(tmp_path):
    """The 503-overloaded case: the model is reachable but unusable."""
    from agent.planner import run_planner
    from agent.providers import ProviderError

    primary = _Prov("preview-model", [ProviderError("503 UNAVAILABLE")])
    backup = _Prov("stable-model", [GOOD_JSON])

    plan, _ = run_planner("Fix it.", tmp_path, provider=primary,
                          fallback_provider=backup, skip_retrieval=True)
    assert plan.plan_steps == ["s1"]


def test_planner_does_not_fall_back_when_primary_succeeds(tmp_path):
    """The fallback must not cost a second call on the happy path."""
    from agent.planner import run_planner

    primary = _Prov("good-model", [GOOD_JSON])
    backup = _Prov("stable-model", [GOOD_JSON])

    plan, _ = run_planner("Fix it.", tmp_path, provider=primary,
                          fallback_provider=backup, skip_retrieval=True)
    assert plan.plan_steps == ["s1"]
    assert backup.calls == 0, "fallback was called despite the primary working"


def test_no_fallback_when_it_would_be_the_same_model(monkeypatch):
    """Retrying the identical call would just fail the same way."""
    from agent.providers import FALLBACK_MODELS, get_fallback_for_role
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("PLANNER_MODEL", FALLBACK_MODELS["gemini"])
    assert get_fallback_for_role("planner") is None
