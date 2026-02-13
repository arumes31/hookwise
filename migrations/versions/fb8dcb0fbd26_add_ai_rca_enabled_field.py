"""Add ai_rca_enabled field

Revision ID: fb8dcb0fbd26
Revises: f3d1f91804f7
Create Date: 2026-02-12 22:59:52.001451

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "fb8dcb0fbd26"
down_revision = "f3d1f91804f7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_config", sa.Column("ai_rca_enabled", sa.Boolean(), nullable=False, server_default="false"))


def downgrade():
    op.drop_column("webhook_config", "ai_rca_enabled")
