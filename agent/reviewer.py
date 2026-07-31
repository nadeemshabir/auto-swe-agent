"""
agent/reviewer.py
The Reviewer agent — third stage of the multi-agent pipeline.

After the Coder produces a diff, the Reviewer examines it with a fresh context
(deliberately NOT sharing the Coder's conversation) and produces a structured
ReviewerOutput (approve / request_changes). If changes are requested, the
orchestrator feeds the concerns back to the Coder for another round (capped
at MAX_REVIEW_ROUNDS).

Design:
  - Fresh context: A model reviewing its own work in the same context tends
    to rubber-stamp it. A fresh context with adversarial framing catches more.
  - Single LLM call per review (not a ReAct loop).
  - Uses REVIEWER_PROVIDER / REVIEWER_MODEL env vars for per-agent config,
    falling back to PLANNER_PROVIDER / LLM_PROVIDER.
  - If JSON parsing fails, defaults to 'approve' — a parsing failure should
    not block the PR.

Offline self-test:  python -m agent.reviewer   (no API key needed)
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
    from .providers import ProviderError, Usage, get_provider
    from .schemas import PlannerOutput, ReviewerOutput
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.providers import ProviderError, Usage, get_provider
    from agent.schemas import PlannerOutput, ReviewerOutput

log = logging.getLogger("agent.reviewer")


# ── system prompt ────────────────────────────────────────────────────────────

REVIEWER_SYSTEM = """\
You are a senior code reviewer performing a correctness and security audit.

You are reviewing an automated fix for a GitHub issue. You have NOT seen the
conversation that produced this fix — you are a fresh pair of eyes.

You will receive:
1. The original issue
2. The repair plan that was followed
3. The git diff of the changes
4. Test results (pass/fail)

Your job is to find problems the Coder missed. Look for:
- Correctness: Does the fix actually address the root cause?
- Completeness: Are there edge cases not handled?
- Regressions: Could this break existing functionality?
- Security: Any new attack surface?
- Style: Major deviations from the repo's conventions?
- Test-first compliance: Does the diff include a new test that would have
  failed before the fix? If the plan included a test_strategy and the diff
  has no new tests, flag this as a concern.

Respond with ONLY a JSON object (no extra text, no markdown fences):
{
  "verdict": "approve" or "request_changes",
  "concerns": ["specific issue 1", "specific issue 2"],
  "confidence": "high" or "medium" or "low",
  "summary": "1-2 sentence overall assessment"
}

If the fix is correct and complete, verdict should be "approve" with an empty
concerns list. Be rigorous but not pedantic — don't request changes for style
nits or minor formatting in an automated fix.
"""


# ═════════════════════════════════════════════════════════════════════════════
# Provider helper
# ═════════════════════════════════════════════════════════════════════════════

def _accrue(budget, provider, usage) -> None:
    """Record one model call against the shared run Budget.

    Reviewer calls are real spend and must count toward the run's step/token/USD
    caps exactly like a Coder step (plan2.md §22 F4). `budget` is duck-typed so
    this module stays standalone for its offline self-test.
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
        log.warning("reviewer: could not record usage into budget", exc_info=True)


