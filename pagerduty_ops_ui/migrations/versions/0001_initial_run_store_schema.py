"""The Run store as of #74: Runs (with the writing queue's status) and their files.

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Unicode(36), primary_key=True),
        sa.Column("command", sa.Unicode(100), nullable=False),
        sa.Column("argv", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.Unicode(320), nullable=False),
        sa.Column("requester_name", sa.Unicode(200), nullable=False),
        sa.Column("requester_role", sa.Unicode(20), nullable=False),
        sa.Column("kind", sa.Unicode(10), nullable=False),
        sa.Column("status", sa.Unicode(10), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("pid", sa.Integer()),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("outcome", sa.Unicode(20)),
        sa.Column("note", sa.UnicodeText()),
        sa.Column("cancelled_by", sa.Unicode(320)),
        sa.Column("stdout", sa.UnicodeText(), nullable=False),
        sa.Column("stderr", sa.UnicodeText(), nullable=False),
        sa.Column("input_hash", sa.Unicode(64), nullable=False),
        sa.Column("output_hash", sa.Unicode(64), nullable=False),
        sa.Column("reviewed_by", sa.Unicode(320)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("applied_run_id", sa.Unicode(36)),
        sa.Column("applied_from", sa.Unicode(36)),
        sa.Column("fresh_dry_run_id", sa.Unicode(36)),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "run_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Unicode(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("role", sa.Unicode(10), nullable=False),
        sa.Column("path", sa.Unicode(400), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Unicode(64), nullable=False),
    )
    op.create_index("ix_run_files_run_id", "run_files", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_files_run_id", "run_files")
    op.drop_table("run_files")
    op.drop_index("ix_runs_status", "runs")
    op.drop_table("runs")
