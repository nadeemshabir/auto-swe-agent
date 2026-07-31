"""
agent/schemas.py
Structured output types for the multi-agent pipeline.

The Planner, Coder, and Reviewer agents each produce structured data that flows
through the orchestrator (workers/tasks.py). These dataclasses define the
contracts between agents — they are the handoff protocol.

Design notes:
  - Pure data, no LLM calls, no side effects.
  - JSON-serializable via asdict() for DB persistence and API responses.
  - Parsing helpers (from_json / from_llm_text) handle the messy reality of
    LLM output: markdown fences, extra text around the JSON block, missing
    fields, wrong types — all normalized here so the rest of the pipeline
    can trust the types.

Offline self-test:  python -m agent.schemas
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

log = logging.getLogger("agent.schemas")


# ═════════════════════════════════════════════════════════════════════════════
# JSON extraction helper
# ═════════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict | None:
    """Extract the first JSON object from LLM output.

    LLMs commonly wrap JSON in markdown fences (```json ... ```) or add
    explanatory text before/after.  This function handles all of that:
      1. Strip markdown code fences (```json ... ``` or ``` ... ```)
      2. Find the first { ... } block (greedy, handles nested braces)
      3. Parse as JSON

    Returns the parsed dict, or None if no valid JSON object is found.
    """
    if not text or not text.strip():
        return None

    # 1. Strip markdown fences.
    stripped = re.sub(r"```(?:json)?\s*\n?", "", text)
    stripped = stripped.strip()

    # 2. Find the outermost { ... } block.
    start = stripped.find("{")
    if start == -1:
        return None

    # Walk forward tracking brace depth to find the matching close.
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None

    return None


# ═════════════════════════════════════════════════════════════════════════════
# PlannerOutput
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class PlannerOutput:
    """The Planner agent's structured analysis of a GitHub issue.

    Fields map to the planner's system prompt contract (agent/planner.py).
    Every field has a safe default so partial / malformed LLM output
    degrades gracefully rather than crashing the pipeline.
    """
    understanding: str = ""
    """2-3 sentence summary of what the issue reports."""

    root_cause_hypothesis: str = ""
    """The Planner's best guess at why the bug exists."""

    files_to_touch: list[str] = field(default_factory=list)
    """Predicted file paths the Coder should modify."""

    plan_steps: list[str] = field(default_factory=list)
    """Ordered steps for the Coder to follow."""

    test_strategy: str = ""
    """Description of what tests to add or run to verify the fix."""

    risk_notes: str = ""
    """Edge cases, potential breaking changes, or caveats."""

    def to_dict(self) -> dict:
        """JSON-serializable dict for DB persistence and API responses."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PlannerOutput:
        """Reconstruct from a dict (e.g. loaded from DB JSON column)."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            understanding=str(data.get("understanding", "")),
            root_cause_hypothesis=str(data.get("root_cause_hypothesis", "")),
            files_to_touch=_ensure_str_list(data.get("files_to_touch")),
            plan_steps=_ensure_str_list(data.get("plan_steps")),
            test_strategy=str(data.get("test_strategy", "")),
            risk_notes=str(data.get("risk_notes", "")),
        )

    @classmethod
    def from_llm_text(cls, text: str) -> PlannerOutput:
        """Parse a PlannerOutput from raw LLM text output.

        Handles markdown fences, extra text around the JSON block, missing
        fields, and wrong types.  If parsing fails entirely, returns a
        PlannerOutput with only `understanding` populated from the raw text
        (better than nothing — the Coder still gets some context).
        """
        data = _extract_json(text)
        if data is not None:
            return cls.from_dict(data)

        # Fallback: couldn't extract JSON.  Use the raw text as understanding
        # so the pipeline doesn't lose the information entirely.
        log.warning("could not parse PlannerOutput JSON; using raw text as understanding")
        return cls(understanding=text.strip()[:2000])

    def is_empty(self) -> bool:
        """True if the plan has no meaningful content."""
        return not self.understanding and not self.plan_steps


