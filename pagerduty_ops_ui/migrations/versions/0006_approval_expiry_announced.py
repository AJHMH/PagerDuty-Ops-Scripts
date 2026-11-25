"""Record when an Approval's expiry was announced in Teams, so it is announced once (#78).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("approval_expiry_announced_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("approval_expiry_announced_at")
