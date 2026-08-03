"""
agent/planner.py
The Planner agent — first stage of the multi-agent pipeline.

Given a GitHub issue and retrieved codebase context, the Planner produces a
structured analysis (PlannerOutput) that the Coder agent will follow. It makes
NO code edits — only reasons about what to change and why.

Design:
  - Single LLM call (not a ReAct loop): planning is a one-shot structured
    reasoning task, not an interactive tool-use conversation.
  - Uses its own provider instance, configurable via PLANNER_PROVIDER /
    PLANNER_MODEL env vars. Defaults to the global LLM_PROVIDER / LLM_MODEL
    so you can run the whole pipeline on a single provider (e.g. Gemini) or
    use a stronger model just for planning (e.g. Claude Opus).
  - Calls retrieval.assemble_context() to get the top-k relevant code chunks
    before prompting, so the Planner sees the actual codebase.
  - If JSON parsing fails on the first attempt, retries once with a nudge.
    If it still fails, falls back to using the raw text as understanding.
  - Records token usage so it counts toward the run's total budget.
  - Follow the project's self-test convention: python -m agent.planner

Offline self-test:  python -m agent.planner   (no API key needed)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Load .env (same pattern as agent/loop.py).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    from .providers import (
        ProviderError, Usage, get_fallback_for_role, get_provider_for_role,
    )
    from .schemas import PlannerOutput
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.providers import (
        ProviderError, Usage, get_fallback_for_role, get_provider_for_role,
    )
    from agent.schemas import PlannerOutput

log = logging.getLogger("agent.planner")

# ── configuration ────────────────────────────────────────────────────────────

# Retrieval settings for the Planner's codebase context.
PLANNER_RETRIEVAL_K = int(os.getenv("PLANNER_RETRIEVAL_K", "10"))
PLANNER_TOKEN_BUDGET = int(os.getenv("PLANNER_TOKEN_BUDGET", "6000"))

# ── system prompt ────────────────────────────────────────────────────────────

PLANNER_SYSTEM = """\
You are an expert software engineer analyzing a GitHub issue.

Your job is to produce a structured analysis and repair plan. You will NOT
write any code — a separate Coder agent will execute your plan.

You will receive:
1. The issue title, body, and labels
2. Relevant code snippets retrieved from the repository

Respond with ONLY a JSON object in this exact format (no extra text):
{
  "understanding": "2-3 sentence summary of what the issue reports",
  "root_cause_hypothesis": "your best guess at the root cause",
  "files_to_touch": ["path/to/file1.py", "path/to/file2.py"],
  "plan_steps": [
    "Step 1: ...",
    "Step 2: ..."
  ],
  "test_strategy": "Specific test to write FIRST: name the test file, function name, input, and expected behavior. Example: 'Add test_parse_empty_string() in tests/test_parser.py that calls parse_json(\"\") and asserts it returns None instead of raising IndexError.' Be concrete enough that a Coder agent can write the test immediately without guessing.",
  "risk_notes": "edge cases, potential breaking changes, or 'None'"
}

