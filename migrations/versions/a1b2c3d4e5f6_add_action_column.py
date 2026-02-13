"""Add action column to WebhookLog

Revision ID: a1b2c3d4e5f6
Revises: 8ed2564b4bb6
Create Date: 2026-02-11 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "8ed2564b4bb6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_log", sa.Column("action", sa.String(length=50), nullable=True))
    op.add_column("webhook_config", sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"))


def downgrade():
    op.drop_column("webhook_config", "is_enabled")
    op.drop_column("webhook_log", "action")