# ═════════════════════════════════════════════════════════════════════════════
# ReviewerOutput
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ReviewerOutput:
    """The Reviewer agent's assessment of the Coder's diff.

    Fields map to the reviewer's system prompt contract (agent/reviewer.py).
    """
    verdict: str = "approve"
    """'approve' or 'request_changes'."""

    concerns: list[str] = field(default_factory=list)
    """Specific issues found in the diff."""

    confidence: str = "medium"
    """'high', 'medium', or 'low'."""

    summary: str = ""
    """1-2 sentence overall assessment."""

    review_status: str = "reviewed"
    """'reviewed' if a model actually judged the diff, 'unavailable' if the
    review could not be performed (provider error, unparseable output).

    The Reviewer deliberately fails OPEN — a broken reviewer must never block a
    PR — but that means `verdict` reads 'approve' on every degraded path. Without
    this field an unreviewed PR is indistinguishable from an approved one, which
    is exactly the silent degradation G8 forbids (plan2.md §16 D18, §22 F15)."""

    def to_dict(self) -> dict:
        """JSON-serializable dict for DB persistence and API responses."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ReviewerOutput:
        """Reconstruct from a dict (e.g. loaded from DB JSON column)."""
        if not isinstance(data, dict):
            return cls()
        verdict = str(data.get("verdict", "approve")).strip().lower()
        if verdict not in ("approve", "request_changes"):
            verdict = "approve"  # default-safe: don't block on garbage
        confidence = str(data.get("confidence", "medium")).strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        review_status = str(data.get("review_status", "reviewed")).strip().lower()
        if review_status not in ("reviewed", "unavailable"):
            review_status = "reviewed"
        return cls(
            verdict=verdict,
            concerns=_ensure_str_list(data.get("concerns")),
            confidence=confidence,
            summary=str(data.get("summary", "")),
            review_status=review_status,
        )

    @property
    def was_reviewed(self) -> bool:
        """True only if a model actually judged the diff."""
        return self.review_status == "reviewed"

    @classmethod
    def from_llm_text(cls, text: str) -> ReviewerOutput:
        """Parse a ReviewerOutput from raw LLM text output.

        If parsing fails entirely, returns a conservative 'approve' verdict —
        a parsing failure should not block the PR.
        """
        data = _extract_json(text)
        if data is not None:
            return cls.from_dict(data)

        # Fallback: couldn't extract JSON.  Default to approve so the pipeline
        # doesn't block — but mark the review as unavailable so nobody mistakes
        # this for a real approval (D18 / §22 F15).  The raw text is preserved
        # in the step trace.
        log.warning("could not parse ReviewerOutput JSON; approving as UNAVAILABLE")
        return cls(
            verdict="approve",
            summary=text.strip()[:500],
            confidence="low",
            review_status="unavailable",
        )

    @property
    def approved(self) -> bool:
        """Convenience: True if the verdict is 'approve'."""
        return self.verdict == "approve"


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_str_list(value) -> list[str]:
    """Coerce a value into a list of strings.  Handles None, a single string,
    a list of mixed types, etc."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise parsing, serialization, and edge cases."""
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("agent/schemas.py self-test")
    print()

    # ── JSON extraction ──────────────────────────────────────────────────
    print("JSON extraction")
    check("plain JSON", _extract_json('{"a": 1}') == {"a": 1})
    check("fenced JSON", _extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("text around JSON", _extract_json('Here is my plan:\n{"a": 1}\nDone!') == {"a": 1})
    check("nested braces", _extract_json('{"a": {"b": 2}}') == {"a": {"b": 2}})
    check("no JSON", _extract_json("no json here") is None)
    check("empty input", _extract_json("") is None)
    check("braces in string", _extract_json('{"a": "{ not nested }"}') == {"a": "{ not nested }"})

    # ── PlannerOutput ────────────────────────────────────────────────────
    print("PlannerOutput parsing")
    valid_json = json.dumps({
        "understanding": "The issue reports a crash.",
        "root_cause_hypothesis": "Off-by-one in pagination.",
        "files_to_touch": ["src/paginate.py"],
        "plan_steps": ["Step 1: Read file", "Step 2: Fix loop"],
        "test_strategy": "Run existing pagination tests.",
        "risk_notes": "None",
    })
    p = PlannerOutput.from_llm_text(valid_json)
    check("understanding parsed", p.understanding == "The issue reports a crash.")
    check("files_to_touch parsed", p.files_to_touch == ["src/paginate.py"])
    check("plan_steps parsed", len(p.plan_steps) == 2)
    check("is_empty false", not p.is_empty())

    # Markdown fences
    p2 = PlannerOutput.from_llm_text(f"```json\n{valid_json}\n```")
    check("fenced parsing works", p2.understanding == "The issue reports a crash.")

    # Fallback when JSON fails
    p3 = PlannerOutput.from_llm_text("I think the bug is in the parser module.")
    check("fallback preserves text", "parser module" in p3.understanding)
    check("fallback has no steps", p3.plan_steps == [])

    # Round-trip serialization
    d = p.to_dict()
    p4 = PlannerOutput.from_dict(d)
    check("round-trip preserves data", p4.understanding == p.understanding)

    # Empty / None handling
    p5 = PlannerOutput.from_dict({})
    check("empty dict → empty PlannerOutput", p5.is_empty())
    p6 = PlannerOutput.from_dict(None)
    check("None → empty PlannerOutput", p6.is_empty())

    # ── ReviewerOutput ───────────────────────────────────────────────────
    print("ReviewerOutput parsing")
    r = ReviewerOutput.from_llm_text(json.dumps({
        "verdict": "request_changes",
        "concerns": ["Missing edge case for empty input"],
        "confidence": "high",
        "summary": "The fix is incomplete.",
    }))
    check("verdict parsed", r.verdict == "request_changes")
    check("concerns parsed", len(r.concerns) == 1)
    check("confidence parsed", r.confidence == "high")
    check("approved property", not r.approved)

    # Approve path
    r2 = ReviewerOutput.from_llm_text(json.dumps({
        "verdict": "approve",
        "concerns": [],
        "confidence": "high",
        "summary": "LGTM.",
    }))
    check("approve verdict", r2.approved)

    # Invalid verdict → defaults to approve
    r3 = ReviewerOutput.from_dict({"verdict": "GARBAGE"})
    check("invalid verdict → approve", r3.approved)

    # Fallback when JSON fails
    r4 = ReviewerOutput.from_llm_text("Looks good to me, ship it!")
    check("fallback → approve", r4.approved)

    # ── _ensure_str_list ─────────────────────────────────────────────────
    print("_ensure_str_list")
    check("None → []", _ensure_str_list(None) == [])
    check("str → [str]", _ensure_str_list("hello") == ["hello"])
    check("empty str → []", _ensure_str_list("  ") == [])
    check("[int, str] → [str, str]", _ensure_str_list([1, "a"]) == ["1", "a"])
    check("[None] → []", _ensure_str_list([None]) == [])

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — all schema parsing, serialization, and edge cases work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
