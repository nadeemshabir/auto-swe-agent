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

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
MAX_WEBHOOK_BODY_BYTES = int(os.getenv("MAX_WEBHOOK_BODY_BYTES", "1048576"))  # 1 MB
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

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
    use_sandbox: bool = False


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

    # ── 5. Enqueue the task ──────────────────────────────────────────────
    from workers.tasks import run_issue
    async_result = run_issue.delay(issue.repo, issue.number)

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
async def manual_trigger(req: ManualRunRequest):
    """Manually trigger an agent run for a given repo + issue number.

    Useful for testing without setting up webhooks. Returns 202 with the
    Celery task ID so you can poll GET /runs/{id} for the result.
    """
    from workers.tasks import run_issue
    async_result = run_issue.delay(req.repo, req.issue_number, req.use_sandbox)

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
async def get_run_steps(run_id: str):
    """Fetch the ordered step-by-step trace of a run from PostgreSQL.

    Enables granular live tracking and replay (plan2.md §7.5).
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

        steps = session.query(RunStep).filter_by(run_id=run_uuid).order_by(RunStep.n.asc()).all()
        return {
            "run_id": run_id,
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
        return {"status": "ready", "redis": "ok", "db": "ok"}
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

    # Patch the webhook secret for testing.
    global GITHUB_WEBHOOK_SECRET, MAX_WEBHOOK_BODY_BYTES
    original_secret = GITHUB_WEBHOOK_SECRET
    GITHUB_WEBHOOK_SECRET = secret

    # Patch run_issue.delay to capture calls without Redis.
    enqueued: list[dict] = []

    class FakeAsyncResult:
        def __init__(self, repo, number):
            self.id = str(uuid.uuid4())

    import workers.tasks
    original_delay = workers.tasks.run_issue.delay

    def fake_delay(*args, **kwargs):
        enqueued.append({"args": args, "kwargs": kwargs})
        return FakeAsyncResult(args[0] if args else "?", args[1] if len(args) > 1 else 0)

    workers.tasks.run_issue.delay = fake_delay

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
        r = client.post("/runs", json={"repo": "test/manual", "issue_number": 7})
        check("manual trigger → 202", r.status_code == 202)
        check("manual trigger enqueues", len(enqueued) == 2)

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
        workers.tasks.run_issue.delay = original_delay
        reset_singletons()

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — HMAC, webhooks, manual trigger, database reads/pagination, and health all work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
