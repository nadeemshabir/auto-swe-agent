"""
tests/test_orchestrator_critical.py
Integration tests for the multi-agent handoffs — plan2.md §22 F1-F4, F11 (M6).

The harness lives in conftest.py. Each test below was confirmed to FAIL against
the pre-fix code before being accepted — a test that passes both before and
after a fix proves nothing.

Run:  pytest tests/ -v
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── F1: the Planner must see the code ────────────────────────────────────────

def test_planner_runs_after_clone_and_index_with_retrieval(pipeline):
    """F1: a Planner that has not seen the repo guesses files_to_touch."""
    _, cap = pipeline()

    assert len(cap["planner_calls"]) == 1
    call = cap["planner_calls"][0]

    assert call["exists"], f"planner got a non-existent workspace: {call['workspace']}"
    assert call["is_git_repo"], "planner workspace is not the cloned repo"
    assert Path(call["workspace"]).resolve() != Path(".").resolve(), \
        "planner fell back to the current working directory"
    assert call["indexed_first"] is True, "planner ran before the index was built"
    assert call["kwargs"].get("skip_retrieval") is not True, \
        "retrieval was disabled — the planner is working from issue text alone"


# ── F2 / F11: the Reviewer must see the whole change ─────────────────────────

def test_reviewer_diff_includes_files_the_coder_created(pipeline):
    """F2: plain `git diff` omits untracked files, hiding the new test."""
    _, cap = pipeline()

    assert cap["reviewer_calls"], "reviewer never ran"
    diff = cap["reviewer_calls"][0]["diff"]

    assert diff, "reviewer received an empty diff"
    assert "tests/test_paginate.py" in diff, \
        f"the file the Coder CREATED is missing from the diff:\n{diff[:400]}"
    assert "test_last_page" in diff, "the new test's body is missing from the diff"


def test_reviewer_gets_real_test_output_not_prose(pipeline):
    """F11: the Reviewer was being handed the Coder's summary as 'TEST RESULTS'."""
    _, cap = pipeline()

    test_output = cap["reviewer_calls"][0]["test_output"]
    assert "exit code" in test_output, \
        f"not real run_tests output: {test_output[:200]!r}"
    assert "Fixed the off-by-one" not in test_output, \
        "the Coder's prose summary is being passed off as test output"


# ── F4: one budget across all three agents ───────────────────────────────────

def test_budget_covers_all_three_agents_without_double_counting(pipeline):
    """F4: Coder rounds were double-counted; Planner/Reviewer weren't counted."""
    result, _ = pipeline()

    # planner $1.00 + reviewer $0.50 + coder (3 calls x $0.20) = $2.10
    assert result["cost_usd"] == pytest.approx(2.10), \
        f"cost {result['cost_usd']} != planner + coder + reviewer"
    # 1000 + 500 + 600
    assert result["input_tokens"] + result["output_tokens"] == 2100
    # 1 planner + 3 coder + 1 reviewer, each counted once
    assert result["steps"] == 5, f"steps {result['steps']} — expected 5"


# ── F3: a good fix survives a failed review round ────────────────────────────

def test_failed_review_rerun_still_opens_a_pr(pipeline, monkeypatch):
    """F3: the re-run hit the budget wall and the round-1 fix was deleted."""
    # Cap steps so the round-1 Coder succeeds but the re-run cannot finish.
    monkeypatch.setenv("MAX_STEPS", "4")

    result, cap = pipeline(reviewer_verdicts=["request_changes", "approve"])

    assert result["pr_url"] == pipeline.PR_URL, \
        f"no PR opened — the good fix was discarded (status={result['status']})"
    assert result["status"] == "completed", \
        f"status leaked the failed re-run: {result['status']}"
    assert "Fixed the off-by-one" in cap.get("pr_body", ""), \
        "the PR body does not carry the round-1 fix"
