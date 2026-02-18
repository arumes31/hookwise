"""Add bearer_auth_enabled field

Revision ID: e5f6a7b8c9d0
Revises: 13be59e49f08
Create Date: 2026-02-18 11:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "13be59e49f08"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "webhook_config",
        sa.Column("bearer_auth_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade():
    op.drop_column("webhook_config", "bearer_auth_enabled")
