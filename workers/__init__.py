"""
workers — Celery application and task definitions.

This package defines the Celery app (the "worker brain") and the orchestrator
task that drives the full issue→PR pipeline. The app is configured from
environment variables, consistent with how agent/ bootstraps from .env.

Start a worker:
    celery -A workers worker --loglevel=info

Enqueue a task programmatically:
    from workers.tasks import run_issue
    result = run_issue.delay("owner/repo", 42)
    print(result.get(timeout=300))
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env so REDIS_URL, WORKSPACE_ROOT, etc. are visible.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from celery import Celery

# ── Celery app ───────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery("workers")

app.conf.update(
    # Broker (message queue) — Redis.
    broker_url=REDIS_URL, #receiver{inbox when main.py receives a webhook}

    # Result backend — same Redis, different DB so broker and results don't
    # collide. Swap /0 → /1 if the URL ends with a DB number.
    result_backend=REDIS_URL.rsplit("/", 1)[0] + "/1"
    if "/" in REDIS_URL.rsplit(":", 1)[-1] 
    else REDIS_URL + "/1", #sender{where the worker stores the results of the task it executed}

    # Serialization — JSON everywhere for debuggability and safety (no pickle).
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone.
    timezone="UTC",
    enable_utc=True,

    # Reliability: acknowledge tasks AFTER they finish, not when they are
    # picked up. If a worker crashes mid-task, the message goes back to the
    # queue and another worker can retry it.
    task_acks_late=True,

    # Don't prefetch more than one task per worker slot — each run_issue is
    # heavy (clones a repo, calls an LLM many times) so we want even load
    # distribution across workers.
    worker_prefetch_multiplier=1,

    # Store results for 24 hours (enough to inspect a run, not forever).
    result_expires=86400,
)

# Auto-discover tasks in this package.
app.autodiscover_tasks(["workers"])
