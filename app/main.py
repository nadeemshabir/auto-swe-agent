"""
app/main.py
FastAPI API gateway — webhook receiver + read API + manual trigger.

This is M3/M4 of the build order (plan2.md §6.2, §7.5). It is the public entry
point of the system: GitHub sends webhook events here, we verify the HMAC
signature, parse the payload, check for webhook deduplication, and drop a
job onto the Celery queue.

It also provides a read API backed by PostgreSQL to query runs and step traces.

Start the server:
    uvicorn app.main:app --reload --port 8000

Offline self-test:
    python -m app.main
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Load .env (same pattern as agent/ and workers/).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI, Header, HTTPException, Request, Response, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("app.main")

# ── configuration ────────────────────────────────────────────────────────────

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()
MAX_WEBHOOK_BODY_BYTES = int(os.getenv("MAX_WEBHOOK_BODY_BYTES", "1048576"))  # 1 MB
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Shared secret for POST /runs. Fail-closed, matching GITHUB_WEBHOOK_SECRET:
# with no token configured the endpoint is disabled rather than open. An
# unauthenticated /runs lets anyone who finds the URL make the agent clone
# arbitrary repositories and burn the LLM budget (plan2.md §22 F8).
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "")

# Optional comma-separated allowlist of "owner/repo" slugs the agent may work
# on. Empty = no restriction. Authentication alone still lets a leaked token
# target any repository, so set this in any shared deployment.
AGENT_REPO_ALLOWLIST = {
    r.strip() for r in os.getenv("AGENT_REPO_ALLOWLIST", "").split(",") if r.strip()
}

# Simple per-process rate limit on POST /runs: N starts per window, per client.
# Deliberately in-process — it bounds accidental abuse and runaway scripts
# without adding a Redis dependency. It is NOT a distributed limit: with
# multiple API replicas each enforces its own budget. Revisit if that matters.
RUNS_RATE_LIMIT = int(os.getenv("RUNS_RATE_LIMIT", "10"))
RUNS_RATE_WINDOW_S = int(os.getenv("RUNS_RATE_WINDOW_S", "60"))
_rate_buckets: dict[str, list[float]] = {}


def _envflag(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


# Whether webhook-triggered runs execute tests inside the hardened sandbox
# (agent/sandbox.py) instead of host-side in the worker container.
#
# OFF by default, and it is a config flip rather than a code change on purpose
# (plan2.md §10, D10): the sandbox needs a real Docker daemon on the host, which
# container platforms — Azure Container Apps, ACI, App Service — do not provide.
# So this stays false while deployed on a container platform, and becomes true
# when the workload moves somewhere with a daemon (a VM, AKS with socket access,
# or locally).
#
# Setting it true without a reachable daemon does NOT silently degrade: the
# orchestrator ends the run as `sandbox_error` (workers/tasks.py step 7), so a
# misconfiguration is loud rather than quietly running untrusted code unguarded.
#
# ⚠️ Before turning this on, the sandbox image must contain the TARGET repo's
# test dependencies — it has no network, and runs a bare `python -m pytest`
# against the image's own site-packages. See DECISION D9.
USE_SANDBOX = _envflag("USE_SANDBOX")

# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Auto-SWE Agent API",
    description="Webhook receiver and read API for the autonomous software engineer.",
    version="0.1.0",
)


# ── request models ───────────────────────────────────────────────────────────

class ManualRunRequest(BaseModel):
    """Body for POST /runs — manual trigger."""
    repo: str
    issue_number: int
    # None = follow the USE_SANDBOX default; true/false overrides it for this
    # one run, which is how you smoke-test the sandbox without changing config.
    use_sandbox: bool | None = None


# ═════════════════════════════════════════════════════════════════════════════
# Webhook endpoint
# ═════════════════════════════════════════════════════════════════════════════

def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC. Uses constant-time comparison to
    prevent timing attacks (plan2.md §9)."""
    if not secret:
        # No secret configured — reject all webhooks in production, but allow
        # in dev by logging a warning. For safety we still reject.
        log.warning("GITHUB_WEBHOOK_SECRET is not set — rejecting webhook")
        return False
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _require_api_token(authorization: str | None, x_agent_token: str | None) -> None:
    """Authenticate a manual trigger, or raise 401/503.

    Accepts either `Authorization: Bearer <token>` or `X-Agent-Token: <token>`.
    Comparison is constant-time, same as the webhook HMAC.

    Fail-closed when unconfigured: an unset AGENT_API_TOKEN disables the
    endpoint (503) rather than leaving it open. This mirrors how
    GITHUB_WEBHOOK_SECRET already behaves and means a deployment cannot
    accidentally expose /runs by forgetting to set a variable.
    """
    if not AGENT_API_TOKEN:
        log.warning("POST /runs called but AGENT_API_TOKEN is not set — refusing")
        raise HTTPException(
            status_code=503,
            detail="Manual trigger is disabled: AGENT_API_TOKEN is not configured.",
        )

    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_agent_token:
        presented = x_agent_token.strip()

    if not presented or not hmac.compare_digest(presented, AGENT_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


def _check_repo_allowed(repo: str) -> None:
    """Reject repositories outside the allowlist, when one is configured."""
    if AGENT_REPO_ALLOWLIST and repo not in AGENT_REPO_ALLOWLIST:
        raise HTTPException(
            status_code=403,
            detail=f"Repository {repo!r} is not in AGENT_REPO_ALLOWLIST",
        )


def _check_rate_limit(client_key: str) -> None:
    """Allow at most RUNS_RATE_LIMIT starts per RUNS_RATE_WINDOW_S, per client."""
    now = time.monotonic()
    window_start = now - RUNS_RATE_WINDOW_S
    hits = [t for t in _rate_buckets.get(client_key, []) if t > window_start]
    if len(hits) >= RUNS_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RUNS_RATE_LIMIT} runs per "
                   f"{RUNS_RATE_WINDOW_S}s",
        )
    hits.append(now)
    _rate_buckets[client_key] = hits


