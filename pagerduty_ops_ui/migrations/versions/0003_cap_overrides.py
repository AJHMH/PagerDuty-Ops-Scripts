"""Record a cap override with its Run: the reason, who gave it, and the caps raised (#75).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("cap_override_reason", sa.Unicode(500)))
        batch.add_column(sa.Column("cap_override_by", sa.Unicode(320)))
        batch.add_column(sa.Column("cap_overrides", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("cap_overrides")
        batch.drop_column("cap_override_by")
        batch.drop_column("cap_override_reason")
