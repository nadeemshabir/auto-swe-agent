"""
db/models.py
SQLAlchemy ORM models — the system of record for all agent activity.

Tables defined here match plan2.md §7.1:

    runs           — one row per issue run (status, cost, PR link, etc.)
    run_steps      — one row per ReAct step (tool calls, tokens, model text)
    webhook_events — dedupe + audit of every GitHub webhook received

The `runs` table has a unique constraint on (repo, issue_number) to enforce
idempotency: re-processing the same issue updates the existing row instead
of creating a duplicate (plan2.md §5, G6).

Offline self-test:
    python -m db.models
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship

# Use JSONB on Postgres (indexable), plain JSON on SQLite (for tests).
PortableJSON = JSON().with_variant(JSONB, "postgresql")


# ── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Shared base for all ORM models."""
    pass


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Used as default for timestamp columns."""
    return datetime.now(timezone.utc)


# ── runs ─────────────────────────────────────────────────────────────────────

class Run(Base):
    """One row per autonomous agent run.

    **Append-only** (plan2.md §16 D16): re-running an issue inserts a NEW row and
    keeps the old one, so a run's history survives. The table previously carried
    a unique constraint on (repo, issue_number), which meant only one run per
    issue could ever exist — re-running destroyed the audit trail the whole
    persistence layer exists to provide (§22 F10).

    Idempotency is instead enforced by a *partial* unique index over
    (repo, issue_number) restricted to status='running': at most one ACTIVE run
    per issue, any number of finished ones. Because the database enforces it,
    two workers racing on the same issue cannot both start — the loser gets an
    IntegrityError and stands down, with no application-level lock needed.

    Statuses:
        queued, running, completed, no_changes, refused, max_steps,
        token_budget, usd_budget, max_tokens, provider_error, sandbox_error,
        github_error, index_error, error, skipped_duplicate,
        stale  — abandoned by a crashed worker and reaped (see §13)
    """

    __tablename__ = "runs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo        = Column(String(255), nullable=False, index=True)
    issue_number = Column(Integer, nullable=False)
    issue_title = Column(String(500), nullable=True)

    provider    = Column(String(50), nullable=True)   # "gemini" / "anthropic"
    model       = Column(String(100), nullable=True)  # e.g. "gemini-3.5-flash"

    status      = Column(String(30), nullable=False, default="queued", index=True)
    branch      = Column(String(255), nullable=True)
    pr_number   = Column(Integer, nullable=True)
    pr_url      = Column(String(500), nullable=True)

    steps_used    = Column(Integer, nullable=False, default=0)
    input_tokens  = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_usd      = Column(Float, nullable=False, default=0.0)

    started_at   = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    finished_at  = Column(DateTime(timezone=True), nullable=True)
    error_detail = Column(Text, nullable=True)
    final_text   = Column(Text, nullable=True)

    # Multi-agent outputs — stored as JSON so the API can expose them without
    # extra queries and the PR description assembly has all data in one place.
    # Nullable for backward compatibility with pre-multi-agent runs.
    planner_output  = Column(PortableJSON, nullable=True)  # PlannerOutput.to_dict()
    reviewer_output = Column(PortableJSON, nullable=True)  # Last ReviewerOutput.to_dict()
    test_evidence   = Column(PortableJSON, nullable=True)  # {"red": "...", "green": "..."}

    # Relationships
    steps = relationship("RunStep", back_populates="run", order_by="RunStep.n",
                         cascade="all, delete-orphan")

    # Idempotency: at most one ACTIVE run per (repo, issue_number). Finished
    # runs accumulate freely, so history is preserved. (plan2.md §5 G6, §7.1, D16)
    #
    # Partial indexes are supported by both Postgres and SQLite (3.8+), so the
    # same guarantee holds in tests as in production.
    __table_args__ = (
        Index(
            "uq_runs_active_issue",
            "repo", "issue_number",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict for API responses."""
        return {
            "id": str(self.id),
            "repo": self.repo,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "steps_used": self.steps_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_detail": self.error_detail,
            "planner_output": self.planner_output,
            "reviewer_output": self.reviewer_output,
            "test_evidence": self.test_evidence,
        }


