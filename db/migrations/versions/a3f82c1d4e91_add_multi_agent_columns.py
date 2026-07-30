"""add multi-agent columns

Adds the columns introduced by the Planner → Coder → Reviewer pipeline:

  runs.planner_output   — JSONB: full PlannerOutput stored per run
  runs.reviewer_output  — JSONB: last ReviewerOutput stored per run
  run_steps.agent_name  — VARCHAR(20): "planner" | "coder" | "reviewer"

All new columns are nullable so existing rows are unaffected and no
backfill is required.

Revision ID: a3f82c1d4e91
Revises: ce67fdd77d09
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f82c1d4e91'
down_revision: Union[str, Sequence[str], None] = 'ce67fdd77d09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Portable JSON type: JSONB on Postgres, plain JSON elsewhere (e.g. SQLite in
# tests).  Mirrors the PortableJSON definition in db/models.py.
_PortableJSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    """Add multi-agent columns to runs and run_steps."""

    # ── runs table ────────────────────────────────────────────────────────────
    # planner_output: the full PlannerOutput dict for this run.
    op.add_column(
        "runs",
        sa.Column("planner_output", _PortableJSON, nullable=True),
    )
    # reviewer_output: the last ReviewerOutput dict for this run.
    op.add_column(
        "runs",
        sa.Column("reviewer_output", _PortableJSON, nullable=True),
    )

    # ── run_steps table ───────────────────────────────────────────────────────
    # agent_name: which agent produced this step ("planner", "coder", "reviewer").
    op.add_column(
        "run_steps",
        sa.Column("agent_name", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    """Remove multi-agent columns."""
    op.drop_column("run_steps", "agent_name")
    op.drop_column("runs", "reviewer_output")
    op.drop_column("runs", "planner_output")
