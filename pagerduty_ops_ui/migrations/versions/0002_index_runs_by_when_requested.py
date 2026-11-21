"""Index Runs by when they were requested, for the newest-first Run history (#69).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_runs_queued_at", "runs", ["queued_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_queued_at", "runs")