def _get_reviewer_provider():
    """Get the LLM provider for the Reviewer agent.

    Resolution order:
      1. REVIEWER_PROVIDER + REVIEWER_MODEL (explicit per-agent config)
      2. PLANNER_PROVIDER + PLANNER_MODEL (shared planner/reviewer config)
      3. LLM_PROVIDER + LLM_MODEL (global defaults)

    The Reviewer typically uses the same quality model as the Planner since
    review quality matters as much as planning quality.
    """
    reviewer_provider = os.getenv("REVIEWER_PROVIDER", "").strip()
    reviewer_model = os.getenv("REVIEWER_MODEL", "").strip()

    # If reviewer-specific config exists, use it.
    if reviewer_provider:
        kwargs = {}
        if reviewer_model:
            kwargs["model"] = reviewer_model
        return get_provider(name=reviewer_provider, **kwargs)

    # Fall back to planner config, then global config.
    planner_provider = os.getenv("PLANNER_PROVIDER", "").strip()
    planner_model = os.getenv("PLANNER_MODEL", "").strip()

    if planner_provider:
        kwargs = {}
        if reviewer_model:
            kwargs["model"] = reviewer_model
        elif planner_model:
            kwargs["model"] = planner_model
        return get_provider(name=planner_provider, **kwargs)

    # Global defaults.
    kwargs = {}
    if reviewer_model:
        kwargs["model"] = reviewer_model
    return get_provider(**kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# The Reviewer
# ═════════════════════════════════════════════════════════════════════════════

def run_reviewer(
    issue_text: str,
    plan: PlannerOutput,
    diff: str,
    test_output: str,
    *,
    provider=None,
    max_tokens: int = 1024,
    budget=None,
) -> tuple[ReviewerOutput, Usage]:
    """Run the Reviewer agent on the Coder's output.

    Parameters
    ----------
    issue_text : str
        The original task string (from Issue.to_task()).
    plan : PlannerOutput
        The Planner's structured plan that the Coder followed.
    diff : str
        The git diff of the Coder's changes. MUST be captured against a staged
        index (`git add -A && git diff --cached`) so files the Coder created —
        typically the new test — are visible. A plain `git diff` omits untracked
        files entirely (plan2.md §22 F2).
    test_output : str
        Real `run_tests` output from the Coder's trace — not its prose summary
        (plan2.md §22 F11).
    provider : LLMProvider, optional
        Override the provider. If None, uses REVIEWER_PROVIDER / LLM_PROVIDER.
    max_tokens : int
        Max output tokens for the LLM call.
    budget : Budget, optional
        Shared run budget. When given, the Reviewer's model call is accrued into
        it so Reviewer spend counts toward the run's caps (plan2.md §22 F4).

    Returns
    -------
    tuple[ReviewerOutput, Usage]
        The review verdict and token usage for budget tracking.
    """
    provider = provider or _get_reviewer_provider()

    # Build the user message with all context the Reviewer needs.
    parts = [
        "=== ORIGINAL ISSUE ===",
        issue_text,
        "",
        "=== REPAIR PLAN ===",
        f"Understanding: {plan.understanding}",
        f"Root cause hypothesis: {plan.root_cause_hypothesis}",
        f"Files to touch: {', '.join(plan.files_to_touch) if plan.files_to_touch else 'not specified'}",
        f"Plan steps:",
    ]
    for i, step in enumerate(plan.plan_steps, 1):
        parts.append(f"  {i}. {step}")
    parts.append(f"Risk notes: {plan.risk_notes}")

    parts.extend([
        "",
        "=== GIT DIFF ===",
        diff[:8000] if diff else "(no diff available)",
        "",
        "=== TEST RESULTS ===",
        test_output[:4000] if test_output else "(no test output available)",
    ])

    user_text = "\n".join(parts)
    messages = [provider.user_message(user_text)]

    try:
        resp = provider.complete(
            system=REVIEWER_SYSTEM,
            messages=messages,
            tools=[],  # Reviewer doesn't use tools
            max_tokens=max_tokens,
        )
    except ProviderError as e:
        log.error("reviewer: provider error: %s", e)
        # Fail open so the pipeline doesn't block, but flag the review as
        # unavailable so a reader can tell this apart from a real approval
        # (plan2.md §16 D18, §22 F15).
        return ReviewerOutput(
            verdict="approve",
            summary=f"Review skipped due to provider error: {e}",
            confidence="low",
            review_status="unavailable",
        ), Usage()

    _accrue(budget, provider, resp.usage)
    review = ReviewerOutput.from_llm_text(resp.text)

    log.info("reviewer: verdict=%s, confidence=%s, concerns=%d",
             review.verdict, review.confidence, len(review.concerns))
    return review, resp.usage


def build_feedback_task(
    issue_text: str,
    plan: PlannerOutput,
    review: ReviewerOutput,
    prior_diff: str = "",
    prior_summary: str = "",
) -> str:
    """Build a task string for the Coder that incorporates reviewer feedback.

    When the Reviewer requests changes, the orchestrator calls this to create
    the feedback prompt for a second pass.

    `prior_diff` and `prior_summary` matter: the Coder re-runs through a fresh
    ReAct loop with an empty message history, so without them it has no memory
    of what it just did and has to re-derive the whole change from scratch —
    burning budget that the run may not have, and risking undoing its own work
    (plan2.md §22 F13). Passing the diff makes the second pass an edit rather
    than a rediscovery.
    """
    lines = [
        "You previously made a fix, and a code reviewer has requested changes.",
        "The changes you already made are ALREADY APPLIED to the working tree —",
        "do not redo them. Read the diff below, then make only the additional",
        "edits needed to address the reviewer's concerns.",
        "",
        "=== REVIEWER FEEDBACK ===",
        f"Verdict: {review.verdict}",
        f"Confidence: {review.confidence}",
        f"Summary: {review.summary}",
    ]
    if review.concerns:
        lines.append("Concerns:")
        for i, concern in enumerate(review.concerns, 1):
            lines.append(f"  {i}. {concern}")

    if prior_summary:
        lines.extend(["", "=== WHAT YOU DID LAST ROUND ===", prior_summary.strip()])

    if prior_diff:
        lines.extend([
            "",
            "=== YOUR CHANGES SO FAR (already on disk) ===",
            prior_diff[:8000],
        ])

    lines.extend([
        "",
        "Address EACH concern listed above, then run the tests again.",
        "Keep the Red→Green evidence headings in your final summary if you have them.",
        "",
        "=== ORIGINAL CONTEXT ===",
        issue_text,
    ])
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise the Reviewer with a mock provider (no API key needed)."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("agent/reviewer.py self-test")
    print()

    # ── Mock provider ────────────────────────────────────────────────────

    class MockResponse:
        def __init__(self, text):
            self.text = text
            self.tool_calls = []
            self.stop_reason = "end_turn"
            self.usage = Usage(input_tokens=300, output_tokens=150)
            self.raw = None

    class MockProvider:
        name = "mock"
        model = "mock-model"

        def __init__(self, text):
            self._text = text

        def complete(self, *, system, messages, tools, max_tokens):
            return MockResponse(self._text)

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

    # ── Test plan for all tests ──────────────────────────────────────────
    test_plan = PlannerOutput(
        understanding="The issue reports a crash on empty input.",
        root_cause_hypothesis="parse() doesn't check for None.",
        files_to_touch=["src/parser.py"],
        plan_steps=["Add a None check"],
        test_strategy="Run pytest",
        risk_notes="None",
    )
    test_diff = "diff --git a/src/parser.py\n+    if data is None: return []\n"
    test_output = "exit code: 0\n1 passed"

    # ── Test 1: Approve ──────────────────────────────────────────────────
    print("approve verdict")
    approve_json = json.dumps({
        "verdict": "approve",
        "concerns": [],
        "confidence": "high",
        "summary": "LGTM. The fix correctly addresses the null case.",
    })
    review, usage = run_reviewer(
        "Fix the crash.", test_plan, test_diff, test_output,
        provider=MockProvider(approve_json),
    )
    check("verdict is approve", review.approved)
    check("no concerns", len(review.concerns) == 0)
    check("confidence is high", review.confidence == "high")
    check("usage tracked", usage.input_tokens == 300)

    # ── Test 2: Request changes ──────────────────────────────────────────
    print("request_changes verdict")
    changes_json = json.dumps({
        "verdict": "request_changes",
        "concerns": ["Missing edge case for empty string", "No test added"],
        "confidence": "medium",
        "summary": "The fix handles None but not empty string.",
    })
    review, usage = run_reviewer(
        "Fix the crash.", test_plan, test_diff, test_output,
        provider=MockProvider(changes_json),
    )
    check("verdict is request_changes", not review.approved)
    check("has 2 concerns", len(review.concerns) == 2)
    check("confidence is medium", review.confidence == "medium")

    # ── Test 3: Malformed JSON → defaults to approve ─────────────────────
    print("malformed JSON fallback")
    review, _ = run_reviewer(
        "Fix it.", test_plan, test_diff, test_output,
        provider=MockProvider("Looks good to me!"),
    )
    check("fallback → approve", review.approved)

    # ── Test 4: Provider error → approve ─────────────────────────────────
    print("provider error fallback")

    class FailingProvider(MockProvider):
        def complete(self, **kwargs):
            raise ProviderError("API is down")

    review, usage = run_reviewer(
        "Fix it.", test_plan, test_diff, test_output,
        provider=FailingProvider(""),
    )
    check("error → approve", review.approved)
    check("error → low confidence", review.confidence == "low")
    check("error → zero usage", usage.input_tokens == 0)

    # ── Test 5: build_feedback_task ──────────────────────────────────────
    print("feedback task builder")
    feedback_review = ReviewerOutput(
        verdict="request_changes",
        concerns=["Missing edge case", "No test"],
        confidence="medium",
        summary="Incomplete fix.",
    )
    feedback = build_feedback_task("Fix the crash.", test_plan, feedback_review)
    check("feedback mentions concerns", "Missing edge case" in feedback)
    check("feedback mentions verdict", "request_changes" in feedback)
    check("feedback includes original issue", "Fix the crash" in feedback)

    # ── Test 6: Provider resolution ──────────────────────────────────────
    print("provider resolution")
    check("_get_reviewer_provider is callable", callable(_get_reviewer_provider))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — reviewer approve, request_changes, fallback, feedback builder all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
