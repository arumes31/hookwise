"""Store the outcome of the latest stale-endpoint check per endpoint.

Revision ID: b4e2c7a9d1f3
Revises: a7d3e5f9b1c4
Create Date: 2026-10-14 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e2c7a9d1f3"
down_revision = "a7d3e5f9b1c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_config", sa.Column("stale_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("webhook_config", sa.Column("stale_check_result", sa.String(length=32), nullable=True))
    op.add_column("webhook_config", sa.Column("stale_check_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("webhook_config", "stale_check_error")
    op.drop_column("webhook_config", "stale_check_result")
    op.drop_column("webhook_config", "stale_checked_at")
