"""
tests/test_orchestrator_trace.py
Integration tests for run identity and trace durability — plan2.md §22 F5-F7,
F10 (M7).

These cover the machinery that makes a run *inspectable after the fact*: the id
a caller is handed must resolve to a database row, the step trace must actually
contain the tool calls, and re-running an issue must not destroy the previous
attempt's record.

The harness lives in conftest.py.

Run:  pytest tests/ -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ── F5: one run id, resolvable in the database ───────────────────────────────

def test_run_id_passed_in_is_the_id_persisted(pipeline):
    """F5: the task used to mint its own UUID, so the caller's id never matched."""
    run_id = str(uuid.uuid4())
    result, _ = pipeline(run_id=run_id)

    assert result["run_id"] == run_id, "the task ignored the caller's run_id"

    from db.models import Run
    with pipeline.session() as session:
        row = session.get(Run, uuid.UUID(run_id))

    assert row is not None, \
        "the id handed to the caller does not resolve to a runs row — " \
        "GET /runs/{id} and /runs/{id}/steps would both miss"
    assert row.repo == "octo/test"
    assert row.pr_url == pipeline.PR_URL


def test_run_id_falls_back_to_a_generated_uuid(pipeline):
    """A bare run_issue.delay(repo, n) with no id must still work."""
    result, _ = pipeline(run_id=None)
    assert uuid.UUID(result["run_id"]), "no usable run_id was produced"


# ── F6: the step trace must contain the tool calls ───────────────────────────

def test_run_steps_persist_tool_calls(pipeline):
    """F6: the writer read a key the loop never sets, so tools was always NULL."""
    run_id = str(uuid.uuid4())
    pipeline(run_id=run_id)

    from db.models import RunStep
    with pipeline.session() as session:
        steps = session.query(RunStep).filter_by(
            run_id=uuid.UUID(run_id)).order_by(RunStep.n).all()
        rows = [(s.agent_name, s.tools) for s in steps]

    assert rows, "no steps were persisted at all"

    coder_tools = [tools for agent, tools in rows if agent == "coder" and tools]
    assert coder_tools, \
        f"every Coder step persisted tools=NULL — the trace is hollow: {rows}"

    flat = [tc for tools in coder_tools for tc in tools]
    names = {tc["name"] for tc in flat}
    assert "edit_file" in names, f"edit_file missing from the trace: {names}"
    assert "run_tests" in names, f"run_tests missing from the trace: {names}"

    edit = next(tc for tc in flat if tc["name"] == "edit_file")
    assert edit["args"].get("path") == "tests/test_paginate.py", \
        "tool args were not persisted faithfully"
    assert edit["is_error"] is False


def test_all_three_agents_appear_in_the_trace(pipeline):
    """Per-agent observability is the reason we kept one Celery task (D14)."""
    run_id = str(uuid.uuid4())
    pipeline(run_id=run_id)

    from db.models import RunStep
    with pipeline.session() as session:
        agents = [s.agent_name for s in session.query(RunStep).filter_by(
            run_id=uuid.UUID(run_id)).order_by(RunStep.n).all()]

    assert agents[0] == "planner", f"planner is not the first step: {agents}"
    assert "coder" in agents
    assert agents[-1] == "reviewer", f"reviewer is not the last step: {agents}"


# ── F7 / F10: append-only runs, history preserved ────────────────────────────

def test_rerunning_an_issue_preserves_the_previous_run(pipeline):
    """F7/F10: re-runs mutated the PK in place, orphaning the old run's steps."""
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())

    pipeline(run_id=first_id, name="first")
    pipeline(run_id=second_id, name="second")

    from db.models import Run, RunStep
    with pipeline.session() as session:
        runs = session.query(Run).filter_by(repo="octo/test", issue_number=42).all()
        ids = {str(r.id) for r in runs}
        first_steps = session.query(RunStep).filter_by(
            run_id=uuid.UUID(first_id)).count()
        second_steps = session.query(RunStep).filter_by(
            run_id=uuid.UUID(second_id)).count()

    assert len(runs) == 2, f"re-running overwrote history — {len(runs)} row(s) kept"
    assert ids == {first_id, second_id}
    assert first_steps > 0, "the first run's steps were deleted by the re-run"
    assert second_steps > 0, "the second run persisted no steps"


def test_second_run_is_skipped_while_one_is_active(pipeline):
    """Idempotency: never two agents working the same issue at once (G6)."""
    from db.models import Run

    # Seed an in-flight run for this issue.
    active_id = uuid.uuid4()
    with pipeline.session() as session:
        session.add(Run(id=active_id, repo="octo/test", issue_number=42,
                        status="running"))
        session.commit()

    result, cap = pipeline()

    assert result["status"] == "skipped_duplicate", \
        f"a concurrent run was allowed to start: {result['status']}"
    assert not cap["planner_calls"], "the planner ran despite the duplicate guard"
    assert result["pr_url"] is None


def test_stale_run_is_reaped_so_the_issue_is_not_blocked_forever(pipeline):
    """A crashed worker leaves 'running' forever; the issue must not be stuck."""
    from db.models import Run
    import workers.tasks as wt

    stale_id = uuid.uuid4()
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=wt.MAX_WALLCLOCK_S + 3600)
    with pipeline.session() as session:
        session.add(Run(id=stale_id, repo="octo/test", issue_number=42,
                        status="running", started_at=long_ago))
        session.commit()

    result, _ = pipeline()

    assert result["status"] == "completed", \
        f"a stale row blocked a new run: {result['status']}"

    with pipeline.session() as session:
        reaped = session.get(Run, stale_id)

    assert reaped.status == "stale", \
        f"the abandoned run was not reaped: {reaped.status}"
    assert reaped.finished_at is not None