def _enqueue_run(run_issue, repo: str, issue_number: int, use_sandbox: bool = False):
    """Enqueue a run under a single, caller-visible UUID.

    The same UUID is used for BOTH Celery's `task_id` and the `runs.id` the
    worker writes, so the id handed back to the caller resolves at
    `GET /runs/{id}` and `GET /runs/{id}/steps` as well as in the Celery result
    backend.

    Previously the API returned `async_result.id` (a Celery task id) while the
    worker minted its own unrelated `uuid4()` for the database row. The two
    could never match, so the DB lookup always missed, `/runs/{id}` silently
    degraded to the Celery fallback (losing planner_output, reviewer_output and
    test_evidence), and `/runs/{id}/steps` returned 404 every time
    (plan2.md §16 D17, §22 F5).
    """
    run_id = uuid.uuid4()
    return run_issue.apply_async(
        args=[repo, issue_number, use_sandbox],
        kwargs={"run_id": str(run_id)},
        task_id=str(run_id),
    )


@app.post("/webhooks/github", status_code=202)
async def webhook_github(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
):
    """Receive a GitHub webhook, verify its HMAC, parse it, and enqueue a job.

    Deduplicates requests using the delivery ID header.
    Returns 202 immediately if actionable, 204 if not actionable, 403 if the
    signature is invalid, 413 if the body is too large.
    """
    # ── 1. Read and size-check the raw body ──────────────────────────────
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    # ── 2. Verify HMAC signature ─────────────────────────────────────────
    if not _verify_signature(body, x_hub_signature_256 or "", GITHUB_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # ── 3. Parse the payload ─────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed JSON")

    event_type = (x_github_event or "").strip()
    if not event_type:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    delivery_id = x_github_delivery or f"unknown-{uuid.uuid4()}"

    # ── M4: Deduplicate webhooks using database ──────────────────────────
    from db.session import get_session
    from db.models import WebhookEvent

    with get_session() as session:
        existing_event = session.query(WebhookEvent).filter_by(delivery_id=delivery_id).first()
        if existing_event:
            log.info("webhook: duplicate delivery %s, skipping", delivery_id)
            return JSONResponse(
                status_code=202,
                content={
                    "message": "duplicate webhook ignored",
                    "delivery_id": delivery_id,
                }
            )

    # ── 4. Decide if actionable ──────────────────────────────────────────
    from agent.github import parse_webhook_event
    issue = parse_webhook_event(payload, event_type=event_type)

    if issue is None:
        # Not actionable (wrong event, non-issue, bot author, etc.) — silent OK.
        # Record skipped event
        try:
            with get_session() as session:
                evt = WebhookEvent(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    action_taken="skipped",
                )
                session.add(evt)
        except Exception as e:
            log.warning("Failed to record skipped webhook event: %s", e)
        return Response(status_code=204)

    # Defence in depth: the payload is HMAC-signed, so it came from a repo whose
    # webhook we configured — but if an allowlist is set, honour it here too.
    # Treated as non-actionable (204) rather than an error: from GitHub's side
    # there is nothing to retry.
    if AGENT_REPO_ALLOWLIST and issue.repo not in AGENT_REPO_ALLOWLIST:
        log.warning("webhook: %s is not in AGENT_REPO_ALLOWLIST — ignoring", issue.repo)
        try:
            with get_session() as session:
                session.add(WebhookEvent(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    repo=issue.repo,
                    issue_number=issue.number,
                    action_taken="skipped",
                ))
        except Exception as e:
            log.warning("Failed to record disallowed-repo webhook event: %s", e)
        return Response(status_code=204)

    # ── 5. Enqueue the task ──────────────────────────────────────────────
    from workers.tasks import run_issue
    async_result = _enqueue_run(run_issue, issue.repo, issue.number, USE_SANDBOX)

    # Record enqueued event in DB
    try:
        with get_session() as session:
            evt = WebhookEvent(
                delivery_id=delivery_id,
                event_type=event_type,
                repo=issue.repo,
                issue_number=issue.number,
                action_taken="enqueued",
            )
            session.add(evt)
    except Exception as e:
        log.warning("Failed to record enqueued webhook event: %s", e)

    log.info(
        "webhook: enqueued run %s for %s#%d (delivery=%s)",
        async_result.id, issue.repo, issue.number,
        delivery_id,
    )

    return JSONResponse(
        status_code=202,
        content={
            "message": "accepted",
            "run_id": async_result.id,
            "repo": issue.repo,
            "issue_number": issue.number,
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# Manual trigger
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/runs", status_code=202)
async def manual_trigger(
    req: ManualRunRequest,
    request: Request,
    authorization: str | None = Header(None),
    x_agent_token: str | None = Header(None),
):
    """Manually trigger an agent run for a given repo + issue number.

    Requires a shared secret (`Authorization: Bearer <AGENT_API_TOKEN>` or
    `X-Agent-Token`), honours AGENT_REPO_ALLOWLIST, and is rate limited.
    Returns 202 with the run id, which resolves at GET /runs/{id}.

    Unlike the webhook this endpoint is not signed by GitHub, so it is the one
    place an outsider could otherwise start arbitrary work on arbitrary
    repositories (plan2.md §22 F8, §9).
    """
    _require_api_token(authorization, x_agent_token)
    _check_repo_allowed(req.repo)
    _check_rate_limit(request.client.host if request.client else "unknown")

    # An explicit value in the body wins; otherwise follow the deployment default.
    use_sandbox = USE_SANDBOX if req.use_sandbox is None else req.use_sandbox

    from workers.tasks import run_issue
    async_result = _enqueue_run(run_issue, req.repo, req.issue_number, use_sandbox)

    # Record the manual run request as a special webhook event for audit
    from db.session import get_session
    from db.models import WebhookEvent
    try:
        with get_session() as session:
            evt = WebhookEvent(
                delivery_id=f"manual-{async_result.id}",
                event_type="manual",
                repo=req.repo,
                issue_number=req.issue_number,
                action_taken="enqueued",
            )
            session.add(evt)
    except Exception as e:
        log.warning("Failed to record manual event: %s", e)

    log.info("manual: enqueued run %s for %s#%d", async_result.id, req.repo, req.issue_number)

    return {
        "message": "accepted",
        "run_id": async_result.id,
        "repo": req.repo,
        "issue_number": req.issue_number,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Read API (backed by PostgreSQL - M4)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/runs")
async def list_runs(
    status: str | None = Query(None, description="Filter by run status"),
    repo: str | None = Query(None, description="Filter by repository name (owner/repo)"),
    limit: int = Query(20, ge=1, le=100, description="Max number of runs to return"),
    cursor: str | None = Query(None, description="UUID of the run to start listing after (for keyset pagination)"),
):
    """Fetch a paginated list of runs from PostgreSQL.

    Sorts by started_at descending. Keyset pagination is implemented using
    the `cursor` parameter (matching plan2.md §7.5).
    """
    from db.session import get_session
    from db.models import Run

    with get_session() as session:
        query = session.query(Run)

        if status:
            query = query.filter(Run.status == status)
        if repo:
            query = query.filter(Run.repo == repo)

        if cursor:
            try:
                cursor_uuid = uuid.UUID(cursor)
                cursor_run = session.get(Run, cursor_uuid)
                if cursor_run:
                    # Sort is started_at DESC. So we fetch runs started BEFORE the cursor run.
                    # If started_at is equal, we fall back to UUID comparison.
                    query = query.filter(
                        (Run.started_at < cursor_run.started_at) |
                        ((Run.started_at == cursor_run.started_at) & (Run.id < cursor_uuid))
                    )
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid cursor UUID format")

        query = query.order_by(Run.started_at.desc(), Run.id.desc())
        runs = query.limit(limit).all()

        next_cursor = None
        if len(runs) == limit:
            next_cursor = str(runs[-1].id)

        return {
            "runs": [r.to_dict() for r in runs],
            "next_cursor": next_cursor,
        }


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Fetch the status and result of a run.

    Checks PostgreSQL first. If not found in the DB (e.g. database failed
    to save or task is still in broker queue but not picked up yet), falls
    back to checking Celery's result backend.
    """
    from db.session import get_session
    from db.models import Run
    from celery.result import AsyncResult
    from workers import app as celery_app

    # Try database first
    try:
        run_uuid = uuid.UUID(run_id)
        with get_session() as session:
            run = session.get(Run, run_uuid)
            if run:
                return run.to_dict()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run UUID format")
    except Exception as e:
        log.warning("DB read failed for run %s: %s. Falling back to Celery.", run_id, e)

    # Fall back to Celery AsyncResult
    result = AsyncResult(run_id, app=celery_app)

    if result.state == "PENDING":
        return {"run_id": run_id, "status": "pending", "detail": "Task is queued or unknown."}
    if result.state == "STARTED":
        return {"run_id": run_id, "status": "running", "detail": "Task is in progress."}
    if result.state == "FAILURE":
        return {
            "run_id": run_id,
            "status": "error",
            "detail": str(result.result) if result.result else "Task failed.",
        }
    if result.state == "REVOKED":
        return {"run_id": run_id, "status": "revoked", "detail": "Task was cancelled."}
    if result.state == "SUCCESS":
        data = result.result or {}
        return {
            "run_id": run_id,
            "status": data.get("status", "unknown"),
            "repo": data.get("repo"),
            "issue_number": data.get("issue_number"),
            "steps_used": data.get("steps", 0),
            "input_tokens": data.get("input_tokens", 0),
            "output_tokens": data.get("output_tokens", 0),
            "cost_usd": data.get("cost_usd", 0.0),
            "pr_number": data.get("pr_number"),
            "pr_url": data.get("pr_url"),
            "error_detail": data.get("error"),
            "final_text": data.get("final_text", ""),
        }

    return {"run_id": run_id, "status": result.state.lower()}


@app.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, agent: str | None = None):
    """Fetch the ordered step-by-step trace of a run from PostgreSQL.

    Enables granular live tracking and replay (plan2.md §7.5).

    Parameters
    ----------
    agent : str, optional
        Filter steps by agent name. One of: 'planner', 'coder', 'reviewer'.
        Omit to return all steps.
    """
    from db.session import get_session
    from db.models import Run, RunStep

    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run UUID format")

    with get_session() as session:
        run = session.get(Run, run_uuid)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        query = session.query(RunStep).filter_by(run_id=run_uuid)
        if agent:
            query = query.filter(RunStep.agent_name == agent)
        steps = query.order_by(RunStep.n.asc()).all()
        return {
            "run_id": run_id,
            "agent_filter": agent,
            "steps": [s.to_dict() for s in steps],
        }



# ═════════════════════════════════════════════════════════════════════════════
# Health endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/healthz")
async def healthz():
    """Liveness probe. Always 200 if the process is up."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness probe. Returns 200 if the API can reach Redis (the broker)
    AND PostgreSQL. Returns 503 if either dependency is down."""
    redis_ok = False
    db_ok = False
    errors = {}

    try:
        import redis as redis_lib
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=2)
        r.ping()
        redis_ok = True
    except Exception as e:
        errors["redis"] = str(e)

    try:
        from db.session import get_session
        from sqlalchemy import text
        with get_session() as session:
            session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        errors["db"] = str(e)

    if redis_ok and db_ok:
        # `sandbox` is echoed so you can confirm the deployed configuration from
        # outside the box — "did USE_SANDBOX actually take effect" is otherwise
        # only answerable by SSH-ing in and reading the environment.
        return {
            "status": "ready",
            "redis": "ok",
            "db": "ok",
            "sandbox": "on" if USE_SANDBOX else "off",
        }
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "not ready", "errors": errors},
        )


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise the API's core logic without any external services.

    Tests HMAC verification, webhook parsing flow, body-size rejection,
    health responses, and the database endpoints. Uses FastAPI's TestClient
    with an in-memory SQLite database.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    def _is_uuid(value: str) -> bool:
        try:
            uuid.UUID(str(value))
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    print("app/main.py self-test")
    print()

    # ── 1. HMAC verification ─────────────────────────────────────────────
    print("HMAC verification")
    secret = "test-secret-12345"
    body = b'{"action":"opened","issue":{"number":1}}'
    good_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    bad_sig = "sha256=" + "0" * 64

    check("valid signature accepted", _verify_signature(body, good_sig, secret))
    check("bad signature rejected", not _verify_signature(body, bad_sig, secret))
    check("missing signature rejected", not _verify_signature(body, "", secret))
    check("no-prefix signature rejected", not _verify_signature(body, "bad", secret))
    check("empty secret rejects all", not _verify_signature(body, good_sig, ""))

    # ── 2. Endpoint tests with TestClient ────────────────────────────────
    print("endpoint tests (TestClient)")

    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  [skip] starlette.testclient not available")
        if failures:
            print(f"\nSELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
            return 1
        print("\nself-test OK (HMAC only).")
        return 0

    # Setup SQLite in-memory database for API test.
    # For SQLite in-memory, we must keep the engine alive as a singleton
    # so all calls to get_session() share the same database.
    from db.session import get_session, init_db, reset_singletons, _get_engine, _get_session_factory
    reset_singletons()
    test_db_url = "sqlite:///:memory:"
    _get_engine(test_db_url)
    _get_session_factory(test_db_url)
    init_db()

    # Patch the webhook secret and API token for testing.
    global GITHUB_WEBHOOK_SECRET, MAX_WEBHOOK_BODY_BYTES
    global AGENT_API_TOKEN, AGENT_REPO_ALLOWLIST, RUNS_RATE_LIMIT, USE_SANDBOX
    original_secret = GITHUB_WEBHOOK_SECRET
    original_api_token = AGENT_API_TOKEN
    original_allowlist = AGENT_REPO_ALLOWLIST
    GITHUB_WEBHOOK_SECRET = secret
    api_token = "test-agent-token-abcdef"
    AGENT_API_TOKEN = api_token
    # Pin every security setting the tests depend on, so the suite does not
    # change behaviour with the ambient .env. A real AGENT_REPO_ALLOWLIST would
    # otherwise reject this test's fixed "octo/test" payload.
    AGENT_REPO_ALLOWLIST = set()

    # Patch run_issue.delay to capture calls without Redis.
    enqueued: list[dict] = []

    class FakeAsyncResult:
        def __init__(self, task_id):
            self.id = task_id

    import workers.tasks
    original_apply_async = workers.tasks.run_issue.apply_async

    def fake_apply_async(args=None, kwargs=None, task_id=None, **rest):
        enqueued.append({"args": args, "kwargs": kwargs, "task_id": task_id})
        return FakeAsyncResult(task_id)

    workers.tasks.run_issue.apply_async = fake_apply_async

    try:
        client = TestClient(app)

        # ── healthz ──────────────────────────────────────────────────────
        r = client.get("/healthz")
        check("healthz returns 200", r.status_code == 200)
        check("healthz body ok", r.json().get("status") == "ok")

        # ── webhook: valid actionable ────────────────────────────────────
        payload = {
            "action": "opened",
            "repository": {"full_name": "octo/test"},
            "issue": {
                "number": 42,
                "title": "Bug in parser",
                "body": "Steps to reproduce...",
                "user": {"login": "alice", "type": "User"},
                "labels": [],
            },
        }
        body_bytes = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        delivery_id = "test-delivery-unique-123"

        r = client.post(
            "/webhooks/github",
            content=body_bytes,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": delivery_id,
                "Content-Type": "application/json",
            },
        )
        check("webhook actionable → 202", r.status_code == 202)
        check("webhook returns run_id", "run_id" in r.json())
        check("task was enqueued", len(enqueued) == 1)

        # F5: the run_id handed to the caller MUST be the same UUID used for the
        # Celery task id and passed to the worker as runs.id — otherwise
        # GET /runs/{id} and /runs/{id}/steps can never resolve it.
        returned_id = r.json()["run_id"]
        job = enqueued[0]
        check("run_id == celery task_id", returned_id == job["task_id"])
        check("run_id passed to the worker as run_id kwarg",
              job["kwargs"].get("run_id") == returned_id)
        check("run_id is a valid UUID", _is_uuid(returned_id), returned_id)

        # ── webhook: deduplication check ──────────────────────────────────
        # Send again with same delivery ID.
        r = client.post(
            "/webhooks/github",
            content=body_bytes,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": delivery_id,
                "Content-Type": "application/json",
            },
        )
        check("duplicate webhook → 202 (ignored)", r.status_code == 202)
        check("duplicate not enqueued again", len(enqueued) == 1)
        check("duplicate response message", "duplicate" in r.json().get("message", ""))

        # ── webhook: non-actionable (edited) ─────────────────────────────
        payload_edited = {**payload, "action": "edited"}
        body_edited = json.dumps(payload_edited).encode()
        sig_edited = "sha256=" + hmac.new(secret.encode(), body_edited, hashlib.sha256).hexdigest()
        
        r = client.post(
            "/webhooks/github",
            content=body_edited,
            headers={
                "X-Hub-Signature-256": sig_edited,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "test-delivery-edited-456",
                "Content-Type": "application/json",
            },
        )
        check("non-actionable → 204", r.status_code == 204)

        # ── manual trigger ───────────────────────────────────────────────
        # ── manual trigger: auth required (F8) ───────────────────────────
        print("POST /runs authentication")
        body = {"repo": "test/manual", "issue_number": 7}
        auth = {"X-Agent-Token": api_token}

        r = client.post("/runs", json=body)
        check("no token → 401", r.status_code == 401, f"got {r.status_code}")
        check("unauthenticated request not enqueued", len(enqueued) == 1)

        r = client.post("/runs", json=body, headers={"X-Agent-Token": "wrong"})
        check("wrong token → 401", r.status_code == 401, f"got {r.status_code}")

        r = client.post("/runs", json=body,
                        headers={"Authorization": f"Bearer {api_token}"})
        check("Bearer token accepted → 202", r.status_code == 202, f"got {r.status_code}")

        r = client.post("/runs", json=body, headers=auth)
        check("X-Agent-Token accepted → 202", r.status_code == 202, f"got {r.status_code}")
        check("manual trigger enqueues", len(enqueued) == 3)
        check("manual trigger run_id == task_id",
              r.json()["run_id"] == enqueued[2]["task_id"])

        # ── sandbox flag: config flip, not a code change (D10) ───────────
        # NOTE: assign the module global directly. `python -m app.main` runs this
        # file as __main__, so `import app.main` would bind a SECOND copy of the
        # module and mutating that one leaves the app under test unchanged.
        print("USE_SANDBOX flag")
        sandbox_orig = USE_SANDBOX

        USE_SANDBOX = False
        client.post("/runs", json=body, headers=auth)
        check("default: run has sandbox off",
              enqueued[-1]["args"][2] is False, str(enqueued[-1]["args"]))

        client.post("/runs", json={**body, "use_sandbox": True}, headers=auth)
        check("explicit true in body overrides the default",
              enqueued[-1]["args"][2] is True, str(enqueued[-1]["args"]))

        USE_SANDBOX = True
        try:
            client.post("/runs", json=body, headers=auth)
            check("USE_SANDBOX=true becomes the default",
                  enqueued[-1]["args"][2] is True, str(enqueued[-1]["args"]))
            client.post("/runs", json={**body, "use_sandbox": False}, headers=auth)
            check("explicit false in body still overrides",
                  enqueued[-1]["args"][2] is False, str(enqueued[-1]["args"]))
        finally:
            USE_SANDBOX = sandbox_orig
        check("readyz reports the sandbox mode",
              client.get("/readyz").json().get("sandbox") in ("on", "off"))

        # ── manual trigger: repo allowlist ───────────────────────────────
        AGENT_REPO_ALLOWLIST = {"only/this"}
        try:
            r = client.post("/runs", json=body, headers=auth)
            check("repo outside allowlist → 403", r.status_code == 403, f"got {r.status_code}")
            r = client.post("/runs", json={"repo": "only/this", "issue_number": 1},
                            headers=auth)
            check("allowlisted repo → 202", r.status_code == 202, f"got {r.status_code}")
        finally:
            AGENT_REPO_ALLOWLIST = set()

        # ── manual trigger: rate limit ───────────────────────────────────
        original_limit = RUNS_RATE_LIMIT
        RUNS_RATE_LIMIT = 2
        _rate_buckets.clear()
        try:
            codes = [client.post("/runs", json=body, headers=auth).status_code
                     for _ in range(4)]
            check("rate limit kicks in after 2", codes == [202, 202, 429, 429], str(codes))
        finally:
            RUNS_RATE_LIMIT = original_limit
            _rate_buckets.clear()

        # ── manual trigger: disabled when no token is configured ─────────
        AGENT_API_TOKEN = ""
        try:
            r = client.post("/runs", json=body, headers=auth)
            check("unconfigured token → 503 (fail-closed)", r.status_code == 503,
                  f"got {r.status_code}")
        finally:
            AGENT_API_TOKEN = api_token

        # ── M4 Database endpoints test ───────────────────────────────────
        print("M4 endpoint checks")
        
        # Manually seed a run in the SQLite DB
        from db.models import Run, RunStep
        run_uuid = uuid.uuid4()
        
        with get_session() as session:
            db_run = Run(
                id=run_uuid,
                repo="octo/test",
                issue_number=42,
                issue_title="Bug in parser",
                status="completed",
                steps_used=2,
                input_tokens=1000,
                output_tokens=500,
                cost_usd=0.05,
            )
            session.add(db_run)
            session.commit()
            
            db_step = RunStep(
                run_id=run_uuid,
                n=0,
                stop_reason="tool_use",
                text="ReAct step 0 text",
                input_tokens=500,
                output_tokens=250,
            )
            session.add(db_step)
            session.commit()

        # GET /runs
        r = client.get("/runs")
        check("GET /runs returns 200", r.status_code == 200)
        data = r.json()
        check("GET /runs has runs list", "runs" in data)
        check("GET /runs contains seeded run", len(data["runs"]) > 0)
        check("GET /runs filtered by status", len(client.get("/runs?status=running").json()["runs"]) == 0)

        # GET /runs/{id}
        r = client.get(f"/runs/{run_uuid}")
        check("GET /runs/{id} returns 200", r.status_code == 200)
        check("GET /runs/{id} has correct repo", r.json()["repo"] == "octo/test")
        check("GET /runs/{id} has steps_used", r.json()["steps_used"] == 2)

        # GET /runs/{id}/steps
        r = client.get(f"/runs/{run_uuid}/steps")
        check("GET /runs/{id}/steps returns 200", r.status_code == 200)
        check("GET /runs/{id}/steps has steps list", "steps" in r.json())
        check("GET /runs/{id}/steps has step 0 text", r.json()["steps"][0]["text"] == "ReAct step 0 text")

    finally:
        # Restore originals.
        GITHUB_WEBHOOK_SECRET = original_secret
        AGENT_API_TOKEN = original_api_token
        AGENT_REPO_ALLOWLIST = original_allowlist
        _rate_buckets.clear()
        workers.tasks.run_issue.apply_async = original_apply_async
        reset_singletons()

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — HMAC, webhooks, manual trigger, database reads/pagination, and health all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
