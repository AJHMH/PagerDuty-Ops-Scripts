"""The permanent Ledger of Writing runs (#76, ADR 0013).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Unicode(36), nullable=False, unique=True),
        sa.Column("command", sa.Unicode(100), nullable=False),
        sa.Column("requested_by", sa.Unicode(320), nullable=False),
        sa.Column("requester_name", sa.Unicode(200), nullable=False),
        sa.Column("requester_role", sa.Unicode(20), nullable=False),
        sa.Column("approved_by", sa.Unicode(320)),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.Unicode(20)),
    )
    op.create_index("ix_ledger_queued_at", "ledger", ["queued_at"])


def downgrade() -> None:
    op.drop_index("ix_ledger_queued_at", "ledger")
    op.drop_table("ledger")