# ── run_steps ────────────────────────────────────────────────────────────────

class RunStep(Base):
    """One row per ReAct step within a run.

    Written incrementally so a crashed run still has partial trace data
    for debugging (plan2.md §4 step 8, §7.1).
    """

    __tablename__ = "run_steps"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id       = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    n            = Column(Integer, nullable=False)  # step number (0-indexed)

    stop_reason  = Column(String(30), nullable=True)  # end_turn, tool_use, max_tokens, refusal
    text         = Column(Text, nullable=True)  # model's text output (clipped)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)

    # Tool calls as JSON: [{name, args, result, is_error}, ...]
    # Uses JSONB on Postgres for indexability; falls back to JSON on SQLite.
    tools        = Column(PortableJSON, nullable=True)

    # Multi-agent: which agent produced this step.
    # Values: "planner", "coder", "reviewer". Nullable for old rows.
    agent_name   = Column(String(20), nullable=True)

    created_at   = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Relationships
    run = relationship("Run", back_populates="steps")

    __table_args__ = (
        # Ensure step numbers are unique within a run.
        UniqueConstraint("run_id", "n", name="uq_run_steps_run_n"),
    )

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict for API responses."""
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "n": self.n,
            "agent_name": self.agent_name,
            "stop_reason": self.stop_reason,
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tools": self.tools,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ── webhook_events ───────────────────────────────────────────────────────────

class WebhookEvent(Base):
    """Dedupe + audit log for every GitHub webhook received.

    The unique constraint on delivery_id guards against GitHub redeliveries
    (plan2.md §7.1, §9).
    """

    __tablename__ = "webhook_events"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    delivery_id  = Column(String(100), nullable=False, unique=True, index=True)
    event_type   = Column(String(50), nullable=False)
    repo         = Column(String(255), nullable=True)
    issue_number = Column(Integer, nullable=True)
    action_taken = Column(String(20), nullable=False)  # enqueued, skipped, duplicate
    received_at  = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "event_type": self.event_type,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "action_taken": self.action_taken,
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Exercise the models against an in-memory SQLite database.

    No Postgres needed — validates the schema, relationships, constraints,
    and serialization logic.
    """
    import logging
    logging.basicConfig(level=logging.WARNING)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("db/models.py self-test")
    print()

    # ── 1. Create in-memory SQLite database ──────────────────────────────
    print("schema creation")
    # SQLite doesn't support JSONB, so we need to handle that.
    # SQLAlchemy auto-falls back to JSON for SQLite.
    engine = create_engine("sqlite:///:memory:", echo=False)
    # For SQLite, we need to render JSONB as JSON
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
    from sqlalchemy import JSON

    # Create tables — the JSONB column will work as JSON on SQLite
    Base.metadata.create_all(engine)
    check("tables created", True)

    # ── 2. Insert a run ──────────────────────────────────────────────────
    print("CRUD operations")
    run_id = uuid.uuid4()
    with Session(engine) as session:
        run = Run(
            id=run_id,
            repo="owner/test-repo",
            issue_number=42,
            issue_title="Fix the parser",
            provider="gemini",
            model="gemini-3.5-flash",
            status="running",
        )
        session.add(run)
        session.commit()

        # Read it back.
        fetched = session.get(Run, run_id)
        check("run inserted and read", fetched is not None)
        check("run repo correct", fetched.repo == "owner/test-repo")
        check("run status correct", fetched.status == "running")

    # ── 3. Insert steps ──────────────────────────────────────────────────
    with Session(engine) as session:
        for i in range(3):
            step = RunStep(
                run_id=run_id,
                n=i,
                stop_reason="tool_use" if i < 2 else "end_turn",
                text=f"Step {i} text",
                input_tokens=100 * (i + 1),
                output_tokens=50 * (i + 1),
                tools=[{"name": "read_file", "args": {"path": "main.py"},
                        "result": "contents...", "is_error": False}] if i < 2 else None,
            )
            session.add(step)
        session.commit()

        # Read steps for this run.
        run = session.get(Run, run_id)
        check("3 steps inserted", len(run.steps) == 3)
        check("steps ordered by n", [s.n for s in run.steps] == [0, 1, 2])
        check("step has tools", run.steps[0].tools is not None)

    # ── 4. Insert webhook event ──────────────────────────────────────────
    with Session(engine) as session:
        evt = WebhookEvent(
            delivery_id="abc-123",
            event_type="issues",
            repo="owner/test-repo",
            issue_number=42,
            action_taken="enqueued",
        )
        session.add(evt)
        session.commit()

        fetched_evt = session.query(WebhookEvent).filter_by(delivery_id="abc-123").one()
        check("webhook event inserted", fetched_evt is not None)
        check("webhook action correct", fetched_evt.action_taken == "enqueued")

    # ── 5. Test idempotency: one ACTIVE run, many finished ones ──────────
    # The seeded run above is status='running', so a second running row for the
    # same (repo, issue) must be rejected while a finished one is allowed —
    # that is what makes the table append-only (D16 / §22 F10).
    print("idempotency: partial unique index on active runs")
    from sqlalchemy.exc import IntegrityError
    with Session(engine) as session:
        second_active = Run(
            repo="owner/test-repo",
            issue_number=42,
            issue_title="Concurrent attempt",
            status="running",
        )
        session.add(second_active)
        try:
            session.commit()
            check("second ACTIVE run rejected", False, "should have raised IntegrityError")
        except IntegrityError:
            session.rollback()
            check("second ACTIVE run rejected", True)

    with Session(engine) as session:
        historical = Run(
            repo="owner/test-repo",
            issue_number=42,
            issue_title="Earlier attempt",
            status="completed",
        )
        session.add(historical)
        try:
            session.commit()
            check("finished run for the same issue allowed (history kept)", True)
        except IntegrityError:
            session.rollback()
            check("finished run for the same issue allowed (history kept)", False,
                  "append-only insert was rejected")

        kept = session.query(Run).filter_by(repo="owner/test-repo", issue_number=42).count()
        check("both runs retained for the same issue", kept == 2, f"got {kept}")

    # ── 6. Test webhook dedupe constraint ────────────────────────────────
    with Session(engine) as session:
        dupe_evt = WebhookEvent(
            delivery_id="abc-123",  # same as above
            event_type="issues",
            action_taken="duplicate",
        )
        session.add(dupe_evt)
        try:
            session.commit()
            check("duplicate delivery_id rejected", False, "should have raised IntegrityError")
        except IntegrityError:
            session.rollback()
            check("duplicate delivery_id rejected", True)

    # ── 7. Test serialization ────────────────────────────────────────────
    print("serialization")
    with Session(engine) as session:
        run = session.get(Run, run_id)
        d = run.to_dict()
        check("to_dict has id", d["id"] == str(run_id))
        check("to_dict has repo", d["repo"] == "owner/test-repo")
        check("to_dict has status", d["status"] == "running")

        step_d = run.steps[0].to_dict()
        check("step to_dict has n", step_d["n"] == 0)
        check("step to_dict has tools", step_d["tools"] is not None)

    # ── 8. Test update (simulating run completion) ───────────────────────
    print("run update (completion)")
    with Session(engine) as session:
        run = session.get(Run, run_id)
        run.status = "completed"
        run.pr_number = 7
        run.pr_url = "https://github.com/owner/test-repo/pull/7"
        run.steps_used = 3
        run.input_tokens = 600
        run.output_tokens = 300
        run.cost_usd = 0.0123
        run.finished_at = _utcnow()
        session.commit()

        run = session.get(Run, run_id)
        check("status updated to completed", run.status == "completed")
        check("pr_number set", run.pr_number == 7)
        check("cost_usd set", abs(run.cost_usd - 0.0123) < 1e-6)
        check("finished_at set", run.finished_at is not None)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed → {failures}")
        return 1
    print("self-test OK — all models, constraints, relationships, and serialization work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
