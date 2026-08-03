"""
workers/tasks.py
The orchestrator — a single Celery task that drives the full issue→PR pipeline.

This is M1 of the build order (plan2.md §18). It glues all the built pieces:

    issue → clone (token-scrubbed) → create_branch → index_repo
          → ReActAgent.run → submit_changes (commit → push → PR)

The orchestrator runs on the HOST (never inside the sandbox). It calls the
LLM provider from the host, dispatches only test-execution into the sandbox
(when enabled), and interacts with GitHub via the REST client + git helpers.

M4 additions:
    - Every run is persisted to PostgreSQL (`runs` table).
    - Every ReAct step is written incrementally (`run_steps` table).
    - Idempotency guard: a second webhook for the same (repo, issue_number)
      while a run is active is silently skipped.

Run a worker:
    celery -A workers worker --loglevel=info

Enqueue programmatically:
    from workers.tasks import run_issue
    result = run_issue.delay("owner/repo", 42)
    print(result.get(timeout=300))

Offline self-test (no Redis, no API key):
    python -m workers.tasks
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env early (same pattern as agent/loop.py).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from . import app  # the Celery app from workers/__init__.py 

log = logging.getLogger("workers.tasks")

# ── configuration ────────────────────────────────────────────────────────────

WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/var/agent/workspaces"))
COMMENT_ON_START = os.getenv("COMMENT_ON_START", "true").lower() in ("1", "true", "yes")
MAX_WALLCLOCK_S = int(os.getenv("MAX_WALLCLOCK_S", "1800"))


# ── database helpers ─────────────────────────────────────────────────────────

def _persist_step(session, run_id, step_n, step_data, agent_name: str = "coder"):
    """Write one ReAct step to the run_steps table (incremental).

    Called after each agent step so a crashed run still has partial trace.
    agent_name tags the row as 'planner', 'coder', or 'reviewer'.

    The key is "tools" — that is what ReActAgent.run() puts in each step dict.
    This used to read "tool_calls", a key the loop never sets, so run_steps.tools
    was NULL for every Coder step ever recorded and the trace table was hollow
    (plan2.md §22 F6).
    """
    try:
        from db.models import RunStep
        tools_json = None
        if step_data.get("tools"):
            tools_json = [
                {
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "result": str(tc.get("result", ""))[:2000],
                    "is_error": tc.get("is_error", False),
                }
                for tc in step_data["tools"]
            ]

        step = RunStep(
            run_id=run_id,
            n=step_n,
            agent_name=agent_name,
            stop_reason=step_data.get("stop_reason"),
            text=(step_data.get("text") or "")[:5000],
            input_tokens=step_data.get("input_tokens", 0),
            output_tokens=step_data.get("output_tokens", 0),
            tools=tools_json,
        )
        session.add(step)
        session.commit()
    except Exception as e:
        log.warning("DB step persist failed (non-fatal): %s", e)
        try:
            session.rollback()
        except Exception:
            pass


def _persist_agent_step(session, run_id, step_n, agent_name: str, output_dict: dict,
                        usage=None):
    """Persist a single-call agent output (Planner or Reviewer) as a RunStep."""
    try:
        from db.models import RunStep
        import json
        step = RunStep(
            run_id=run_id,
            n=step_n,
            agent_name=agent_name,
            stop_reason="end_turn",
            text=json.dumps(output_dict)[:5000],
            input_tokens=getattr(usage, 'input_tokens', 0) if usage else 0,
            output_tokens=getattr(usage, 'output_tokens', 0) if usage else 0,
            tools=None,
        )
        session.add(step)
        session.commit()
    except Exception as e:
        log.warning("DB agent step persist failed (non-fatal): %s", e)
        try:
            session.rollback()
        except Exception:
            pass


def _resolve_run_id(explicit, task_self) -> uuid.UUID:
    """Decide which UUID this run is recorded under.

    Order: the id the caller passed, then the Celery task id, then a fresh UUID.
    The API passes the same value for both, which is what makes the run_id a
    caller receives resolvable at GET /runs/{id} (plan2.md §22 F5). The
    fallbacks keep `run_issue.delay(repo, n)` and direct `.run()` calls working.
    """
    if explicit:
        try:
            return uuid.UUID(str(explicit))
        except (ValueError, AttributeError, TypeError):
            log.warning("run_id %r is not a UUID; generating one", explicit)

    try:
        task_id = getattr(getattr(task_self, "request", None), "id", None)
        if task_id:
            return uuid.UUID(str(task_id))
    except (ValueError, AttributeError, TypeError):
        pass   # eager/local invocation, or a non-UUID task id

    return uuid.uuid4()


def _reap_stale_runs(session, repo: str, issue_number: int) -> int:
    """Mark abandoned 'running' rows as 'stale' so a new run can proceed.

    A worker that crashes mid-run leaves its row in 'running' forever. With the
    partial unique index on active runs (D16) that would block the issue
    permanently, so anything older than the wall-clock ceiling plus a grace
    margin is presumed dead (plan2.md §13, §22 F10).

    Returns the number of rows reaped.
    """
    try:
        from db.models import Run
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAX_WALLCLOCK_S + 300)
        stale = session.query(Run).filter(
            Run.repo == repo,
            Run.issue_number == issue_number,
            Run.status == "running",
            Run.started_at < cutoff,
        ).all()
        for row in stale:
            log.warning("reaping stale run %s for %s#%d (started %s)",
                        row.id, repo, issue_number, row.started_at)
            row.status = "stale"
            row.error_detail = "abandoned by a crashed worker; reaped"
            row.finished_at = datetime.now(timezone.utc)
        if stale:
            session.commit()
        return len(stale)
    except Exception as e:
        log.warning("stale-run reaping failed (non-fatal): %s", e)
        try:
            session.rollback()
        except Exception:
            pass
        return 0


def _capture_diff(workspace) -> str:
    """Capture the Coder's changes as a diff the Reviewer can actually read.

    Stages first (`git add -A`) and diffs the index, because a plain `git diff`
    omits untracked files — so any file the Coder *created*, which in the
    Red→Green workflow is normally the new test, would be invisible to a
    Reviewer that is explicitly asked whether a test was added (plan2.md §22 F2).

    Staging is safe and costs nothing: submit_changes() runs `git add -A` before
    committing anyway, and the workspace is deleted at the end of the run.

    Uses agent.github._git rather than raw subprocess so the call inherits
    _GIT_HARDENING (hooks and fsmonitor disabled). That matters here: by this
    point the repo's own tests have run against this workspace, so host-side git
    is a trust boundary (§9 / audit 1 H1).
    """
    from agent.github import _git   # hardened git wrapper
    try:
        _git(workspace, "add", "-A")
        proc = _git(workspace, "diff", "--cached")
        return (proc.stdout or "").strip()
    except Exception as e:
        log.warning("could not capture diff for reviewer: %s", e)
        return ""


def _sync_budget(result: dict, budget) -> None:
    """Copy the shared Budget's running totals into the result dict.

    All three agents accrue into one Budget, so totals are read straight off it
    rather than accumulated per stage. The previous code did
    `result["steps"] += b.steps` against an already-cumulative Budget, which
    counted the Coder's first round twice (plan2.md §22 F4).
    """
    result["steps"] = budget.steps
    result["input_tokens"] = budget.total.input_tokens
    result["output_tokens"] = budget.total.output_tokens
    result["cost_usd"] = round(budget.spent_usd, 6)


MAX_REVIEW_ROUNDS = int(os.getenv("MAX_REVIEW_ROUNDS", "2"))


# ═════════════════════════════════════════════════════════════════════════════
# The orchestrator task
# ═════════════════════════════════════════════════════════════════════════════

@app.task(
    bind=True,
    name="workers.run_issue",
    time_limit=MAX_WALLCLOCK_S,
    soft_time_limit=MAX_WALLCLOCK_S - 60,  # graceful shutdown 60s before hard kill
    acks_late=True,
)
def run_issue(
    self,
    repo: str,
    issue_number: int,
    use_sandbox: bool = False,
    run_id: str | None = None,
) -> dict:
    """Execute the full issue→PR pipeline for a single GitHub issue.

    This is the only Celery task. One invocation = one autonomous run.
    Returns a result dict with status, cost, PR URL (if any), and trace.

    Persists the run and each step to PostgreSQL for durable observability.
    The DB writes are best-effort — a DB failure does not prevent the agent
    from doing its work.

    Parameters
    ----------
    repo : str
        The "owner/repo" slug (e.g. "octo-org/widget").
    issue_number : int
        The GitHub issue number to resolve.
    use_sandbox : bool
        If True, run tests inside a hardened Docker container. Requires a
        running Docker daemon and a pre-built sandbox image.
    run_id : str, optional
        The UUID this run is recorded under. The API mints it and passes it as
        BOTH this argument and Celery's `task_id`, so the id a caller is handed
        resolves in the database *and* in the Celery result backend. Previously
        the task minted its own UUID, so the id returned by the API could never
        match a `runs` row and the read endpoints were unreachable
        (plan2.md §16 D17, §22 F5). Falls back to the Celery task id, then to a
        fresh UUID, so a bare `run_issue.delay(repo, n)` still works.
    """
    # Lazy imports — heavy deps (LLM SDKs, sentence-transformers, chromadb)
    # should only load inside the worker, not when the Celery app starts.
    from agent.github import (
        GitHubClient,
        GitHubError,
        Issue,
        branch_for_issue,
        clone,
        create_branch,
        submit_changes,
    )
    from agent.loop import Budget, ReActAgent, RunResult
    from agent.providers import ProviderError, get_provider

    run_id = _resolve_run_id(run_id, self)
    run_id_str = str(run_id)
    workspace: Path | None = None
    sandbox = None
    client = GitHubClient()

    result = {
        "run_id": run_id_str,
        "repo": repo,
        "issue_number": issue_number,
        "status": "error",
        "steps": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "pr_number": None,
        "pr_url": None,
        "error": None,
        "final_text": "",
    }

    # ── Open the DB session and insert the run row ────────────────────────
    # The `runs` table is append-only: every attempt inserts a NEW row, so a
    # re-run never overwrites the previous attempt's record or orphans its
    # run_steps. Idempotency comes from the partial unique index on active runs
    # rather than from mutating rows in place (plan2.md §16 D16, §22 F7/F10).
    db_session = None
    run_db = None
    try:
        from sqlalchemy.exc import IntegrityError

        from db.models import Run
        # We manage the session manually (not via context manager) because
        # we need it open across the whole pipeline for incremental writes.
        from db.session import _get_session_factory
        db_session = _get_session_factory()()

        # Clear out runs abandoned by a crashed worker; otherwise their
        # 'running' row would block this issue forever.
        _reap_stale_runs(db_session, repo, issue_number)

        # Idempotency: is another run already active for this issue?
        active = db_session.query(Run).filter_by(
            repo=repo, issue_number=issue_number, status="running",
        ).first()

        if active:
            log.info("[%s] skipping — a run is already active for %s#%d (run %s)",
                     run_id_str, repo, issue_number, active.id)
            result["status"] = "skipped_duplicate"
            result["error"] = f"run {active.id} is already active"
            db_session.close()
            return result

        run_db = Run(
            id=run_id,
            repo=repo,
            issue_number=issue_number,
            status="running",
            provider=os.getenv("LLM_PROVIDER", "anthropic"),
            model=os.getenv("LLM_MODEL", ""),
        )
        try:
            db_session.add(run_db)
            db_session.commit()
            log.info("[%s] inserted run row for %s#%d", run_id_str, repo, issue_number)
        except IntegrityError:
            # Lost the race: another worker inserted its 'running' row between
            # our SELECT above and this INSERT. The partial unique index is the
            # arbiter, so no application-level lock is needed — we simply stand
            # down (plan2.md §22 F10).
            db_session.rollback()
            log.info("[%s] skipping — lost the insert race for %s#%d",
                     run_id_str, repo, issue_number)
            result["status"] = "skipped_duplicate"
            result["error"] = "another run for this issue started concurrently"
            db_session.close()
            return result

    except Exception as e:
        log.warning("[%s] DB setup failed (non-fatal, continuing without DB): %s", run_id_str, e)
        run_db = None
        if db_session:
            try:
                db_session.rollback()
            except Exception:
                pass

    try:
        # Step counter shared across all agents so step numbers are globally unique.
        _global_step_n = 0

        # ONE budget for the whole run. Planner, Coder, and Reviewer all accrue
        # into it, so the caps in .env bound the run's *total* spend rather than
        # just the Coder's, and the totals are read straight off it at the end
        # instead of being summed per stage (plan2.md §22 F4).
        budget = Budget()
        # ── 1. Fetch the issue ────────────────────────────────────────────
        log.info("[%s] fetching issue %s#%d", run_id_str, repo, issue_number)
        try:
            issue = client.get_issue(repo, issue_number)
        except GitHubError as e:
            result["status"] = "github_error"
            result["error"] = f"could not fetch issue: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # Update the run row with issue title.
        if run_db and db_session:
            try:
                run_db.issue_title = issue.title
                db_session.commit()
            except Exception:
                try:
                    db_session.rollback()
                except Exception:
                    pass

        from agent.planner import run_planner
        from agent.reviewer import build_feedback_task, run_reviewer
        from agent.schemas import PlannerOutput, ReviewerOutput

        # ── 1b. Acknowledge immediately ──────────────────────────────────
        # Posted BEFORE the clone and index, which together take a while. The
        # plan comment cannot appear until the Planner has actually read the
        # code, so without this the issue sits silent long enough to look
        # broken. Cheap, and it is the first signal a human gets.
        def _say(body: str) -> None:
            """Comment on the issue. Never let a comment failure affect a run."""
            try:
                client.comment_on_issue(repo, issue_number, body)
            except Exception as e:
                log.warning("[%s] could not comment: %s", run_id_str, e)

        def _announce_model_switch(agent_name, old_model, new_model, reason):
            """Surface a mid-run model swap on the issue.

            Passed to the Planner and Coder as `on_model_switch`. They know
            nothing about GitHub — this keeps that knowledge in the orchestrator
            while still making a silent degradation visible to whoever is
            watching the issue.
            """
            _say(
                f"⚠️ **{agent_name}: switching models.**\n\n"
                f"`{old_model}` is unavailable — {reason}\n\n"
                f"Retrying on `{new_model}`. This may affect quality, and the "
                f"PR will note it if anything looks off."
            )

        if COMMENT_ON_START:
            _say(
                "👋 **Got it — I'm on this.**\n\n"
                "Cloning the repository and indexing the codebase now, then my "
                "Planner will work out what needs to change. I'll post its "
                "analysis here shortly, then open a PR.\n\n"
                f"*(run `{run_id_str}`)*"
            )

        # ── 2. Clone ─────────────────────────────────────────────────────
        workspace = WORKSPACE_ROOT / run_id_str / "repo"
        log.info("[%s] cloning %s into %s", run_id_str, repo, workspace)
        try:
            clone(repo, workspace)
        except GitHubError as e:
            result["status"] = "github_error"
            result["error"] = f"clone failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # ── 3. Create branch ─────────────────────────────────────────────
        branch = branch_for_issue(issue_number)
        log.info("[%s] creating branch %s", run_id_str, branch)
        try:
            base = create_branch(workspace, branch)
        except GitHubError as e:
            result["status"] = "github_error"
            result["error"] = f"branch creation failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # Update branch in DB.
        if run_db and db_session:
            try:
                run_db.branch = branch
                db_session.commit()
            except Exception:
                try:
                    db_session.rollback()
                except Exception:
                    pass

        # ── 4. Index the codebase ────────────────────────────────────────
        log.info("[%s] indexing codebase for semantic search", run_id_str)
        try:
            from agent import retrieval
            retrieval.index_repo(str(workspace))
        except Exception as e:
            result["status"] = "index_error"
            result["error"] = f"indexing failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # ── 5. Planner — structured analysis of the issue ─────────────────
        # MUST run after the clone and the index: the Planner retrieves real
        # code for the issue, so `files_to_touch` is grounded in the repository
        # instead of guessed from the issue title (plan2.md §22 F1). Retrieval
        # is left enabled; a retrieval failure degrades to issue-text-only
        # planning inside run_planner() rather than failing the run.
        plan = PlannerOutput()   # safe empty default
        planner_usage = None
        log.info("[%s] running Planner agent (with codebase retrieval)", run_id_str)
        try:
            plan, planner_usage = run_planner(
                issue.to_task(),
                workspace,
                budget=budget,
                on_model_switch=_announce_model_switch,
            )
            _persist_agent_step(db_session, run_id, _global_step_n, "planner",
                                plan.to_dict(), planner_usage)
            _global_step_n += 1
            # Store planner output on the run row.
            if run_db and db_session:
                try:
                    run_db.planner_output = plan.to_dict()
                    db_session.commit()
                except Exception:
                    try:
                        db_session.rollback()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("[%s] Planner failed (continuing with empty plan): %s", run_id_str, e)

        # ── 6. Post the plan — the follow-up to the acknowledgement ───────
        if COMMENT_ON_START:
            if plan.plan_steps:
                steps_str = "\n".join(f"{i}. {s}" for i, s in
                                      enumerate(plan.plan_steps, 1))
                root_cause = (
                    f"\n\n**Why I think it's happening:**\n{plan.root_cause_hypothesis}"
                    if plan.root_cause_hypothesis else ""
                )
                files_str = (
                    "\n\n**Files I expect to touch:** "
                    + ", ".join(f"`{f}`" for f in plan.files_to_touch)
                    if plan.files_to_touch else ""
                )
                _say(
                    f"🔍 **Here's what I found.**\n\n"
                    f"**What I think you're asking for:**\n{plan.understanding}"
                    f"{root_cause}{files_str}\n\n"
                    f"**My plan:**\n{steps_str}\n\n"
                    f"Writing the code now — I'll open a PR when the tests pass."
                )
            else:
                # The Planner produced nothing usable even after its fallback.
                # Say so rather than posting a confident-looking empty plan —
                # the Coder is about to work without one, and that is worth
                # knowing when reviewing whatever PR appears.
                _say(
                    "🔍 **I couldn't produce a structured plan for this one.**\n\n"
                    "I'll still attempt a fix, working straight from the issue "
                    "text — but treat the resulting PR with extra scepticism, "
                    "and consider rewording the issue to be narrower and more "
                    "specific if the result misses the mark."
                )

        # ── 7. (Optional) Start sandbox ──────────────────────────────────
        if use_sandbox:
            log.info("[%s] starting hardened sandbox", run_id_str)
            try:
                from agent.sandbox import Sandbox, SandboxError, docker_available
                if not docker_available():
                    result["status"] = "sandbox_error"
                    result["error"] = "Docker daemon is not reachable"
                    log.error("[%s] %s", run_id_str, result["error"])
                    return result
                sandbox = Sandbox(str(workspace))
                sandbox.start()
            except Exception as e:
                result["status"] = "sandbox_error"
                result["error"] = f"sandbox start failed: {e}"
                log.error("[%s] %s", run_id_str, result["error"])
                return result

        # ── 8. Coder — ReAct agent guided by the plan ────────────────────
        log.info("[%s] starting Coder agent (plan has %d steps)",
                 run_id_str, len(plan.plan_steps))
        try:
            agent = ReActAgent(
                workspace=workspace,
                auto_index=False,   # already indexed in step 4
                sandbox=sandbox,
                budget=budget,      # shared with Planner + Reviewer (F4)
                on_model_switch=_announce_model_switch,
            )
            agent_result: RunResult = agent.run_with_plan(issue.to_task(), plan)
        except (ProviderError, ValueError) as e:
            result["status"] = "provider_error"
            result["error"] = f"agent setup/run failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        _sync_budget(result, budget)
        result["final_text"] = agent_result.final_text[:2000]

        log.info("[%s] Coder finished: %s", run_id_str, agent_result.summary())

        # The best result we can still ship. A later review round may improve on
        # it, but must never destroy it: if a re-run exhausts its budget or
        # errors, we open a PR from this one rather than discarding the work and
        # deleting the workspace with it in the finally block (plan2.md §22 F3).
        best_result: RunResult | None = (
            agent_result if agent_result.status == "completed" else None
        )

        # Extract Red/Green test evidence from the Coder's final summary.
        test_evidence = ReActAgent.extract_test_evidence(agent_result.final_text)

        # Persist Coder steps.
        if db_session and run_db:
            for step_data in agent_result.steps:
                _persist_step(db_session, run_id, _global_step_n, step_data, "coder")
                _global_step_n += 1

        # ── 9. Reviewer — fresh-context audit of the diff ────────────────
        last_review = ReviewerOutput(verdict="approve", summary="No review performed.")
        if best_result is not None:
            # Staged diff, so files the Coder CREATED (typically the new test)
            # are visible to the Reviewer (F2), and the real run_tests output
            # rather than the Coder's prose summary (F11).
            diff_text = _capture_diff(workspace)
            test_output = agent.last_test_output

            for review_round in range(MAX_REVIEW_ROUNDS):
                log.info("[%s] running Reviewer (round %d/%d)",
                         run_id_str, review_round + 1, MAX_REVIEW_ROUNDS)
                try:
                    last_review, rev_usage = run_reviewer(
                        issue.to_task(), plan, diff_text, test_output,
                        budget=budget,
                        on_model_switch=_announce_model_switch,
                    )
                    _persist_agent_step(db_session, run_id, _global_step_n,
                                        "reviewer", last_review.to_dict(), rev_usage)
                    _global_step_n += 1
                    _sync_budget(result, budget)
                    # Update reviewer output on the run row.
                    if run_db and db_session:
                        try:
                            run_db.reviewer_output = last_review.to_dict()
                            db_session.commit()
                        except Exception:
                            try:
                                db_session.rollback()
                            except Exception:
                                pass
                except Exception as e:
                    log.warning("[%s] Reviewer failed (skipping): %s", run_id_str, e)
                    break

                if last_review.approved:
                    log.info("[%s] Reviewer approved (round %d)",
                             run_id_str, review_round + 1)
                    break

                if review_round < MAX_REVIEW_ROUNDS - 1:
                    # Re-run the Coder with reviewer feedback.
                    log.info("[%s] Reviewer requested changes — re-running Coder",
                             run_id_str)
                    # Hand the Coder its own prior work — the re-run starts from
                    # an empty message history (plan2.md §22 F13).
                    feedback_task = build_feedback_task(
                        issue.to_task(), plan, last_review,
                        prior_diff=diff_text,
                        prior_summary=best_result.final_text if best_result else "",
                    )
                    try:
                        rerun: RunResult = agent.run(feedback_task)
                    except Exception as e:
                        log.warning("[%s] Coder re-run raised (%s) — keeping the "
                                    "previous completed result", run_id_str, e)
                        break

                    _sync_budget(result, budget)
                    if db_session and run_db:
                        for step_data in rerun.steps:
                            _persist_step(db_session, run_id,
                                          _global_step_n, step_data, "coder")
                            _global_step_n += 1

                    if rerun.status != "completed":
                        # Budget exhausted, refusal, provider error. The earlier
                        # fix is still on disk and still good — ship that (F3).
                        log.warning("[%s] Coder re-run ended as '%s' — keeping the "
                                    "previous completed result",
                                    run_id_str, rerun.status)
                        break

                    best_result = rerun
                    result["final_text"] = rerun.final_text[:2000]
                    test_evidence = ReActAgent.extract_test_evidence(rerun.final_text)
                    # Refresh diff + test output for the next review round.
                    diff_text = _capture_diff(workspace)
                    test_output = agent.last_test_output
                else:
                    log.warning(
                        "[%s] max review rounds reached with outstanding concerns — "
                        "opening PR with 'needs-human-review' note", run_id_str
                    )

        # ── 10. Submit changes (commit → push → PR) ─────────────────────
        # Gated on best_result, not on the latest attempt: a failed review
        # re-run must not discard an earlier good fix (F3).
        if best_result is not None:
            log.info("[%s] agent completed — submitting changes", run_id_str)
            try:
                pr_title = f"Fix #{issue_number}: {issue.title}"

                # Build rich PR body deterministically from agent outputs
                # (no extra LLM call needed — we already have all the data).
                # Say plainly when review did not actually happen, so an
                # "approved" PR is never confused with an unreviewed one
                # (plan2.md §22 F15, goal G8).
                if not last_review.was_reviewed:
                    reviewer_section = (
                        f"\n\n## Reviewer notes\n"
                        f"> ⚠️ **Automated review was UNAVAILABLE for this run** — "
                        f"this PR has not been reviewed. Please review it manually.\n\n"
                        f"Reason: {last_review.summary or 'unknown'}"
                    )
                else:
                    reviewer_section = (
                        f"\n\n## Reviewer notes\n"
                        f"{last_review.summary}  "
                        f"*(confidence: {last_review.confidence})*"
                    )
                    if last_review.concerns:
                        concerns_str = "\n".join(
                            f"- {c}" for c in last_review.concerns
                        )
                        reviewer_section += f"\n\n**Concerns addressed:**\n{concerns_str}"
                    if not last_review.approved:
                        reviewer_section += "\n\n> ⚠️ Reviewer still had concerns — human review recommended."

                # Plan deviations: files the Coder edited that the Planner did
                # not list. Non-blocking, but a reviewer should see them (F12).
                deviations = getattr(agent, "plan_deviations", [])
                if deviations:
                    dev_str = "\n".join(f"- {d}" for d in dict.fromkeys(deviations))
                    reviewer_section += (
                        f"\n\n**Plan deviations** *(files changed outside the "
                        f"Planner's `files_to_touch`)*:\n{dev_str}"
                    )

                # Test evidence section: show Red/Green if available,
                # otherwise fall back to the plan's test_strategy text.
                if test_evidence.get("red") or test_evidence.get("green"):
                    test_section = (
                        f"## Test Evidence\n\n"
                        f"### 🔴 Red (before fix)\n"
                        f"```\n{test_evidence.get('red', 'N/A')}\n```\n\n"
                        f"### 🟢 Green (after fix)\n"
                        f"```\n{test_evidence.get('green', 'N/A')}\n```\n"
                    )
                else:
                    test_section = f"## Tests\n{plan.test_strategy or 'See test results in CI.'}\n"

                pr_body = (
                    f"Closes #{issue_number}\n\n"
                    f"## Issue\n{plan.understanding or issue.title}\n\n"
                    f"## Root cause\n{plan.root_cause_hypothesis or 'See diff.'}\n\n"
                    f"## Fix\n{best_result.final_text[:1500]}\n\n"
                    f"{test_section}"
                    f"{reviewer_section}\n\n"
                    f"---\n"
                    f"*Automated by auto-swe-agent · run `{run_id_str}` · "
                    f"{result['steps']} steps · "
                    f"{result['input_tokens'] + result['output_tokens']} tokens · "
                    f"${result['cost_usd']:.4f}*"
                )

                pr = submit_changes(
                    workspace,
                    repo=repo,
                    branch=branch,
                    base=base,
                    title=pr_title,
                    body=pr_body,
                )
                if pr is not None:
                    result["status"] = "completed"
                    result["pr_number"] = pr.number
                    result["pr_url"] = pr.html_url
                    log.info("[%s] PR opened: %s", run_id_str, pr.html_url)
                else:
                    # Agent said "completed" but made no file changes.
                    result["status"] = "no_changes"
                    log.info("[%s] agent completed but no changes to commit", run_id_str)
            except GitHubError as e:
                result["status"] = "github_error"
                result["error"] = f"submit failed: {e}"
                log.error("[%s] %s", run_id_str, result["error"])
                return result
        else:
            # Agent didn't complete successfully (budget, refusal, error, etc.)
            result["status"] = agent_result.status
            if agent_result.status not in ("completed", "max_tokens"):
                result["error"] = agent_result.final_text[:500]

        # ── 11. Comment the result back on the issue ─────────────────────
        # Figures come from the shared budget, so they cover all three agents.
        _sync_budget(result, budget)
        _tokens = result["input_tokens"] + result["output_tokens"]
        try:
            if result["pr_url"]:
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Pull request opened: {result['pr_url']}\n\n"
                    f"*{result['steps']} steps · {_tokens} tokens · "
                    f"${result['cost_usd']:.4f}*"
                )
            elif result["status"] == "no_changes":
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Agent completed analysis but found no code changes needed.\n\n"
                    f"*{result['steps']} steps · ${result['cost_usd']:.4f}*"
                )
            else:
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Agent finished with status **{result['status']}**.\n\n"
                    # `error` is initialised to None, so .get()'s default never
                    # fires — use `or` so a None never gets subscripted. Reachable
                    # whenever the Coder ends as max_tokens, which sets a status
                    # but no error string.
                    f"Reason: {(result.get('error') or 'unknown')[:500]}\n\n"
                    f"*{result['steps']} steps · ${result['cost_usd']:.4f}*"
                )
        except GitHubError as e:
            # Non-fatal: the work is done even if the comment fails.
            log.warning("[%s] could not post result comment: %s", run_id_str, e)

    except Exception as e:
        # Catch-all for truly unexpected errors (import failures, etc.)
        result["status"] = "error"
        result["error"] = f"unexpected error: {e}"
        log.exception("[%s] unexpected error in run_issue", run_id_str)

    finally:
        # ── 12. Cleanup ──────────────────────────────────────────────────
        # Tear down the sandbox if it was started.
        if sandbox is not None:
            try:
                sandbox.close()
            except Exception as e:
                log.warning("[%s] sandbox cleanup failed: %s", run_id_str, e)

        # Drop this run's vectors. The workspace path is unique per run, so
        # without this every run leaves a full copy of the repo's chunks behind
        # forever (plan2.md §22 F9).
        if workspace is not None:
            try:
                from agent import retrieval as _retrieval
                if _retrieval.drop_repo(str(workspace)):
                    log.info("[%s] dropped vector index for %s", run_id_str, workspace)
            except Exception as e:
                log.warning("[%s] vector index cleanup failed: %s", run_id_str, e)

        # Remove the workspace directory.
        if workspace is not None:
            run_dir = workspace.parent  # {WORKSPACE_ROOT}/{run_id}/
            if run_dir.exists():
                try:
                    shutil.rmtree(run_dir)
                    log.info("[%s] cleaned up workspace %s", run_id_str, run_dir)
                except OSError as e:
                    log.warning("[%s] workspace cleanup failed: %s", run_id_str, e)

        # ── M4: Final DB update ──────────────────────────────────────────
        if run_db and db_session:
            try:
                run_db.status = result["status"]
                run_db.steps_used = result["steps"]
                run_db.input_tokens = result["input_tokens"]
                run_db.output_tokens = result["output_tokens"]
                run_db.cost_usd = result["cost_usd"]
                run_db.pr_number = result.get("pr_number")
                run_db.pr_url = result.get("pr_url")
                run_db.error_detail = result.get("error")
                run_db.final_text = result.get("final_text", "")[:5000]
                run_db.finished_at = datetime.now(timezone.utc)
                # Persist test evidence if available.
                if 'test_evidence' in dir():
                    run_db.test_evidence = test_evidence
                db_session.commit()
            except Exception as e:
                log.warning("[%s] final DB update failed: %s", run_id_str, e)
                try:
                    db_session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    db_session.close()
                except Exception:
                    pass

    log.info("[%s] final result: %s", run_id_str, {k: v for k, v in result.items() if k != "final_text"})
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test  (no Redis, no API key, no Docker)
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise the orchestrator's logic without any external services.

    Creates a temp workspace, mocks the GitHub client and the LLM provider,
    and runs through the pipeline steps to verify wiring. This mirrors the
    self-test pattern in agent/loop.py and agent/github.py.
    """
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("workers/tasks.py self-test")
    print()

    # ── 1. Verify imports ────────────────────────────────────────────────
    print("imports")
    try:
        from agent.github import (
            GitHubClient,
            GitHubError,
            Issue,
            branch_for_issue,
            clone,
            commit_all,
            configure_identity,
            create_branch,
            has_uncommitted_changes,
            submit_changes,
        )
        check("agent.github imports", True)
    except ImportError as e:
        check("agent.github imports", False, str(e))
        print(f"\nSELF-TEST ABORTED: missing agent.github — {e}")
        return 1

    try:
        from agent.loop import Budget, ReActAgent, RunResult
        from agent.providers import ProviderError
        from agent.providers.base import Usage
        check("agent.loop + providers imports", True)
    except ImportError as e:
        check("agent.loop + providers imports", False, str(e))
        print(f"\nSELF-TEST ABORTED: missing agent.loop/providers — {e}")
        return 1

    # ── 2. Test the pipeline logic (mocked, no network) ──────────────────
    print("pipeline wiring (mocked)")

    # Simulate: create a temp "workspace" that looks like a cloned repo
    # with a change the agent could have made.
    import subprocess
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        git_available = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        git_available = False

    if git_available:
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "repo"
            ws.mkdir(parents=True)

            # Init a repo so git helpers work.
            subprocess.run(["git", "init", "-q", str(ws)], capture_output=True)
            configure_identity(ws)

            # Create an initial commit (mirrors a real clone).
            (ws / "README.md").write_text("# Test\n", encoding="utf-8")
            commit_all(ws, "initial commit")

            # Create the agent branch.
            base = create_branch(ws, branch_for_issue(99))
            check("branch created", base in ("main", "master"))

            # Simulate the agent editing a file.
            (ws / "fix.py").write_text("x = 42  # fixed\n", encoding="utf-8")
            check("agent edit detected", has_uncommitted_changes(ws))

            # Commit (simulating what submit_changes does internally).
            committed = commit_all(ws, "Fix #99: test fix")
            check("commit succeeded", committed is True)
            check("tree clean after commit", not has_uncommitted_changes(ws))

        # Verify Issue.to_task() produces a good task string.
        issue = Issue(
            repo="owner/test-repo",
            number=99,
            title="Off-by-one in pagination",
            body="The paginate() function skips the last page.",
            labels=["bug"],
            author="tester",
        )
        task = issue.to_task()
        check("to_task has issue number", "#99" in task)
        check("to_task has title", "Off-by-one" in task)
        check("to_task has body", "paginate()" in task)

        # Verify RunResult can be created and summarized.
        result = RunResult(
            status="completed",
            final_text="Fixed the off-by-one.",
            steps=[],
            budget=Budget(steps=5, total=Usage(input_tokens=1000, output_tokens=500), spent_usd=0.05),
        )
        summary = result.summary()
        check("RunResult summary works", "completed" in summary and "0.0500" in summary)
    else:
        print("  [skip] git not on PATH — skipping pipeline tests")

    # ── 3. Verify task registration ──────────────────────────────────────
    print("Celery task registration")
    check("run_issue is a Celery task", hasattr(run_issue, "delay"))
    check("run_issue has correct name", run_issue.name == "workers.run_issue")

    # ── 4. Test DB models (SQLite in-memory) ─────────────────────────────
    print("M4: database persistence (SQLite in-memory)")
    try:
        from db.models import Run, RunStep, WebhookEvent, Base, _utcnow
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)

        test_run_id = uuid.uuid4()
        with Session(engine) as session:
            run = Run(
                id=test_run_id,
                repo="owner/test-repo",
                issue_number=42,
                status="running",
                provider="gemini",
                model="gemini-3.5-flash",
            )
            session.add(run)
            session.commit()

            # Add a step.
            step = RunStep(
                run_id=test_run_id,
                n=0,
                stop_reason="tool_use",
                text="Let me read the file.",
                input_tokens=500,
                output_tokens=100,
                tools=[{"name": "read_file", "args": {"path": "main.py"},
                        "result": "...", "is_error": False}],
            )
            session.add(step)
            session.commit()

            fetched = session.get(Run, test_run_id)
            check("run persisted to DB", fetched is not None)
            check("step linked to run", len(fetched.steps) == 1)
            check("step tools stored", fetched.steps[0].tools is not None)

            # Test idempotency: update existing run.
            fetched.status = "completed"
            fetched.pr_number = 7
            fetched.finished_at = _utcnow()
            session.commit()
            check("run updated (idempotent)", session.get(Run, test_run_id).status == "completed")

        check("DB persistence works", True)
    except Exception as e:
        check("DB persistence works", False, str(e))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — imports, pipeline wiring, task registration, and DB persistence all work.")
    print("Start Redis + a Celery worker to run a real issue end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
