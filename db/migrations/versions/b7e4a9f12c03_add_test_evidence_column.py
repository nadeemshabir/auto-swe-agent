"""add test_evidence column

Adds the test_evidence JSONB column to the runs table to store
Red/Green TDD evidence captured from the Coder agent's final output.

Schema: {"red": "<failing test output>", "green": "<passing test output>"}

Nullable so existing rows are unaffected and no backfill is required.

Revision ID: b7e4a9f12c03
Revises: a3f82c1d4e91
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e4a9f12c03'
down_revision: Union[str, Sequence[str], None] = 'a3f82c1d4e91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Portable JSON type: JSONB on Postgres, plain JSON elsewhere.
_PortableJSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    """Add test_evidence column to runs."""
    op.add_column(
        "runs",
        sa.Column("test_evidence", _PortableJSON, nullable=True),
    )


def downgrade() -> None:
    """Remove test_evidence column."""
    op.drop_column("runs", "test_evidence")
