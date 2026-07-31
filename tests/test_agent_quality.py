"""
tests/test_agent_quality.py
Tests for the quality-tier findings — plan2.md §22 F9, F12, F13, F14, F15 (M10/M11).

These cover behaviour that was not *wrong* so much as useless: a guardrail that
could not match a path, evidence parsing that broke on reordering, a reviewer
whose failures were indistinguishable from approvals, and an index that leaked.

The harness lives in conftest.py.
"""

from __future__ import annotations

import pytest

from agent.loop import ReActAgent
from agent.reviewer import build_feedback_task
from agent.schemas import PlannerOutput, ReviewerOutput


# ── F14: Red/Green evidence extraction ───────────────────────────────────────

@pytest.mark.parametrize("text,red,green", [
    # The ordinary case.
    ("done\n### Red (before fix)\n1 failed\n### Green (after fix)\n1 passed\n",
     "1 failed", "1 passed"),
    # Short markers — the old code mis-computed the section end for these.
    ("### Red\nboom\n### Green\nfine\n", "boom", "fine"),
    # Green before Red: the old code produced garbage, since it assumed order.
    ("### Green (after fix)\nfine\n### Red (before fix)\nboom\n", "boom", "fine"),
    # Fenced blocks, including an info string.
    ("### Red\n```\nE assert\n```\n### Green\n```text\nok\n```\n",
     "E assert", "ok"),
    # Different heading level and trailing punctuation.
    ("## Red:\nboom\n## Green:\nfine\n", "boom", "fine"),
    # Only one half present.
    ("### Red (before fix)\nonly red\n", "only red", ""),
    # No evidence at all.
    ("just a prose summary", "", ""),
    ("", "", ""),
])
def test_extract_test_evidence_is_robust(text, red, green):
    """F14: marker-length arithmetic could run backwards or past the section."""
    evidence = ReActAgent.extract_test_evidence(text)
    assert evidence["red"] == red
    assert evidence["green"] == green


# ── F12: the files_to_touch guardrail ────────────────────────────────────────

def test_guardrail_does_not_fire_on_equivalent_paths(tmp_path):
    """F12: 'src/a.py', './src/a.py' and an absolute path are the same file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")

    agent = ReActAgent.__new__(ReActAgent)
    agent.workspace = tmp_path
    agent._files_to_touch = ["src/a.py"]

    assert agent._plan_deviation("src/a.py") is None
    assert agent._plan_deviation("./src/a.py") is None
    assert agent._plan_deviation(str(tmp_path / "src" / "a.py")) is None


def test_guardrail_flags_a_real_deviation(tmp_path):
    agent = ReActAgent.__new__(ReActAgent)
    agent.workspace = tmp_path
    agent._files_to_touch = ["src/a.py"]

    deviation = agent._plan_deviation("src/b.py")
    assert deviation is not None
    assert "src/b.py" in deviation


def test_guardrail_is_inert_without_a_plan(tmp_path):
    """An empty files_to_touch constrains nothing — don't flag every edit."""
    agent = ReActAgent.__new__(ReActAgent)
    agent.workspace = tmp_path

    agent._files_to_touch = None
    assert agent._plan_deviation("anything.py") is None
    agent._files_to_touch = []
    assert agent._plan_deviation("anything.py") is None


def test_plan_deviation_reaches_the_pr_body(pipeline):
    """F12: a log.warning reaches nobody; the reviewer and PR must see it."""
    # The plan does NOT list the test file the Coder creates.
    result, cap = pipeline(planner_files=["paginate.py"])

    assert result["pr_url"] == pipeline.PR_URL
    body = cap["pr_body"]
    assert "Plan deviations" in body, f"deviation not surfaced in the PR:\n{body}"
    assert "tests/test_paginate.py" in body


# ── F15: a failed review must not look like an approval ──────────────────────

def test_unavailable_review_is_flagged_in_the_pr(pipeline):
    """F15: the Reviewer fails open, so 'approve' alone is not evidence."""
    result, cap = pipeline(reviewer_unavailable=True)

    assert result["pr_url"] == pipeline.PR_URL, \
        "a failed review must not block the PR — it fails open by design"
    body = cap["pr_body"]
    assert "UNAVAILABLE" in body, \
        f"an unreviewed PR is indistinguishable from a reviewed one:\n{body}"
    assert "review it manually" in body.lower()


def test_healthy_review_is_not_flagged(pipeline):
    """The warning must not appear when review actually happened."""
    _, cap = pipeline()
    assert "UNAVAILABLE" not in cap["pr_body"]


def test_reviewer_parse_failure_marks_review_unavailable():
    """A verdict recovered from unparseable text is not a real verdict."""
    review = ReviewerOutput.from_llm_text("Looks good to me, ship it!")
    assert review.approved, "must still fail open"
    assert not review.was_reviewed
    assert review.review_status == "unavailable"

    good = ReviewerOutput.from_llm_text(
        '{"verdict":"approve","concerns":[],"confidence":"high","summary":"LGTM"}')
    assert good.was_reviewed


# ── F13: the Coder must remember its own prior work ──────────────────────────

def test_feedback_task_carries_the_prior_diff():
    """F13: the re-run starts from an empty history and re-derives everything."""
    plan = PlannerOutput(understanding="u", plan_steps=["s"])
    review = ReviewerOutput(verdict="request_changes", concerns=["handle empty input"])

    task = build_feedback_task(
        "Issue #42: fix paginate", plan, review,
        prior_diff="diff --git a/paginate.py b/paginate.py\n+ceil division",
        prior_summary="I changed paginate() to use ceiling division.",
    )

    assert "ceil division" in task, "the prior diff was not passed to the Coder"
    assert "ceiling division" in task, "the prior summary was not passed"
    assert "handle empty input" in task, "reviewer concerns missing"
    assert "ALREADY APPLIED" in task, \
        "the Coder is not told its earlier edits are already on disk"


# ── F9: the vector index must not leak ───────────────────────────────────────

def test_vector_index_is_dropped_on_cleanup(pipeline):
    """F9: each run indexes a unique path; without cleanup they accumulate."""
    _, cap = pipeline()

    assert cap["dropped_index"], \
        "the run's vectors were never dropped — the collection grows forever"
    assert cap["dropped_index"][0].endswith("repo"), cap["dropped_index"]
