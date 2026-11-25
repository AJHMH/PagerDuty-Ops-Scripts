"""Schedules for read-only commands, and the Scheduled runs they start (#80, ADR 0014).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Unicode(36), primary_key=True),
        sa.Column("name", sa.Unicode(100), nullable=False),
        sa.Column("command", sa.Unicode(100), nullable=False),
        sa.Column("subcommand", sa.Unicode(100)),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("timing", sa.JSON(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Unicode(320), nullable=False),
        sa.Column("creator_name", sa.Unicode(200), nullable=False),
        sa.Column("creator_role", sa.Unicode(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Unicode(320)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_id", sa.Unicode(36)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("schedule_id", sa.Unicode(36)))
        batch.add_column(sa.Column("drift", sa.Boolean()))
        batch.add_column(sa.Column("drift_summary", sa.Unicode(500)))
        batch.create_index("ix_runs_schedule_id", ["schedule_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_schedule_id")
        batch.drop_column("drift_summary")
        batch.drop_column("drift")
        batch.drop_column("schedule_id")
    op.drop_index("ix_schedules_next_run_at", "schedules")
    op.drop_table("schedules")