Be specific. Name exact functions, classes, and line ranges when possible.
If the issue is unclear, state your assumptions explicitly in risk_notes.
Do NOT wrap the JSON in markdown fences. Output only the JSON object.
"""

RETRY_NUDGE = """\
Your previous response was not valid JSON. Please respond with ONLY a JSON \
object matching the format described in the system prompt. No extra text, \
no markdown fences — just the raw JSON object starting with { and ending with }.
"""


# ═════════════════════════════════════════════════════════════════════════════
# Provider helper
# ═════════════════════════════════════════════════════════════════════════════

def _get_planner_provider():
    """Get the LLM provider for the Planner agent.

    Resolution lives in agent.providers.resolve_role — PLANNER_PROVIDER /
    PLANNER_MODEL, then LLM_PROVIDER / LLM_MODEL, then a hardcoded default that
    is never empty. This lets you run everything on one cheap model by default
    and override just the Planner to something stronger.
    """
    return get_provider_for_role("planner")


# ═════════════════════════════════════════════════════════════════════════════
# Retrieval helper
# ═════════════════════════════════════════════════════════════════════════════

def _accrue(budget, provider, usage) -> None:
    """Record one model call against the shared run Budget.

    Planner calls are real spend and must count toward the run's step/token/USD
    caps exactly like a Coder step — otherwise two of the three agents run
    uncapped and the reported cost understates reality (plan2.md §22 F4).

    `budget` is duck-typed (anything with `.record(usage, cost)`) so this module
    stays standalone and its offline self-test needs no Budget import.
    """
    if budget is None:
        return
    try:
        cost = provider.cost_usd(usage)
    except Exception:
        cost = 0.0   # a pricing glitch must never break a run
    try:
        budget.record(usage, cost)
    except Exception:
        log.warning("planner: could not record usage into budget", exc_info=True)


def _retrieve_context(workspace: str | Path, issue_text: str,
                      k: int = PLANNER_RETRIEVAL_K,
                      token_budget: int = PLANNER_TOKEN_BUDGET) -> str:
    """Retrieve relevant code chunks for the Planner's context.

    Uses the same retrieval pipeline as the Coder (agent/retrieval.py).
    If retrieval fails (deps missing, index not built), returns an empty
    string — the Planner can still produce a useful plan from the issue alone.
    """
    try:
        try:
            from . import retrieval
        except ImportError:
            from agent import retrieval
        ctx = retrieval.assemble_context(
            issue_text, repo=str(workspace), k=k, token_budget=token_budget,
        )
        return ctx or ""
    except Exception as e:
        log.warning("planner: retrieval failed (will plan without code context): %s", e)
        return ""


# ═════════════════════════════════════════════════════════════════════════════
# The Planner
# ═════════════════════════════════════════════════════════════════════════════

def run_planner(
    issue_text: str,
    workspace: str | Path,
    *,
    provider=None,
    retrieval_k: int = PLANNER_RETRIEVAL_K,
    retrieval_budget: int = PLANNER_TOKEN_BUDGET,
    max_tokens: int = 2048,
    skip_retrieval: bool = False,
    budget=None,
    fallback_provider=None,
) -> tuple[PlannerOutput, Usage]:
    """Run the Planner agent.

    Parameters
    ----------
    issue_text : str
        The task string (from Issue.to_task()).
    workspace : str | Path
        Path to the cloned repository.
    provider : LLMProvider, optional
        Override the provider. If None, uses PLANNER_PROVIDER / LLM_PROVIDER.
    retrieval_k : int
        Number of code chunks to retrieve for context.
    retrieval_budget : int
        Token budget for retrieved context.
    max_tokens : int
        Max output tokens for the LLM call.
    skip_retrieval : bool
        If True, skip retrieval (for testing or when the index isn't built).
        Leave this False in the real pipeline — a Planner that has not seen the
        codebase guesses `files_to_touch` from the issue title alone.
    budget : Budget, optional
        Shared run budget. When given, every Planner model call is accrued into
        it so Planner spend counts toward the run's caps (plan2.md §22 F4).
    fallback_provider : LLMProvider, optional
        Model to retry on when the configured one yields no usable plan. If
        omitted, one is resolved from FALLBACK_MODELS — but ONLY when `provider`
        was not supplied by the caller (§22 F24).

    Returns
    -------
    tuple[PlannerOutput, Usage]
        The structured plan and the token usage for budget tracking.
    """
    caller_supplied_provider = provider is not None
    provider = provider or _get_planner_provider()

    # Retrieve codebase context (unless skipped).
    code_context = ""
    if not skip_retrieval:
        code_context = _retrieve_context(workspace, issue_text,
                                          k=retrieval_k,
                                          token_budget=retrieval_budget)

    # Build the user message.
    parts = [issue_text]
    if code_context:
        parts.append(f"\n\n--- RELEVANT CODE FROM THE REPOSITORY ---\n{code_context}")
    user_text = "\n".join(parts)

    total_usage = Usage()

    # Attempt with the configured model, then — if that produced no usable plan
    # — once more on the fallback model. A model can be reachable but unusable:
    # overloaded (503), or emitting output the parser cannot use. Both used to
    # leave the Coder running with no plan_steps, no files_to_touch and no
    # test_strategy, which is worse than a duller model that answers cleanly
    # (plan2.md §22 F24).
    plan, usage, raw = _attempt(provider, user_text, max_tokens, budget, "configured")
    total_usage = total_usage + usage

    if not plan.plan_steps:
        # Only auto-resolve a fallback when WE chose the provider. An explicit
        # provider= means the caller wants that one specifically — silently
        # reaching for a different model would surprise them, and would make
        # offline tests fire real API calls the moment a key is in .env.
        fallback = fallback_provider
        if fallback is None and not caller_supplied_provider:
            fallback = get_fallback_for_role("planner")
        if fallback is not None:
            log.warning("planner: configured model produced no usable plan — "
                        "retrying on the fallback model")
            fb_plan, fb_usage, fb_raw = _attempt(
                fallback, user_text, max_tokens, budget, "fallback")
            total_usage = total_usage + fb_usage
            if fb_plan.plan_steps:
                plan, raw = fb_plan, fb_raw
            elif not plan.understanding:
                plan, raw = fb_plan, fb_raw

    if not plan.plan_steps:
        log.warning("planner: no model produced a parseable plan; "
                    "using raw text as understanding")
        plan = PlannerOutput(understanding=(raw or "No plan generated.").strip()[:2000])

    log.info("planner: produced plan with %d steps, %d files",
             len(plan.plan_steps), len(plan.files_to_touch))
    return plan, total_usage


def _attempt(provider, user_text: str, max_tokens: int, budget,
             label: str) -> tuple[PlannerOutput, Usage, str]:
    """One model's shot at producing a plan: initial call, then a reparse nudge.

    Returns (plan, usage, raw_text). `plan.plan_steps` being empty means this
    model did not produce anything usable — the caller decides whether to try
    another one.
    """
    model = getattr(provider, "model", "?")
    messages = [provider.user_message(user_text)]
    usage = Usage()

    try:
        resp = provider.complete(system=PLANNER_SYSTEM, messages=messages,
                                 tools=[], max_tokens=max_tokens)
    except ProviderError as e:
        log.error("planner[%s/%s]: provider error: %s", label, model, e)
        return PlannerOutput(), usage, ""

    usage = usage + resp.usage
    _accrue(budget, provider, resp.usage)
    plan = PlannerOutput.from_llm_text(resp.text)
    if plan.plan_steps:
        return plan, usage, resp.text

    # Unparseable — nudge once for raw JSON before giving up on this model.
    log.warning("planner[%s/%s]: unparseable output, nudging for raw JSON",
                label, model)
    try:
        messages.append(provider.assistant_turn(resp))
        messages.append(provider.user_message(RETRY_NUDGE))
        resp2 = provider.complete(system=PLANNER_SYSTEM, messages=messages,
                                  tools=[], max_tokens=max_tokens)
        usage = usage + resp2.usage
        _accrue(budget, provider, resp2.usage)
        plan2 = PlannerOutput.from_llm_text(resp2.text)
        if plan2.plan_steps:
            return plan2, usage, resp2.text
    except ProviderError as e:
        log.error("planner[%s/%s]: provider error on nudge: %s", label, model, e)

    return plan, usage, resp.text


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise the Planner with a mock provider (no API key needed)."""
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("agent/planner.py self-test")
    print()

    # ── Mock provider ────────────────────────────────────────────────────
    canned_response = json.dumps({
        "understanding": "The issue reports a crash when input is empty.",
        "root_cause_hypothesis": "The parse() function doesn't check for None.",
        "files_to_touch": ["src/parser.py", "tests/test_parser.py"],
        "plan_steps": [
            "Step 1: Add a None check at the top of parse()",
            "Step 2: Add a test for empty input",
        ],
        "test_strategy": "Run pytest tests/test_parser.py",
        "risk_notes": "None",
    })

    class MockUsage:
        input_tokens = 500
        output_tokens = 200

    class MockResponse:
        def __init__(self, text):
            self.text = text
            self.tool_calls = []
            self.stop_reason = "end_turn"
            self.usage = Usage(input_tokens=500, output_tokens=200)
            self.raw = None

    class MockProvider:
        name = "mock"
        model = "mock-model"

        def __init__(self, responses=None):
            self._responses = list(responses or [MockResponse(canned_response)])
            self._call_count = 0

        def complete(self, *, system, messages, tools, max_tokens):
            idx = min(self._call_count, len(self._responses) - 1)
            self._call_count += 1
            return self._responses[idx]

        def user_message(self, text):
            return {"role": "user", "content": text}

        def assistant_turn(self, resp):
            return {"role": "assistant", "content": resp.text}

        def tool_result_message(self, results):
            return {"role": "user", "content": "tool results"}

        def count_tokens(self, *, system, messages, tools):
            return 100

        def cost_usd(self, usage):
            return 0.001

    # ── Test 1: Happy path ───────────────────────────────────────────────
    print("happy path")
    with tempfile.TemporaryDirectory() as d:
        plan, usage = run_planner(
            "Fix the crash on empty input.",
            d,
            provider=MockProvider(),
            skip_retrieval=True,
        )
        check("understanding parsed", "crash" in plan.understanding.lower())
        check("files_to_touch has 2 files", len(plan.files_to_touch) == 2)
        check("plan_steps has 2 steps", len(plan.plan_steps) == 2)
        check("usage tracked", usage.input_tokens == 500)
        check("not empty", not plan.is_empty())

    # ── Test 2: Retry on bad JSON ────────────────────────────────────────
    print("retry on bad JSON")
    with tempfile.TemporaryDirectory() as d:
        plan, usage = run_planner(
            "Fix the crash.",
            d,
            provider=MockProvider(responses=[
                MockResponse("Here is my analysis..."),  # bad: no JSON
                MockResponse(canned_response),            # retry: good JSON
            ]),
            skip_retrieval=True,
        )
        check("retry succeeded", "crash" in plan.understanding.lower())
        check("usage accumulated", usage.input_tokens == 1000)  # 500 + 500

    # ── Test 3: Total failure → fallback ─────────────────────────────────
    print("total failure fallback")
    with tempfile.TemporaryDirectory() as d:
        plan, usage = run_planner(
            "Fix the crash.",
            d,
            provider=MockProvider(responses=[
                MockResponse("I'll look into this..."),  # no JSON
                MockResponse("Still working on it..."),  # no JSON on retry either
            ]),
            skip_retrieval=True,
        )
        check("fallback uses raw text", "look into" in plan.understanding.lower())
        check("plan is effectively 'empty' (no steps)", len(plan.plan_steps) == 0)

    # ── Test 4: round-trip to_dict/from_dict ─────────────────────────────
    print("serialization")
    with tempfile.TemporaryDirectory() as d:
        plan, _ = run_planner(
            "Fix it.",
            d,
            provider=MockProvider(),
            skip_retrieval=True,
        )
        d = plan.to_dict()
        restored = PlannerOutput.from_dict(d)
        check("round-trip preserves understanding", restored.understanding == plan.understanding)
        check("round-trip preserves files", restored.files_to_touch == plan.files_to_touch)

    # ── Test 5: provider resolution ──────────────────────────────────────
    print("provider resolution")
    # Can't test actual provider creation without API keys, but we can verify
    # the function exists and accepts the right args.
    check("_get_planner_provider is callable", callable(_get_planner_provider))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — planner parsing, retry, fallback, and serialization all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
