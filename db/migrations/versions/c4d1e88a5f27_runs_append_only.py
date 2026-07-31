"""make runs append-only; enforce one ACTIVE run per issue

Replaces the unique constraint on (repo, issue_number) with a PARTIAL unique
index restricted to status='running'.

Why (plan2.md §16 D16, §22 F7/F10): the old constraint meant only one run per
issue could ever exist, so re-running an issue overwrote its record — a strange
property for a system whose main value is a durable audit trail. Worse, the
orchestrator worked around it by mutating the row's primary key in place, which
orphaned the run's own run_steps children.

The new index keeps the guarantee that actually matters (never two agents
working the same issue at once) while letting finished runs accumulate. Because
the database enforces it, two workers racing on the same issue cannot both
start: the loser gets an IntegrityError and stands down, so no application-level
lock is needed.

Partial indexes are supported by PostgreSQL and by SQLite 3.8+, so the same
guarantee holds in tests as in production.

Revision ID: c4d1e88a5f27
Revises: b7e4a9f12c03
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4d1e88a5f27'
down_revision: Union[str, Sequence[str], None] = 'b7e4a9f12c03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CONSTRAINT = "uq_runs_repo_issue"
_NEW_INDEX = "uq_runs_active_issue"


def upgrade() -> None:
    """Drop the total unique constraint, add the partial one.

    No data migration is needed: the old constraint guaranteed at most one row
    per (repo, issue_number), so there cannot already be two rows in 'running'
    for the same issue.
    """
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.drop_constraint(_OLD_CONSTRAINT, "runs", type_="unique")
    else:
        # SQLite cannot drop a constraint in place; batch mode rebuilds the table.
        with op.batch_alter_table("runs") as batch:
            batch.drop_constraint(_OLD_CONSTRAINT, type_="unique")

    op.create_index(
        _NEW_INDEX,
        "runs",
        ["repo", "issue_number"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Restore the total unique constraint.

    NOTE: this is lossy-by-refusal. Once runs have accumulated, more than one
    row per (repo, issue_number) will exist and recreating the constraint will
    FAIL. That is deliberate — silently deleting run history to satisfy a
    downgrade would be worse. Dedupe manually first, e.g.:

        DELETE FROM runs a USING runs b
         WHERE a.repo = b.repo
           AND a.issue_number = b.issue_number
           AND a.started_at < b.started_at;
    """
    op.drop_index(_NEW_INDEX, table_name="runs")
    op.create_unique_constraint(
        _OLD_CONSTRAINT, "runs", ["repo", "issue_number"],
    )
