"""db — PostgreSQL persistence layer for the autonomous SWE agent.

This package provides SQLAlchemy models (runs, run_steps, webhook_events),
a session factory, and Alembic migrations. It is the system of record for
all agent activity (plan2.md §7.1).

Quick start:
    from db.session import get_session
    from db.models import Run, RunStep, WebhookEvent

    with get_session() as session:
        run = session.get(Run, run_id)
"""

from db.models import Base, Run, RunStep, WebhookEvent
from db.session import get_session, init_db

__all__ = ["Base", "Run", "RunStep", "WebhookEvent", "get_session", "init_db"]
