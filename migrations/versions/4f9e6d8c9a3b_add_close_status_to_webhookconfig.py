"""add close_status to WebhookConfig

Revision ID: 4f9e6d8c9a3b
Revises: 13be59e49f08
Create Date: 2026-02-20 14:50:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "4f9e6d8c9a3b"
down_revision = "13be59e49f08"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_config", sa.Column("close_status", sa.String(length=100), nullable=True))


def downgrade():
    op.drop_column("webhook_config", "close_status")
