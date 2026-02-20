"""Add health and security fields

Revision ID: e7d4f6c5b2a1
Revises: b7c8d9e0f1a2
Create Date: 2026-02-20 09:40:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7d4f6c5b2a1"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_config", sa.Column("config_health_status", sa.String(length=20), server_default="OK", nullable=True))
    op.add_column("webhook_config", sa.Column("config_health_message", sa.String(length=255), nullable=True))
    op.add_column("webhook_config", sa.Column("last_ip", sa.String(length=45), nullable=True))


def downgrade():
    op.drop_column("webhook_config", "last_ip")
    op.drop_column("webhook_config", "config_health_message")
    op.drop_column("webhook_config", "config_health_status")
