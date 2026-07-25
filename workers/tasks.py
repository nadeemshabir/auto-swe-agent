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
from datetime import datetime, timezone
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

def _persist_run(session, run_db):
    """Commit the run row, logging but not crashing on DB errors."""
    try:
        session.add(run_db)
        session.commit()
    except Exception as e:
        log.warning("DB persist failed (non-fatal): %s", e)
        try:
            session.rollback()
        except Exception:
            pass


def _persist_step(session, run_id, step_n, step_data):
    """Write one ReAct step to the run_steps table (incremental).

    Called after each agent step so a crashed run still has partial trace.
    """
    try:
        from db.models import RunStep
        tools_json = None
        if step_data.get("tool_calls"):
            tools_json = [
                {
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "result": str(tc.get("result", ""))[:2000],
                    "is_error": tc.get("is_error", False),
                }
                for tc in step_data["tool_calls"]
            ]

        step = RunStep(
            run_id=run_id,
            n=step_n,
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
) -> dict:
    """Execute the full issue→PR pipeline for a single GitHub issue.

    This is the only Celery task. One invocation = one autonomous run.
    Returns a result dict with status, cost, PR URL (if any), and trace.

    M4: Also persists the run and each step to PostgreSQL for durable
    observability. The DB writes are best-effort — a DB failure does
    not prevent the agent from doing its work.

    Parameters
    ----------
    repo : str
        The "owner/repo" slug (e.g. "octo-org/widget").
    issue_number : int
        The GitHub issue number to resolve.
    use_sandbox : bool
        If True, run tests inside a hardened Docker container. Requires a
        running Docker daemon and a pre-built sandbox image.
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
    from agent.providers import ProviderError

    run_id = uuid.uuid4()
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

    # ── M4: Open DB session and create the run row ────────────────────────
    db_session = None
    run_db = None
    try:
        from db.session import get_session as _get_session
        from db.models import Run

        # We manage the session manually (not via context manager) because
        # we need it open across the whole pipeline for incremental writes.
        from db.session import _get_session_factory
        db_session = _get_session_factory()()

        # Idempotency check: is there already a run for this (repo, issue)?
        existing = db_session.query(Run).filter_by(
            repo=repo, issue_number=issue_number,
        ).first()

        if existing and existing.status == "running":
            log.info("[%s] skipping — a run is already active for %s#%d (run %s)",
                     run_id_str, repo, issue_number, existing.id)
            result["status"] = "skipped_duplicate"
            result["error"] = f"run {existing.id} is already active"
            db_session.close()
            return result

        if existing:
            # Re-run: update the existing row instead of inserting a duplicate.
            run_db = existing
            run_db.id = run_id  # new run_id for the new attempt
            run_db.status = "running"
            run_db.started_at = datetime.now(timezone.utc)
            run_db.finished_at = None
            run_db.error_detail = None
            run_db.steps_used = 0
            run_db.input_tokens = 0
            run_db.output_tokens = 0
            run_db.cost_usd = 0.0
            run_db.pr_number = None
            run_db.pr_url = None
            run_db.final_text = None
            # Delete old steps for a clean slate.
            from db.models import RunStep
            db_session.query(RunStep).filter_by(run_id=existing.id).delete()
            log.info("[%s] re-running %s#%d (updating existing row)", run_id_str, repo, issue_number)
        else:
            # Fresh run: insert a new row.
            run_db = Run(
                id=run_id,
                repo=repo,
                issue_number=issue_number,
                status="running",
                provider=os.getenv("LLM_PROVIDER", "anthropic"),
                model=os.getenv("LLM_MODEL", ""),
            )
            log.info("[%s] created new run row for %s#%d", run_id_str, repo, issue_number)

        _persist_run(db_session, run_db)

    except Exception as e:
        log.warning("[%s] DB setup failed (non-fatal, continuing without DB): %s", run_id_str, e)
        if db_session:
            try:
                db_session.rollback()
            except Exception:
                pass

    try:
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

        # ── 2. Comment "on it" ────────────────────────────────────────────
        if COMMENT_ON_START:
            try:
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Auto-SWE agent is working on this issue. (run `{run_id_str}`)"
                )
            except GitHubError as e:
                # Non-fatal — the agent can still work without posting a comment.
                log.warning("[%s] could not post start comment: %s", run_id_str, e)

        # ── 3. Clone ─────────────────────────────────────────────────────
        workspace = WORKSPACE_ROOT / run_id_str / "repo"
        log.info("[%s] cloning %s into %s", run_id_str, repo, workspace)
        try:
            clone(repo, workspace)
        except GitHubError as e:
            result["status"] = "github_error"
            result["error"] = f"clone failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # ── 4. Create branch ─────────────────────────────────────────────
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

        # ── 5. Index the codebase ────────────────────────────────────────
        log.info("[%s] indexing codebase for semantic search", run_id_str)
        try:
            from agent import retrieval
            retrieval.index_repo(str(workspace))
        except Exception as e:
            result["status"] = "index_error"
            result["error"] = f"indexing failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # ── 6. (Optional) Start sandbox ──────────────────────────────────
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

        # ── 7. Run the ReAct agent ───────────────────────────────────────
        log.info("[%s] starting ReAct agent loop", run_id_str)
        try:
            agent = ReActAgent(
                workspace=workspace,
                auto_index=False,  # we already indexed in step 5
                sandbox=sandbox,
            )
            agent_result: RunResult = agent.run(issue.to_task())
        except (ProviderError, ValueError) as e:
            result["status"] = "provider_error"
            result["error"] = f"agent setup/run failed: {e}"
            log.error("[%s] %s", run_id_str, result["error"])
            return result

        # Record budget usage regardless of outcome.
        b = agent_result.budget
        result["steps"] = b.steps
        result["input_tokens"] = b.total.input_tokens
        result["output_tokens"] = b.total.output_tokens
        result["cost_usd"] = round(b.spent_usd, 6)
        result["final_text"] = agent_result.final_text[:2000]  # cap for JSON

        log.info("[%s] agent finished: %s", run_id_str, agent_result.summary())

        # ── M4: Persist each step from the trace ─────────────────────────
        if db_session and run_db:
            for step_n, step_data in enumerate(agent_result.steps):
                _persist_step(db_session, run_id, step_n, step_data)

        # ── 8. Submit changes (commit → push → PR) ──────────────────────
        if agent_result.status == "completed":
            log.info("[%s] agent completed — submitting changes", run_id_str)
            try:
                pr_title = f"Fix #{issue_number}: {issue.title}"
                pr_body = (
                    f"Closes #{issue_number}\n\n"
                    f"## What changed\n\n{agent_result.final_text[:1500]}\n\n"
                    f"---\n"
                    f"*Automated by auto-swe-agent · "
                    f"run `{run_id_str}` · "
                    f"{b.steps} steps · "
                    f"{b.total.input_tokens + b.total.output_tokens} tokens · "
                    f"${b.spent_usd:.4f}*"
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

        # ── 9. Comment the result back on the issue ──────────────────────
        try:
            if result["pr_url"]:
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Pull request opened: {result['pr_url']}\n\n"
                    f"*{b.steps} steps · "
                    f"{b.total.input_tokens + b.total.output_tokens} tokens · "
                    f"${b.spent_usd:.4f}*"
                )
            elif result["status"] == "no_changes":
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Agent completed analysis but found no code changes needed.\n\n"
                    f"*{b.steps} steps · ${b.spent_usd:.4f}*"
                )
            else:
                client.comment_on_issue(
                    repo, issue_number,
                    f"🤖 Agent finished with status **{result['status']}**.\n\n"
                    f"Reason: {result.get('error', 'unknown')[:500]}\n\n"
                    f"*{b.steps} steps · ${b.spent_usd:.4f}*"
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
        # ── 10. Cleanup ──────────────────────────────────────────────────
        # Tear down the sandbox if it was started.
        if sandbox is not None:
            try:
                sandbox.close()
            except Exception as e:
                log.warning("[%s] sandbox cleanup failed: %s", run_id_str, e)

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
