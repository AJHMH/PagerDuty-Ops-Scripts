"""Record Approvals of Irreversible operations with their Runs (#77).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

COLUMNS = (
    ("approval_status", sa.Unicode(20)),
    ("approval_reasons", sa.JSON()),
    ("approval_requested_at", sa.DateTime(timezone=True)),
    ("approval_expires_at", sa.DateTime(timezone=True)),
    ("approval_decided_by", sa.Unicode(320)),
    ("approval_decided_at", sa.DateTime(timezone=True)),
    ("approval_comment", sa.Unicode(500)),
)


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for name, type_ in COLUMNS:
            batch.add_column(sa.Column(name, type_))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
